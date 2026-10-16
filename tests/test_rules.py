from app.main import valid_email, valid_phone, valid_password, fallback_score


def test_email_validation():
    assert valid_email("person@example.com")
    assert not valid_email("person-at-example")
    assert not valid_email("person @example.com")


def test_indian_mobile_validation():
    assert valid_phone("9876543210")
    assert not valid_phone("5876543210")
    assert not valid_phone("98765-123")


def test_password_policy():
    assert valid_password("DemoAccess@2026")
    assert not valid_password("short")
    assert not valid_password("demopassword2026")


def test_score_is_bounded_and_explainable():
    lead = {"interaction_count": 6, "expected_value": 800000, "industry": "Fintech", "status": "Qualified", "last_contact_date": None, "source": "Referral"}
    result = fallback_score(lead)
    assert 0 <= result["score"] <= 100
    assert result["band"] in {"Hot", "Warm", "Cold"}
    assert len(result["reasons"]) == 3
