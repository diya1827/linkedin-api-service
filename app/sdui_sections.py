"""
Profile SECTIONS (Experience / Education / Skills / Certifications / Languages)
via LinkedIn's SDUI lazy-card endpoint. No browser engine involved.

How the current profile page works (verified 2026-08-30 against a real session):

  * The profile document is a server-driven-UI (SDUI) page. Server-side it
    renders only the intro region. Every section card below it is an EMPTY
    placeholder <div id="profileCardsExperienceOnly<handle>"> etc.
  * The page's hydration blob (`window.__como_rehydration__`, a JSON array of
    React-Server-Components "flight" chunks) carries, for every placeholder, a
    `proto.sdui.actions.core.AsyncComponentRequest` describing exactly how the
    browser should fill it: a `newComponentId` plus `requestedArguments`.
  * The browser runtime POSTs that to
        /flagship-web/rsc-action/actions/component?componentId=<id>&sduiid=<id>
    with body {"clientArguments": {payload, states, requestMetadata, screenId,
    knownTemplateIds}} and gets the card back as another flight payload.

None of that needs JavaScript or a scroll: we lift the requests out of the
HTML and replay them ourselves. Visible text sits as plain strings inside the
React element `children` arrays, so parsing is a depth-first walk that emits
(heading, item-start, text, item-end) events, then per-section line rules.

Everything here is best-effort: any failure returns empty sections, never
raises, so the intro card path is unaffected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from app.config import settings
from app.schemas import EducationItem, ExperienceItem

logger = logging.getLogger(__name__)

COMPONENT_ENDPOINT = "https://www.linkedin.com/flagship-web/rsc-action/actions/component"
_COMPONENT_PREFIX = "com.linkedin.sdui.generated.profile.dsl.impl."
# Only the cards that hold profile sections. Aside/recommendation cards are skipped.
_SECTION_CARD_RE = re.compile(r"^profileCards(ExperienceOnly|BelowActivityPart\d+(WithoutExp)?)$")
# Polite spacing between the per-card POSTs (one profile = up to ~8 calls).
_INTER_REQUEST_DELAY_S = 0.4

_REHYDRATION_RE = re.compile(
    r"window\.__como_rehydration__\s*=\s*(\[.*?\])\s*;?\s*</script>", re.DOTALL
)
_PAGE_INSTANCE_RE = re.compile(
    r'"pageUrn\\?":\\?"(urn:li:page:d_flagship3_profile_view_base)\\?",\\?"trackingId\\?":\\?"([^"\\]+)'
)

# Keys whose subtrees never carry visible text (actions, tracking, styling,
# nested request definitions). Skipping them keeps navigation titles such as
# "Skills for X at Y" out of the text stream.
_SKIP_KEYS = frozenset({
    "action", "actions", "onClick", "viewTrackingSpecs", "renderPayload", "style",
    "className", "onShowLessAction", "onShowMoreAction", "trackingSpecs",
    "requestedArguments", "newHierarchy", "onClientRequestFailureAction",
    "onVisibleAction", "onInvisibleAction",
})


# --------------------------------------------------------------------------- #
# 1. Lift the AsyncComponentRequests out of the profile HTML
# --------------------------------------------------------------------------- #
def parse_flight(text: str) -> dict[str, Any]:
    """Parse RSC flight text ("<hex>:<json>\\n" per row) into {row_id: value}.
    Module rows (`I[...]`) and hint rows are skipped."""
    rows: dict[str, Any] = {}
    for line in text.split("\n"):
        m = re.match(r"^([0-9a-f]+):(.*)$", line)
        if not m:
            continue
        key, value = m.groups()
        if value[:1] in ("I", "H"):  # module import / hint rows
            continue
        try:
            rows[key] = json.loads(value)
        except ValueError:
            continue
    return rows


def _iter_dicts(node: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 120:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v, depth + 1)


def extract_component_requests(html_text: str) -> dict[str, dict]:
    """
    Return {short_card_name: requestedArguments} for every section card the
    page would lazy-load, e.g. {"profileCardsExperienceOnly": {...}}.
    Empty dict if the page is not an SDUI profile (login wall, challenge, ...).
    """
    m = _REHYDRATION_RE.search(html_text)
    if not m:
        return {}
    try:
        chunks = json.loads(m.group(1))
    except ValueError:
        return {}
    if not isinstance(chunks, list):
        return {}
    rows = parse_flight("".join(c for c in chunks if isinstance(c, str)))

    found: dict[str, dict] = {}
    for row in rows.values():
        for d in _iter_dicts(row):
            if d.get("$type") != "proto.sdui.actions.core.AsyncComponentRequest":
                continue
            cid = d.get("newComponentId") or ""
            if not cid.startswith(_COMPONENT_PREFIX):
                continue
            short = cid[len(_COMPONENT_PREFIX):]
            if _SECTION_CARD_RE.match(short) and short not in found:
                found[short] = d.get("requestedArguments") or {}
    return found


def extract_page_instance(html_text: str) -> str:
    """The x-li-page-instance header the runtime sends; falls back to a dummy id."""
    m = _PAGE_INSTANCE_RE.search(html_text)
    if m:
        return f"{m.group(1)};{m.group(2)}"
    return "urn:li:page:d_flagship3_profile_view_base;dummy"


def build_client_arguments(requested_arguments: dict) -> dict:
    """Mirror the runtime's `sr()` reshaping: drop $type/requestedStateKeys and
    add the empty screenId/knownTemplateIds/states the server expects. Sending
    the raw RequestedArguments object back returns HTTP 500."""
    return {
        "clientArguments": {
            "payload": requested_arguments.get("payload"),
            "states": [],
            "requestMetadata": requested_arguments.get("requestMetadata")
            or {"$type": "proto.sdui.common.RequestMetadata"},
            "screenId": "",
            "knownTemplateIds": [],
        }
    }


# --------------------------------------------------------------------------- #
# 2. Replay them
# --------------------------------------------------------------------------- #
def _component_headers(csrf_token: str, page_instance: str, handle: str, user_agent: str, li_track: str) -> dict:
    return {
        "User-Agent": user_agent,
        "Accept": "text/x-component",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "csrf-token": csrf_token,
        "x-li-rsc-stream": "true",
        "x-li-anchor-page-key": "d_flagship3_profile_view_base",
        "x-li-page-instance": page_instance,
        "x-li-track": li_track,
        "Origin": "https://www.linkedin.com",
        "Referer": f"https://www.linkedin.com/in/{quote(handle)}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def fetch_section_cards(
    handle: str,
    html_text: str,
    *,
    cookie_header: str,
    csrf_token: str,
    user_agent: str,
    li_track: str,
    proxy: str | None = None,
) -> dict[str, str]:
    """
    POST each section card's AsyncComponentRequest and return
    {short_card_name: flight_text}. Cards that fail are simply omitted.
    """
    requests_by_card = extract_component_requests(html_text)
    if not requests_by_card:
        logger.warning("SDUI sections: no AsyncComponentRequest found in profile HTML")
        return {}

    headers = _component_headers(
        csrf_token, extract_page_instance(html_text), handle, user_agent, li_track
    )
    headers["Cookie"] = cookie_header
    out: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT, proxy=proxy) as client:
        for i, (card, req_args) in enumerate(requests_by_card.items()):
            if i:
                await asyncio.sleep(_INTER_REQUEST_DELAY_S)
            cid = _COMPONENT_PREFIX + card
            try:
                resp = await client.post(
                    COMPONENT_ENDPOINT,
                    params={"componentId": cid, "sduiid": cid},
                    headers=headers,
                    content=json.dumps(build_client_arguments(req_args)),
                )
            except httpx.HTTPError as exc:
                logger.warning("SDUI card %s failed: %s", card, exc)
                continue
            logger.info("SDUI card %s -> %s (%d bytes)", card, resp.status_code, len(resp.content))
            if resp.status_code == 200 and resp.text.lstrip()[:1].isalnum():
                out[card] = resp.text
            elif resp.status_code in (999, 302, 403):
                # Bot flag / session bounce: stop immediately, do not hammer.
                logger.warning("SDUI card fetch blocked with %s; aborting remaining cards", resp.status_code)
                break
    return out


# --------------------------------------------------------------------------- #
# 3. Flight payload -> event stream -> sections
# --------------------------------------------------------------------------- #
def _walk(node: Any, rows: dict, ev: list, depth: int = 0, in_children: bool = False) -> None:
    if depth > 120:
        return
    if isinstance(node, str):
        if node.startswith("$L") or node.startswith("$@"):
            ref = rows.get(node[2:])
            if ref is not None:
                _walk(ref, rows, ev, depth + 1, in_children)
        elif node.startswith("$"):
            return
        elif in_children and node.strip():
            ev.append(("text", node.strip()))
    elif isinstance(node, list):
        if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict):
            props = node[3]
            ck = props.get("componentkey") or props.get("componentKey") or ""
            if isinstance(ck, str) and ck.startswith("ProfileNullStateCardAnchor_"):
                ev.append(("heading", ck.split("_", 1)[1]))
            a11y = props.get("a11yText")
            if isinstance(a11y, str) and a11y.startswith("Thumbnail for "):
                ev.append(("media", a11y[len("Thumbnail for "):].strip()))
            for k, v in props.items():
                if k in _SKIP_KEYS:
                    continue
                if k == "initialItems" and isinstance(v, list):
                    for it in v:
                        ev.append(("item_start",))
                        _walk(it.get("item") if isinstance(it, dict) else it, rows, ev, depth + 1)
                        ev.append(("item_end",))
                    continue
                _walk(v, rows, ev, depth + 1, in_children=(k == "children"))
        else:
            for x in node:
                _walk(x, rows, ev, depth + 1, in_children)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in _SKIP_KEYS:
                continue
            _walk(v, rows, ev, depth + 1, in_children=(k == "children"))


def flight_to_sections(flight_text: str) -> dict[str, list[list[str]]]:
    """
    Turn one card's flight payload into {heading: [item_lines, ...]}, where
    item_lines is the ordered visible text of one entry. Media captions
    (document/image attachments) are dropped.
    """
    rows = parse_flight(flight_text)
    roots = [
        k for k, v in rows.items()
        if isinstance(v, list) and len(v) == 4 and isinstance(v[3], dict) and "data-sdui-component" in v[3]
    ]
    if not roots:
        return {}
    ev: list = []
    _walk(rows[roots[0]], rows, ev)

    media = {e[1] for e in ev if e[0] == "media"}
    sections: dict[str, list[list[str]]] = {}
    heading: str | None = None
    current: list[str] | None = None
    for e in ev:
        kind = e[0]
        if kind == "heading":
            heading = e[1]
            sections.setdefault(heading, [])
        elif kind == "item_start":
            current = []
        elif kind == "item_end":
            if heading is not None and current:
                sections[heading].append(current)
            current = None
        elif kind == "text" and current is not None:
            txt = e[1]
            if txt in media or txt == heading:
                continue
            current.append(txt)
    return sections


# --- line classifiers ------------------------------------------------------- #
_MON = r"[A-Z][a-z]{2}"
_DATE_RANGE_RE = re.compile(
    rf"^(?P<start>(?:{_MON} )?\d{{4}})\s*[-–]\s*(?P<end>(?:{_MON} )?\d{{4}}|Present)(?:\s*·.*)?$"
)
_ISSUED_RE = re.compile(rf"^Issued (?P<date>(?:{_MON} )?\d{{4}})")
_DURATION_RE = re.compile(r"^(\d+ yrs?)?\s*(\d+ mos?)?$")
_EMPLOYMENT_TYPES = (
    "Full-time", "Part-time", "Self-employed", "Freelance", "Contract",
    "Internship", "Apprenticeship", "Seasonal", "Temporary",
)
_EMPLOYMENT_RE = re.compile(r"^(" + "|".join(map(re.escape, _EMPLOYMENT_TYPES)) + r")(\s*·\s*(.*))?$")
_SKILLS_LINE_RE = re.compile(r"(and \+\d+ skills?$|^Skills:)")
_WORK_MODE_RE = re.compile(r"\s*·\s*(On-site|Remote|Hybrid)$")
_NOISE_RE = re.compile(r"^(Show all|See all|…|\.\.\.|Visible|Media)\b")


def _is_date(line: str) -> bool:
    return bool(_DATE_RANGE_RE.match(line))


def _is_duration(line: str) -> bool:
    return bool(line) and bool(_DURATION_RE.match(line)) and any(ch.isdigit() for ch in line)


def _is_location(line: str) -> bool:
    if _is_date(line) or len(line) > 90:
        return False
    return bool(_WORK_MODE_RE.search(line)) or ("," in line and len(line.split()) <= 8)


def _split_dates(line: str) -> tuple[str | None, str | None]:
    m = _DATE_RANGE_RE.match(line)
    if not m:
        return None, None
    return m.group("start"), m.group("end")


def _clean_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln and not _SKILLS_LINE_RE.search(ln) and not _NOISE_RE.match(ln)]


def _parse_role(title: str, company: str | None, rest: list[str]) -> ExperienceItem:
    """`rest` starts at the date line (or employment-type line) of one role."""
    emp_type = None
    if rest and _EMPLOYMENT_RE.match(rest[0]) and not _is_date(rest[0]):
        emp_type = _EMPLOYMENT_RE.match(rest[0]).group(1)
        rest = rest[1:]
    start = end = None
    if rest and _is_date(rest[0]):
        start, end = _split_dates(rest[0])
        rest = rest[1:]
    location = None
    if rest and _is_location(rest[0]):
        location = _WORK_MODE_RE.sub("", rest[0])
        rest = rest[1:]
    description = " ".join(rest).strip() or None
    return ExperienceItem(
        title=title.strip() or None,
        company_name=(company or "").strip() or None,
        location=location,
        start_date=start,
        end_date=end or ("Present" if start else None),
        description=description,
        employment_type=emp_type,
    )


def _role_boundaries(lines: list[str]) -> list[int]:
    """Indexes of role TITLE lines inside a grouped (multi-role) company item:
    a line is a title if a date line follows it directly or after one
    employment-type line."""
    idx = []
    for i, ln in enumerate(lines):
        if _is_date(ln) or _EMPLOYMENT_RE.match(ln) or _is_location(ln):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        nxt2 = lines[i + 2] if i + 2 < len(lines) else ""
        if _is_date(nxt) or (_EMPLOYMENT_RE.match(nxt) and _is_date(nxt2)):
            idx.append(i)
    return idx


def parse_experience(items: list[list[str]]) -> list[ExperienceItem]:
    out: list[ExperienceItem] = []
    for raw in items:
        lines = _clean_lines(raw)
        if len(lines) < 2:
            continue
        second = lines[1]
        grouped = _is_duration(second) or (
            _EMPLOYMENT_RE.match(second) and _is_duration((_EMPLOYMENT_RE.match(second).group(3) or "").strip())
        )
        if grouped:
            company = lines[0]
            group_type = None
            m = _EMPLOYMENT_RE.match(second)
            if m:
                group_type = m.group(1)
            body = lines[2:]
            bounds = _role_boundaries(body)
            for n, b in enumerate(bounds):
                stop = bounds[n + 1] if n + 1 < len(bounds) else len(body)
                role = _parse_role(body[b], company, body[b + 1:stop])
                role.employment_type = role.employment_type or group_type
                out.append(role)
            continue
        title = lines[0]
        company, emp_type = second, None
        if " · " in second:
            head, _, tail = second.partition(" · ")
            if _EMPLOYMENT_RE.match(tail.strip()):
                company, emp_type = head.strip(), tail.strip()
        item = _parse_role(title, company, lines[2:])
        if emp_type and not item.employment_type:
            item.employment_type = emp_type
        out.append(item)
    return out


_GRADE_RE = re.compile(r"^Grade:", re.I)


def parse_education(items: list[list[str]]) -> list[EducationItem]:
    out: list[EducationItem] = []
    for raw in items:
        lines = _clean_lines(raw)
        if not lines:
            continue
        institution = lines[0]
        degree = field = None
        start_year = end_year = None
        for ln in lines[1:]:
            if _is_date(ln):
                s, e = _split_dates(ln)
                start_year = int(s[-4:]) if s else None
                end_year = int(e[-4:]) if e and e[-4:].isdigit() else None
            elif re.fullmatch(r"\d{4}", ln):
                end_year = end_year or int(ln)
            elif degree is None and not _GRADE_RE.match(ln):
                degree, _, field = ln.partition(", ")
                degree = degree.strip() or None
                field = field.strip() or None
        out.append(EducationItem(
            institution=institution, degree=degree, field_of_study=field,
            start_year=start_year, end_year=end_year,
        ))
    return out


def parse_names(items: list[list[str]]) -> list[str]:
    names = []
    for raw in items:
        lines = _clean_lines(raw)
        if lines and lines[0] not in names:
            names.append(lines[0])
    return names


# Anchor names observed live: Experience, Education, Skills, Certifications,
# VolunteerExperience, Interests. The rest are the SDUI naming pattern applied
# to sections the test profiles did not carry.
_HEADING_TO_FIELD = {
    "Experience": "experience",
    "Education": "education",
    "Skills": "skills",
    "Certifications": "certifications",
    "Certification": "certifications",
    "LicensesAndCertifications": "certifications",
    "LicensesCertifications": "certifications",
    "Languages": "languages",
    "Language": "languages",
    "VolunteerExperience": "volunteering",
    "Volunteering": "volunteering",
}


def cards_to_sections(cards: dict[str, str]) -> dict[str, list]:
    """Merge every fetched card into the ProfileResponse section fields."""
    result: dict[str, list] = {
        "experience": [], "education": [], "skills": [], "certifications": [], "languages": [],
        "volunteering": [],
    }
    for card, flight in cards.items():
        try:
            sections = flight_to_sections(flight)
        except Exception as exc:  # defensive: a malformed card must not sink the response
            logger.warning("SDUI card %s did not parse: %s", card, exc)
            continue
        for heading, items in sections.items():
            field = _HEADING_TO_FIELD.get(heading)
            if not field or not items:
                continue
            if field in ("experience", "volunteering"):
                result[field].extend(parse_experience(items))
            elif field == "education":
                result[field].extend(parse_education(items))
            else:
                for name in parse_names(items):
                    if name not in result[field]:
                        result[field].append(name)
    return result
