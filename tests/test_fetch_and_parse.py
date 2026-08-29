"""
Correctness tests (no network, via httpx MockTransport + monkeypatch):
 - classic vs dash produce identical parsed output
 - dash dates survive (dateRange/start/end), ongoing role -> "Present"
 - URN geoLocation resolves
 - empty/soft-block 200 -> honest 502 (not a hollow profile)
 - HTML-200 challenge and relative-302 don't crash
"""
import asyncio

import httpx
import pytest

from app import linkedin_service as ls
from app.config import settings
from app.schemas import ProfileResponse


# --------------------------------------------------------------------------- #
# Parser: classic vs dash equivalence + dash date survival
# --------------------------------------------------------------------------- #
def _classic_payload():
    return {"included": [
        {"$type": "com.linkedin.voyager.identity.profile.Profile",
         "firstName": "Diya", "lastName": "Singh", "headline": "SWE",
         "summary": "hi", "locationName": "Delhi, India"},
        {"$type": "com.linkedin.voyager.identity.profile.Position",
         "title": "Engineer", "companyName": "Acme",
         "timePeriod": {"startDate": {"year": 2022, "month": 1},
                        "endDate": {"year": 2024, "month": 6}}},
        {"$type": "com.linkedin.voyager.identity.profile.Education",
         "schoolName": "IIT", "degreeName": "BTech", "fieldOfStudy": "CS",
         "timePeriod": {"startDate": {"year": 2018}, "endDate": {"year": 2022}}},
    ]}


def _dash_payload():
    return {"included": [
        {"$type": "com.linkedin.voyager.dash.identity.profile.Profile",
         "entityUrn": "urn:li:fsd_profile:X",
         "firstName": "Diya", "lastName": "Singh", "headline": "SWE",
         "summary": "hi", "geoLocation": "urn:li:fsd_geo:100"},
        {"$type": "com.linkedin.voyager.dash.identity.profile.geo.Geo",  # resolved by URN
         "entityUrn": "urn:li:fsd_geo:100", "defaultLocalizedName": "Delhi, India"},
        {"$type": "com.linkedin.voyager.dash.identity.profile.Position",
         "title": "Engineer", "company": "urn:li:fsd_company:1",
         "dateRange": {"start": {"year": 2022, "month": 1},
                       "end": {"year": 2024, "month": 6}}},
        {"$type": "com.linkedin.voyager.dash.organization.Company",
         "entityUrn": "urn:li:fsd_company:1", "name": "Acme"},
        {"$type": "com.linkedin.voyager.dash.identity.profile.Education",
         "entityUrn": "urn:li:fsd_edu:1", "schoolName": "IIT",
         "degreeName": "BTech", "fieldOfStudy": "CS",
         "dateRange": {"start": {"year": 2018}, "end": {"year": 2022}}},
    ]}


def test_classic_and_dash_produce_identical_output():
    c = ls.parse_voyager_json("u", "diya", _classic_payload()).model_dump()
    d = ls.parse_voyager_json("u", "diya", _dash_payload()).model_dump()
    for field in ("full_name", "headline", "about", "experience", "education"):
        assert c[field] == d[field], f"{field} differs: {c[field]} != {d[field]}"
    assert c["location"]["raw_location"] == d["location"]["raw_location"] == "Delhi, India"


def test_dash_dates_survive_and_not_all_present():
    exp = ls.parse_voyager_json("u", "diya", _dash_payload()).experience[0]
    assert exp.start_date == "2022-01"
    assert exp.end_date == "2024-06"        # ended job must NOT say "Present"


def test_ongoing_role_reports_present():
    payload = {"included": [
        {"$type": "com.linkedin.voyager.dash.identity.profile.Position",
         "title": "Intern", "company": "urn:li:fsd_company:1",
         "dateRange": {"start": {"year": 2026, "month": 1}}},  # no end
        {"$type": "com.linkedin.voyager.dash.organization.Company",
         "entityUrn": "urn:li:fsd_company:1", "name": "Rippling"},
    ]}
    exp = ls.parse_voyager_json("u", "diya", payload).experience[0]
    assert exp.start_date == "2026-01" and exp.end_date == "Present"


def test_picks_target_profile_not_viewer():
    """Response contains the viewer's own profile alongside the target's — must
    pick the one whose publicIdentifier matches the requested handle."""
    payload = {"included": [
        {"$type": "com.linkedin.voyager.dash.identity.profile.Profile",
         "entityUrn": "urn:li:fsd_profile:VIEWER", "firstName": "x", "lastName": "x",
         "headline": "Student at x", "publicIdentifier": "some-viewer-123"},
        {"$type": "com.linkedin.voyager.dash.identity.profile.Profile",
         "entityUrn": "urn:li:fsd_profile:BILL", "firstName": "Bill", "lastName": "Gates",
         "headline": "Co-chair, Gates Foundation", "publicIdentifier": "williamhgates"},
    ]}
    r = ls.parse_voyager_json("u", "williamhgates", payload)
    assert r.full_name == "Bill Gates"
    assert r.headline == "Co-chair, Gates Foundation"


def test_empty_payload_returns_nulls_not_errors():
    r = ls.parse_voyager_json("u", "diya", {"included": []})
    assert r.full_name == "diya" and r.location is None and r.experience == []


# --------------------------------------------------------------------------- #
# fetch_profile_payload: usable-guard + failure modes (no network)
# --------------------------------------------------------------------------- #
def _client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _run(coro):
    return asyncio.run(coro)


def test_soft_block_empty_200_raises_502_not_hollow(monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_PROFILE_QUERY_ID", "voyagerIdentityDashProfiles.test")

    def handler(request):  # every API call returns an empty-included 200 (soft block)
        return httpx.Response(200, json={"data": {}, "included": []},
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(ls, "make_voyager_client", lambda follow=False: _client_with(handler))
    # HTML strategy also empty
    async def empty_html(handle):
        return httpx.Response(200, text="<html></html>")
    monkeypatch.setattr(ls, "_fetch_profile_html", empty_html)

    with pytest.raises(ls.HTTPException) as ei:
        _run(ls.fetch_profile_payload("diya"))
    assert ei.value.status_code == 502  # honest failure, not a hollow 200


def test_html_200_challenge_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_PROFILE_QUERY_ID", "")

    def handler(request):  # profileView returns an HTML challenge with a 200
        return httpx.Response(200, text="<html>login</html>",
                              headers={"content-type": "text/html"})

    monkeypatch.setattr(ls, "make_voyager_client", lambda follow=False: _client_with(handler))
    async def empty_html(handle):
        return httpx.Response(200, text="<html></html>")
    monkeypatch.setattr(ls, "_fetch_profile_html", empty_html)

    with pytest.raises(ls.HTTPException) as ei:  # _json_or_none avoids a 500
        _run(ls.fetch_profile_payload("diya"))
    assert ei.value.status_code == 502


def test_relative_redirect_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_PROFILE_QUERY_ID", "voyagerIdentityDashProfiles.test")
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        if hits["n"] == 1:  # relative Location — the historic crasher
            return httpx.Response(302, headers={"location": "/uas/login?redirect=1"})
        return httpx.Response(200, json={"included": [
            {"$type": "com.linkedin.voyager.dash.identity.profile.Profile",
             "firstName": "Diya", "lastName": "Singh"}]},
            headers={"content-type": "application/json"})

    monkeypatch.setattr(ls, "make_voyager_client", lambda follow=False: _client_with(handler))
    data = _run(ls.fetch_profile_payload("diya"))
    assert ls.parse_voyager_json("u", "diya", data).full_name == "Diya Singh"
