"""
Offline tests for the SDUI section path (app/sdui_sections.py). Fixtures are
hand-built flight payloads in the exact shape LinkedIn returns (verified against
a live capture on 2026-08-30); no network, no real profile data.
"""
import asyncio
import json

from app import sdui_sections as S


def _flight(rows: dict) -> str:
    return "\n".join(f"{k}:{json.dumps(v)}" for k, v in rows.items())


def _p(text):  # a <p> element carrying one visible string
    return ["$", "p", None, {"className": "x", "children": [text]}]


def _item(*lines):
    return {"key": "entity-collection-item-1", "item": ["$", "div", None, {"children": [_p(t) for t in lines]}]}


def _card(component, heading, items, extra_children=()):
    return ["$", "div", None, {
        "data-sdui-component": f"com.linkedin.sdui.generated.profile.dsl.impl.{component}",
        "children": [
            ["$", "$L4", None, {"componentKey": f"ProfileNullStateCardAnchor_{heading}",
                                "children": ["$", "$Lf", None, {"textProps": {"tagName": "h2", "children": [heading]}}]}],
            ["$", "$L10", None, {"useCollectionKey": True, "initialItems": list(items)}],
            *extra_children,
        ],
    }]


EXPERIENCE_FLIGHT = _flight({
    "1": None,
    "0": _card("profileCardsExperienceOnly", "Experience", [
        _item("Security Engineer Intern", "Acme · Internship", "Jan 2026 - Jul 2026 · 7 mos",
              "Bengaluru, Karnataka, India · On-site", "Built things.", "More things.",
              "Python, Go and +3 skills"),
        # grouped multi-role company
        _item("Club X", "Part-time · 3 yrs 8 mos",
              "Vice President", "Full-time", "Feb 2025 - Present · 7 mos", "New Delhi, Delhi, India", "Led stuff.",
              "Member", "Nov 2022 - Aug 2023 · 10 mos"),
        # media caption should be dropped
        _item("Project Intern", "BigCo · Internship", "Jun 2025 - Aug 2025 · 3 mos", "Did work.", "certificate of completion"),
    ], extra_children=[["$", "$L79", None, {"a11yText": "Thumbnail for certificate of completion", "imageId": 1}]]),
})

EDU_FLIGHT = _flight({
    "0": ["$", "div", None, {
        "data-sdui-component": "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsBelowActivityPart1WithoutExp",
        "children": [
            _card("ignored", "Education", [
                _item("Some University", "Bachelor's degree, Computer Science", "Nov 2022 – May 2026"),
                _item("Some School", "Aug 2020 – Jul 2022", "Grade: 96.2%"),
            ]),
            _card("ignored", "Certifications", [_item("Cert A", "Issuer A", "Issued Jul 2025"), _item("Cert B", "Issuer B")]),
            _card("ignored", "Skills", [_item("Code Review"), _item("Test Coverage")]),
            _card("ignored", "Languages", [_item("English", "Native or bilingual proficiency")]),
            _card("ignored", "VolunteerExperience", [_item("Fellow", "Some Org", "Aug 2025 - Jan 2026 · 6 mos", "Social Services")]),
        ],
    }]
})


def test_flight_to_sections_groups_items_under_headings_and_drops_media():
    secs = S.flight_to_sections(EXPERIENCE_FLIGHT)
    assert list(secs) == ["Experience"]
    assert len(secs["Experience"]) == 3
    assert "certificate of completion" not in secs["Experience"][2]


def test_parse_experience_single_and_grouped_roles():
    exp = S.cards_to_sections({"profileCardsExperienceOnly": EXPERIENCE_FLIGHT})["experience"]
    assert [e.title for e in exp] == ["Security Engineer Intern", "Vice President", "Member", "Project Intern"]
    first = exp[0]
    assert (first.company_name, first.employment_type) == ("Acme", "Internship")
    assert (first.start_date, first.end_date, first.location) == ("Jan 2026", "Jul 2026", "Bengaluru, Karnataka, India")
    assert first.description == "Built things. More things."  # skills line dropped
    vp, member = exp[1], exp[2]
    assert vp.company_name == member.company_name == "Club X"
    assert (vp.start_date, vp.end_date, vp.employment_type, vp.location) == ("Feb 2025", "Present", "Full-time", "New Delhi, Delhi, India")
    assert vp.description == "Led stuff."
    assert (member.start_date, member.end_date, member.employment_type) == ("Nov 2022", "Aug 2023", "Part-time")
    assert exp[3].description == "Did work."


def test_parse_education_certs_skills_languages():
    res = S.cards_to_sections({"profileCardsBelowActivityPart1WithoutExp": EDU_FLIGHT})
    uni, school = res["education"]
    assert (uni.institution, uni.degree, uni.field_of_study, uni.start_year, uni.end_year) == \
        ("Some University", "Bachelor's degree", "Computer Science", 2022, 2026)
    assert (school.degree, school.start_year, school.end_year) == (None, 2020, 2022)
    assert res["certifications"] == ["Cert A", "Cert B"]
    assert res["skills"] == ["Code Review", "Test Coverage"]
    assert res["languages"] == ["English"]
    (vol,) = res["volunteering"]
    assert (vol.title, vol.company_name, vol.start_date, vol.end_date) == ("Fellow", "Some Org", "Aug 2025", "Jan 2026")


def test_malformed_card_yields_empty_sections_not_error():
    res = S.cards_to_sections({"profileCardsBelowActivityPart2": "garbage", "x": "0:[1,2"})
    assert res == {"experience": [], "education": [], "skills": [], "certifications": [], "languages": [], "volunteering": []}


REHYDRATION_HTML = (
    '<html><script id="rehydrate-data">window.__como_rehydration__ = '
    + json.dumps([
        '0:' + json.dumps({"$type": "proto.sdui.actions.core.ReplaceComponent", "value": {"content": {"$case": "asyncContent", "asyncContent": {
            "$type": "proto.sdui.actions.core.AsyncComponentRequest",
            "newComponentId": "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly",
            "requestedArguments": {"$type": "proto.sdui.actions.requests.RequestedArguments", "requestedStateKeys": [],
                                   "payload": {"vanityName": "someone", "isSelfView": False},
                                   "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"}}}}}}) + "\n"
        + '1:' + json.dumps({"$type": "proto.sdui.actions.core.AsyncComponentRequest",
                              "newComponentId": "com.linkedin.sdui.generated.profile.dsl.impl.pymkRecommendedEntitySection",
                              "requestedArguments": {"payload": {}}}),
        '2:{"pageKey":"d_flagship3_profile_view_base","pageInstance":{"pageUrn":"urn:li:page:d_flagship3_profile_view_base","trackingId":"abc=="}}',
    ])
    + ";</script></html>"
)


def test_extract_component_requests_only_section_cards_and_reshapes_body():
    reqs = S.extract_component_requests(REHYDRATION_HTML)
    assert list(reqs) == ["profileCardsExperienceOnly"]
    body = S.build_client_arguments(reqs["profileCardsExperienceOnly"])
    ca = body["clientArguments"]
    assert ca["payload"] == {"vanityName": "someone", "isSelfView": False}
    assert ca["states"] == [] and ca["screenId"] == "" and ca["knownTemplateIds"] == []
    assert "$type" not in ca and "requestedStateKeys" not in ca
    assert S.extract_page_instance(REHYDRATION_HTML) == "urn:li:page:d_flagship3_profile_view_base;abc=="


def test_extract_on_non_sdui_page_is_empty():
    assert S.extract_component_requests("<html>login wall</html>") == {}


def test_fetch_section_cards_replays_requests(monkeypatch):
    import httpx
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=EXPERIENCE_FLIGHT)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "proxy"}))
    monkeypatch.setattr(S, "_INTER_REQUEST_DELAY_S", 0)
    cards = asyncio.run(S.fetch_section_cards("someone", REHYDRATION_HTML, cookie_header='li_at=x; JSESSIONID="ajax:1"',
                                              csrf_token="ajax:1", user_agent="UA", li_track="{}"))
    assert list(cards) == ["profileCardsExperienceOnly"]
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/flagship-web/rsc-action/actions/component"
    assert req.url.params["componentId"] == "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly"
    assert req.headers["csrf-token"] == "ajax:1" and req.headers["x-li-rsc-stream"] == "true"
    assert json.loads(req.content)["clientArguments"]["payload"]["vanityName"] == "someone"
