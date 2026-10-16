"""Small shared pseudonymisation helpers for outbound AI payloads."""
from __future__ import annotations
import hashlib, hmac, os, re

_SALT = os.getenv("PSEUDONYM_SALT", "local-demo-pseudonym-salt-change-before-deploy")

def pseudonym_alias(entity_type: str, record_id: int) -> str:
    """Return a stable one-way alias; never send names, emails or phones as an identifier."""
    message = f"{entity_type}:{record_id}".encode("utf-8")
    digest = hmac.new(_SALT.encode("utf-8"), message, hashlib.sha256).hexdigest()[:8].upper()
    return f"Account {digest}"

def mask_email(value: str) -> str:
    if "@" not in (value or ""):
        return ""
    local, domain = value.split("@", 1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain

def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return "+91 ******" + digits[-4:] if len(digits) >= 4 else ""

def outbound_summary_signals(*, entity_type: str, record_id: int, days_silent: int, overdue_followups: int,
                             open_opportunities: int, pipeline_value: float, ai_score: float,
                             status: str, sentiment: str) -> dict:
    """Drop free text and direct contact fields; only return minimal numeric/status signals."""
    return {
        "entity_type": entity_type,
        "company_alias": pseudonym_alias(entity_type, record_id),
        "days_silent": max(0, int(days_silent)),
        "overdue_followups": max(0, int(overdue_followups)),
        "open_opportunities": max(0, int(open_opportunities)),
        "pipeline_value": max(0.0, float(pipeline_value)),
        "ai_score": max(0.0, min(100.0, float(ai_score))),
        "status": str(status)[:30],
        "recent_sentiment": str(sentiment)[:20],
    }
