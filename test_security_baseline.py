from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_is_available():
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "VAJRA AI OS"
    assert "database_connected" in body


def test_projects_require_authentication():
    client = TestClient(app)
    response = client.get("/api/projects")

    assert response.status_code == 401


def test_tasks_require_authentication():
    client = TestClient(app)
    response = client.get("/api/tasks")

    assert response.status_code == 401
