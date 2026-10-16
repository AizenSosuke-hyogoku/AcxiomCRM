"""Privacy-aware AI features. All endpoints have deterministic fallbacks."""
from __future__ import annotations
import os, random, math
from pathlib import Path
from datetime import date, datetime
from typing import Any
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="AcxiomCRM AI Service", version="1.0.0", description="Local lead scoring, summaries and fair workload assignment")
MODEL = None
MODEL_PATH = Path(os.getenv("MODEL_PATH", str(Path(__file__).resolve().parent / "artifacts" / "lead_model.joblib")))
FEATURES = ["interaction_count", "expected_value", "industry", "status", "days_since_last_contact", "source"]
INDUSTRIES = ["Fintech", "Healthcare", "Retail", "Logistics", "EdTech", "Technology"]
STATUSES = ["New", "Contacted", "Qualified", "Unqualified", "Converted", "Lost"]
SOURCES = ["Website", "Referral", "LinkedIn", "Event", "Cold Outreach"]

def label_rule(row: dict[str, Any]) -> int:
    score = 0.25 + min(int(row.get("interaction_count", 0)), 12) * 0.035
    score += min(float(row.get("expected_value", 0)), 2_000_000) / 2_000_000 * 0.12
    score += 0.22 if row.get("status") == "Qualified" else 0.10 if row.get("status") == "Contacted" else 0
    score += 0.10 if row.get("source") == "Referral" else 0.05 if row.get("source") == "LinkedIn" else 0
    score -= min(int(row.get("days_since_last_contact", 30)), 90) * 0.004
    return int(score + random.uniform(-0.12, 0.12) > 0.52)

def train_model():
    global MODEL
    try:
        random.seed(73)
        import numpy as np
        from sklearn.compose import ColumnTransformer
        from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder, StandardScaler
        import pandas as pd
        rng = np.random.default_rng(73)
        rows, labels = [], []
        for _ in range(2400):
            row = {
                "interaction_count": int(rng.integers(0, 16)),
                "expected_value": float(rng.integers(25_000, 2_500_000)),
                "industry": str(rng.choice(INDUSTRIES)),
                "status": str(rng.choice(STATUSES)),
                "days_since_last_contact": int(rng.integers(0, 91)),
                "source": str(rng.choice(SOURCES)),
            }
            rows.append(row)
            labels.append(label_rule(row))
        frame = pd.DataFrame(rows, columns=FEATURES)
        categorical = ["industry", "status", "source"]
        numeric = ["interaction_count", "expected_value", "days_since_last_contact"]
        prep = ColumnTransformer([
            ("cat", OneHotEncoder(handle_unknown="ignore"), categorical),
            ("num", StandardScaler(), numeric),
        ])
        model = Pipeline([("features", prep), ("classifier", RandomForestClassifier(n_estimators=90, max_depth=8, class_weight="balanced", random_state=73, n_jobs=1))])
        model.fit(frame, labels)
        MODEL = model
        try:
            import joblib
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(model, MODEL_PATH)
        except Exception as save_exc:
            print(f"[ai-service] Model trained in memory; artifact save skipped: {save_exc}")
    except Exception as exc:
        # Never make scoring unavailable because an optional ML dependency failed.
        print(f"[ai-service] Model training skipped; deterministic fallback active: {exc}")
        MODEL = None

@app.on_event("startup")
def startup():
    global MODEL
    if MODEL_PATH.exists():
        try:
            import joblib
            MODEL = joblib.load(MODEL_PATH)
            return
        except Exception as exc:
            print(f"[ai-service] Existing model could not load; retraining: {exc}")
    train_model()

class LeadInput(BaseModel):
    interaction_count: int = Field(ge=0, le=1000, default=0)
    expected_value: float = Field(ge=0, le=100_000_000, default=0)
    industry: str = "Technology"
    status: str = "New"
    days_since_last_contact: int = Field(ge=0, le=3650, default=30)
    source: str = "Website"

class SummaryInput(BaseModel):
    entity_type: str = "lead"
    company_alias: str = "Account"
    days_silent: int = 0
    overdue_followups: int = 0
    open_opportunities: int = 0
    pipeline_value: float = 0
    ai_score: float = 0
    status: str = "New"
    recent_sentiment: str = "Neutral"

class Candidate(BaseModel):
    user_id: int
    name: str
    open_workload: int = 0
    industry_wins: int = 0
    capacity: int = 12

class AssignInput(BaseModel):
    industry: str = "Technology"
    candidates: list[Candidate]

@app.get("/health")
def health():
    return {"status": "ok", "model": "random_forest" if MODEL is not None else "deterministic_fallback"}

@app.post("/ai/score")
def score_lead(payload: LeadInput):
    row = payload.model_dump()
    probability = None
    if MODEL is not None:
        try:
            import pandas as pd
            probability = float(MODEL.predict_proba(pd.DataFrame([row], columns=FEATURES))[0][1]) * 100
        except Exception:
            probability = None
    if probability is None:
        raw = 35 + min(payload.interaction_count, 12) * 2.6 + min(payload.expected_value / 100_000, 15)
        raw += 18 if payload.status == "Qualified" else 8 if payload.status == "Contacted" else 0
        raw += 8 if payload.source == "Referral" else 4 if payload.source == "LinkedIn" else 0
        raw -= min(payload.days_since_last_contact, 60) * 0.75
        probability = max(2, min(97, raw))
    probability = round(max(1, min(99, probability)), 1)
    band = "Hot" if probability > 70 else "Warm" if probability >= 40 else "Cold"
    reasons = []
    reasons.append("recent engagement supports momentum" if payload.days_since_last_contact <= 7 else "contact is going quiet" if payload.days_since_last_contact > 21 else "contact cadence is moderate")
    reasons.append("qualification signals are positive" if payload.status == "Qualified" else "more qualification evidence is needed")
    reasons.append("multiple interactions provide useful signal" if payload.interaction_count >= 4 else "log another meaningful customer interaction")
    return {"score": probability, "band": band, "reasons": reasons[:3], "model": "random_forest" if MODEL is not None else "rule_fallback"}

@app.post("/ai/summary")
def summary(payload: SummaryInput):
    risk = "High" if payload.overdue_followups > 0 or payload.days_silent >= 21 or (payload.open_opportunities > 0 and payload.ai_score < 40) else "Medium" if payload.days_silent >= 8 or payload.ai_score < 60 else "Low"
    alias = payload.company_alias or "This account"
    amount = f"₹{payload.pipeline_value:,.0f}" if payload.pipeline_value else "no tracked pipeline"
    if payload.days_silent >= 21:
        first = f"{alias} has been quiet for {payload.days_silent} days with {amount} in open pipeline."
        action = "Call the decision-maker today and confirm the next milestone."
    elif payload.overdue_followups:
        first = f"{alias} has {payload.overdue_followups} overdue follow-up(s) and {amount} in open pipeline."
        action = "Resolve the overdue follow-up and update the CRM outcome."
    elif payload.ai_score and payload.ai_score >= 70:
        first = f"{alias} is a {payload.status.lower()} lead with a strong {payload.ai_score:.0f}/100 conversion score."
        action = "Prioritise a tailored demo or proposal while engagement is high."
    else:
        first = f"{alias} has {amount} in open pipeline; recent activity sentiment is {payload.recent_sentiment.lower()}."
        action = "Confirm the buyer's main blocker and agree a dated next step."
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            import httpx, json
            prompt = ("Write a CRM executive summary in strict JSON with keys summary (max two short sentences), risk_level (Low/Medium/High), sentiment, next_action. "
                      "Do not invent facts. Input is pseudonymized and contains no direct contact details. Input: " + payload.model_dump_json())
            r = httpx.post("https://api.anthropic.com/v1/messages", timeout=3.0, headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 220, "messages": [{"role": "user", "content": prompt}]})
            if r.is_success:
                txt = r.json()["content"][0]["text"]
                start, end = txt.find("{"), txt.rfind("}")
                if start >= 0 and end > start:
                    result = json.loads(txt[start:end+1])
                    if result.get("risk_level") in ("Low", "Medium", "High") and result.get("next_action"):
                        return {**result, "source": "llm"}
        except Exception:
            pass
    return {"summary": first + f" Risk: {risk}.", "risk_level": risk, "sentiment": payload.recent_sentiment, "next_action": action, "source": "template_fallback"}

@app.post("/ai/assign")
def assign(payload: AssignInput):
    viable = [c for c in payload.candidates if c.open_workload < c.capacity]
    if not viable:
        return {"user_id": None, "reason": "All sales executives are at configured capacity."}
    chosen = min(viable, key=lambda c: (c.open_workload * 1.0 - min(c.industry_wins, 4) * 0.75, c.user_id))
    reason = f"{chosen.open_workload} open items"
    if chosen.industry_wins:
        reason += f" and {chosen.industry_wins} prior wins in {payload.industry}"
    else:
        reason += "; lowest workload among available executives"
    return {"user_id": chosen.user_id, "reason": reason}
