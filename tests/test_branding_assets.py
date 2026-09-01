"""Brand icon + PWA manifest are served at the conventional root paths so
every page (staff app and public) shows the Meantime mark, and 'Install as
app' on the staff app picks up the manifest and its icons."""

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_favicon_served_at_root():
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO magic
    assert "max-age" in r.headers.get("cache-control", "")


def test_apple_touch_icon_served_at_root_and_precomposed():
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n", path


def test_manifest_is_valid_and_points_at_real_icons():
    r = client.get("/site.webmanifest")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers["content-type"]
    data = json.loads(r.content)
    assert data["name"] == "Meantime Concierge"
    assert data["display"] == "standalone"
    for icon in data["icons"]:
        got = client.get(icon["src"])
        assert got.status_code == 200, icon["src"]
        assert got.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_staff_base_links_the_manifest_and_icon(admin_client):
    # a page that extends admin/_base.html (the installable staff app)
    r = admin_client.get("/admin/bookings")
    assert r.status_code == 200
    assert 'rel="manifest"' in r.text
    assert "/favicon.ico" in r.text
    assert 'name="theme-color"' in r.text
