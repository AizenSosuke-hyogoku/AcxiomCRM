import os
from fastapi.testclient import TestClient
from ai_service.main import app as ai_app


def test_ai_health_and_score(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(ai_app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        result = client.post("/ai/score", json={
            "interaction_count": 7,
            "expected_value": 450000,
            "industry": "Fintech",
            "status": "Qualified",
            "days_since_last_contact": 3,
            "source": "Referral",
        })
        assert result.status_code == 200
        body = result.json()
        assert 0 <= body["score"] <= 100
        assert body["band"] in {"Hot", "Warm", "Cold"}
        assert len(body["reasons"]) == 3


def test_ai_summary_uses_clear_risk_and_action(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(ai_app) as client:
        response = client.post("/ai/summary", json={
            "entity_type": "customer", "company_alias": "Account 14",
            "days_silent": 29, "overdue_followups": 1,
            "open_opportunities": 2, "pipeline_value": 420000,
            "ai_score": 35, "status": "Qualified", "recent_sentiment": "Negative",
        })
        assert response.status_code == 200
        result = response.json()
        assert result["risk_level"] == "High"
        assert result["next_action"]
        assert "Account 14" in result["summary"]
        assert "test@example.com" not in result["summary"]


def test_assignment_respects_capacity():
    with TestClient(ai_app) as client:
        response = client.post("/ai/assign", json={
            "industry": "Healthcare",
            "candidates": [
                {"user_id": 1, "name": "Rep A", "open_workload": 12, "capacity": 12},
                {"user_id": 2, "name": "Rep B", "open_workload": 3, "industry_wins": 2, "capacity": 10},
            ],
        })
        assert response.status_code == 200
        assert response.json()["user_id"] == 2
        assert "open items" in response.json()["reason"]
