import re

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services.staff_auth import create_or_update_staff_user

from .conftest import STAFF_TEST_PASSWORD


def _login_page_csrf(client) -> str:
    resp = client.get("/admin/login")
    return re.search(r'name="csrf_token" value="([^"]+)"', resp.text).group(1)


def test_unauthenticated_request_redirects_to_login(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        resp = client.get("/admin/")
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/admin/login")
    finally:
        app.dependency_overrides.clear()


def test_login_success_sets_session_and_redirects(db, staff_user, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        csrf_token = _login_page_csrf(client)
        resp = client.post(
            "/admin/login",
            data={"csrf_token": csrf_token, "email": staff_user.email, "password": STAFF_TEST_PASSWORD, "next": "/admin/"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/"

        dashboard = client.get("/admin/")
        assert dashboard.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_login_wrong_password_rejected(db, staff_user):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        csrf_token = _login_page_csrf(client)
        resp = client.post(
            "/admin/login",
            data={"csrf_token": csrf_token, "email": staff_user.email, "password": "totally-wrong", "next": "/admin/"},
        )
        assert resp.status_code == 401
        assert "Invalid email or password" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_login_unknown_email_rejected(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        csrf_token = _login_page_csrf(client)
        resp = client.post(
            "/admin/login",
            data={"csrf_token": csrf_token, "email": "nobody@example.com", "password": "whatever123", "next": "/admin/"},
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_inactive_staff_cannot_log_in(db):
    from app.services.staff_auth import deactivate_staff_user

    create_or_update_staff_user(db, email="inactive@example.com", name="Inactive", password=STAFF_TEST_PASSWORD)
    deactivate_staff_user(db, email="inactive@example.com")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, follow_redirects=False)
        csrf_token = _login_page_csrf(client)
        resp = client.post(
            "/admin/login",
            data={"csrf_token": csrf_token, "email": "inactive@example.com", "password": STAFF_TEST_PASSWORD, "next": "/admin/"},
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_logout_clears_session(admin_client):
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', admin_client.get("/admin/").text).group(1)
    resp = admin_client.post("/admin/logout", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303

    after_logout = admin_client.get("/admin/", follow_redirects=False)
    assert after_logout.status_code == 303
    assert after_logout.headers["location"].startswith("/admin/login")
