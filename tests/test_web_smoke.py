from fastapi.testclient import TestClient
from app.main import app


def test_login_page_and_protected_api():
    with TestClient(app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert "WELCOME BACK" in page.text
        assert client.get("/api/customers").status_code == 401


def test_seeded_dashboard_and_customer_scope():
    with TestClient(app) as client:
        csrf = client.get("/login").text
        # Token is held in the signed session cookie; pull it from the HTML's hidden input.
        import re
        token = re.search(r'name="csrf_token" value="([^"]+)"', csrf).group(1)
        resp = client.post("/login", data={"csrf_token": token, "email": "admin@acxiomcrm.demo", "password": "Admin@12345!"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard"
        dash = client.get("/dashboard")
        assert dash.status_code == 200
        assert "Executive daily brief" in dash.text
        assert "Monthly sales" in dash.text
        customers = client.get("/api/customers")
        assert customers.status_code == 200
        assert customers.json()["count"] >= 40
        assert "password_hash" not in str(customers.json())


def test_invalid_customer_is_rejected_server_side():
    with TestClient(app) as client:
        import re
        page = client.get("/login").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        client.post("/login", data={"csrf_token": token, "email": "admin@acxiomcrm.demo", "password": "Admin@12345!"}, follow_redirects=False)
        customers = client.get("/customers")
        token2 = re.search(r'name="csrf_token" value="([^"]+)"', customers.text).group(1)
        response = client.post("/customers/save", data={"csrf_token": token2, "name": "Invalid Person", "email": "not-an-email", "phone": "123", "company": "Bad Co", "industry": "Technology", "city": "Pune", "state": "Maharashtra", "status": "Active", "assigned_to": "3"}, follow_redirects=False)
        assert response.status_code == 303
        assert "valid%20email" in response.headers["location"]
