"""
Gate tests for Phase 0 (debug routes off by default) and Phase 3 (survivability:
/health session status + response cache). All no-network via monkeypatch.
"""
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.config import settings
from app.schemas import ProfileResponse

client = TestClient(app)


# --- Phase 0 gate ---------------------------------------------------------- #
def test_debug_routes_disabled_by_default():
    """With ENABLE_DEBUG_ROUTES off (default), debug routes are not registered."""
    assert client.get("/api/v1/debug/auth").status_code == 404
    assert client.get("/api/v1/debug/probe?profile_url=x").status_code == 404


# --- Phase 3 gate: /health session status ---------------------------------- #
def test_health_reports_expired_with_broken_cookies(monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_COOKIE", 'li_at=broken; JSESSIONID="ajax:1"')

    async def fake_check_auth():
        return {"authenticated": False, "status_code": 999, "body_preview": "SECRET"}

    monkeypatch.setattr(main, "check_auth", fake_check_auth)

    body = client.get("/health").json()
    assert body["linkedin_session"] == "expired"
    # must not leak cookie material or upstream body
    assert "SECRET" not in str(body)
    assert "cookie" not in str(body).lower()


def test_health_ok_and_not_configured(monkeypatch):
    async def ok_auth():
        return {"authenticated": True, "status_code": 200}

    monkeypatch.setattr(settings, "LINKEDIN_COOKIE", "li_at=x")
    monkeypatch.setattr(main, "check_auth", ok_auth)
    assert client.get("/health").json()["linkedin_session"] == "ok"

    monkeypatch.setattr(settings, "LINKEDIN_COOKIE", "")
    monkeypatch.setattr(settings, "LINKEDIN_LI_AT_COOKIE", "")
    monkeypatch.setattr(settings, "LINKEDIN_JSESSIONID", "")
    assert client.get("/health").json()["linkedin_session"] == "not_configured"


# --- Phase 3 gate: cache serves repeats ------------------------------------ #
def test_repeat_request_served_from_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(url, **kwargs):
        calls["n"] += 1
        return ProfileResponse(profile_url=url, profile_handle="foo", full_name="Foo Bar")

    monkeypatch.setattr(main, "fetch_linkedin_profile_voyager", fake_fetch)
    main._cache.clear()
    main._hits.clear()

    body = {"profile_url": "https://www.linkedin.com/in/foo/"}
    r1 = client.post("/api/v1/parse-profile", json=body)
    r2 = client.post("/api/v1/parse-profile", json=body)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["meta"] is None or r2.json()["meta"]["cached"] is True
    assert r1.json()["full_name"] == "Foo Bar"
    assert calls["n"] == 1  # second request served from cache, no second fetch


def test_rate_limit_returns_429(monkeypatch):
    async def fake_fetch(url, **kwargs):
        return ProfileResponse(profile_url=url, profile_handle="foo", full_name="Foo")

    monkeypatch.setattr(main, "fetch_linkedin_profile_voyager", fake_fetch)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 3)
    main._cache.clear()
    main._hits.clear()

    # Distinct handles to bypass the cache and actually exercise the limiter.
    codes = [
        client.post("/api/v1/parse-profile",
                    json={"profile_url": f"https://www.linkedin.com/in/user{i}/"}).status_code
        for i in range(5)
    ]
    assert 429 in codes
