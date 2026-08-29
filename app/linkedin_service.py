import re
import os
import json
import base64
import html as html_lib
import logging
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from fastapi import HTTPException

from app.config import settings
from app.schemas import ProfileResponse, Location, ExperienceItem, EducationItem

logger = logging.getLogger(__name__)

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"


# --------------------------------------------------------------------------- #
# URL / token helpers
# --------------------------------------------------------------------------- #
def extract_handle_from_url(url_str: str) -> str:
    """Pull the vanity handle out of a LinkedIn profile URL."""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url_str)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid LinkedIn profile URL format.")
    return match.group(1).strip()


def clean_token(val: str) -> str:
    if not val:
        return ""
    return val.strip().strip('"').strip("'")


def _load_credentials() -> tuple[str, str]:
    """Return (li_at, csrf_token) from settings, or raise 500 if missing."""
    li_at = clean_token(settings.LINKEDIN_LI_AT_COOKIE)
    jsession = clean_token(settings.LINKEDIN_JSESSIONID)
    if not li_at or not jsession:
        raise HTTPException(
            status_code=500,
            detail="LinkedIn credentials missing in environment settings.",
        )
    # The csrf-token header must equal the JSESSIONID cookie value (quotes stripped).
    csrf_token = jsession.replace('"', "")
    return li_at, csrf_token


def _tracking_id() -> str:
    """A fresh base64 tracking id per request, matching the browser's page-instance
    suffix (a static value would itself be a bot signal)."""
    return base64.b64encode(os.urandom(16)).decode()


def _api_headers(csrf_token: str) -> dict:
    """
    Voyager JSON-API headers, matched against a REAL Chrome/Voyager request captured
    from the browser's network tab so they're indistinguishable from the web app at
    the header level. The stale/fake bits that used to give us away (and are now
    fixed): an obviously-fake x-li-track clientVersion, an old User-Agent, Accept
    +2.0 (LinkedIn uses +2.1), a "dummy" page-instance, and missing sec-* hints.

    Note: no Cookie header here — cookies are managed by the client's jar so a
    JSESSIONID rotated mid-flight (via Set-Cookie on a 302) stays in sync with csrf.
    """
    return {
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "csrf-token": csrf_token,
        "priority": "u=1, i",
        "referer": "https://www.linkedin.com/feed/",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        "x-li-lang": "en_US",
        "x-li-page-instance": "urn:li:page:d_flagship3_profile_view_base;" + _tracking_id(),
        "x-li-track": (
            '{"clientVersion":"1.13.46267","mpVersion":"1.13.46267","osName":"web",'
            '"timezoneOffset":5.5,"timezone":"Asia/Calcutta","deviceFormFactor":"DESKTOP",'
            '"mpName":"voyager-web","displayDensity":1.5,"displayWidth":1920,"displayHeight":1080}'
        ),
        "x-restli-protocol-version": "2.0.0",
    }


def _seed_jar() -> httpx.Cookies:
    """
    Seed a cookie jar. If LINKEDIN_COOKIE (a full raw Cookie header) is set, parse
    and use every cookie from it — this includes routing/security cookies (lidc,
    bcookie, liap, ...) that LinkedIn requires on protected endpoints. Otherwise
    fall back to just li_at + JSESSIONID.
    """
    jar = httpx.Cookies()
    raw = clean_token(settings.LINKEDIN_COOKIE)
    if raw:
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                jar.set(name.strip(), value.strip(), domain=".linkedin.com")
        if not jar.get("JSESSIONID"):
            raise HTTPException(status_code=500, detail="LINKEDIN_COOKIE is set but contains no JSESSIONID.")
        return jar

    li_at, csrf_token = _load_credentials()
    jar.set("li_at", li_at, domain=".linkedin.com")
    # Browsers store JSESSIONID wrapped in quotes; match that.
    jar.set("JSESSIONID", f'"{csrf_token}"', domain=".linkedin.com")
    return jar


def _proxy() -> str | None:
    """Optional outbound proxy for all LinkedIn requests (empty -> direct)."""
    return clean_token(settings.LINKEDIN_PROXY) or None


def make_voyager_client(follow: bool = False) -> httpx.AsyncClient:
    """
    Build an httpx client with a cookie jar seeded from credentials. Using the jar
    (instead of a manual Cookie header) lets LinkedIn rotate JSESSIONID via
    Set-Cookie on redirects and keeps subsequent requests consistent. Routed through
    LINKEDIN_PROXY when configured.
    """
    return httpx.AsyncClient(
        timeout=settings.REQUEST_TIMEOUT,
        follow_redirects=follow,
        cookies=_seed_jar(),
        proxy=_proxy(),
    )


def _current_csrf(client: httpx.AsyncClient) -> str:
    """Read the live JSESSIONID from the jar (quotes stripped) for csrf-token."""
    raw = client.cookies.get("JSESSIONID") or ""
    return raw.strip('"')


async def voyager_get(client: httpx.AsyncClient, url: str, max_redirects: int = 4) -> httpx.Response:
    """
    GET a Voyager API URL, following redirects manually so csrf-token is re-synced
    to the (possibly rotated) JSESSIONID on every hop. This is what fixes the
    "CSRF check failed" 302->403 loop that auto-follow produced.
    """
    resp = None
    for _ in range(max_redirects + 1):
        resp = await client.get(url, headers=_api_headers(_current_csrf(client)))
        location = resp.headers.get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            # Location may be relative (e.g. "/uas/login?redirect=1"); resolve it
            # against the current URL so client.get doesn't choke on a bare path.
            url = urljoin(str(resp.url), location)
            continue
        break
    return resp


def build_voyager_headers() -> dict:
    """Backwards-compatible header builder (includes Cookie) for standalone use."""
    li_at, csrf_token = _load_credentials()
    return {**_api_headers(csrf_token), "Cookie": f'li_at={li_at}; JSESSIONID="{csrf_token}"'}


def build_html_headers() -> dict:
    """
    Headers that mimic a real browser *document navigation* (not an API call).

    LinkedIn's anti-bot layer returns HTTP 999 when a page GET carries JSON-API
    headers (Accept: normalized+json, csrf-token, x-restli-*). A genuine
    navigation sends Accept: text/html plus the Sec-Fetch/Sec-CH-UA hints below.
    """
    li_at, csrf_token = _load_credentials()
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "Cookie": f'li_at={li_at}; JSESSIONID="{csrf_token}"',
    }


# --------------------------------------------------------------------------- #
# Network layer — fetch the raw normalized payload
# --------------------------------------------------------------------------- #
async def _get(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> httpx.Response:
    return await client.get(url, headers=headers or build_voyager_headers())


_PROFILE_TYPE_TAILS = (".Profile", ".MiniProfile", "profile.Position", "profile.Education")


def _json_or_none(resp: httpx.Response) -> dict | None:
    """
    Return parsed JSON only if the response actually IS JSON. A 200 can carry an
    HTML challenge/login page (bot mitigation), which would make resp.json() raise
    and surface as a confusing 500. Guard on content-type + a real parse.
    """
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _usable(payload: dict | None) -> bool:
    """
    True only if the payload carries a real profile entity. A soft block / stale
    queryId / non-visible profile returns 200 with an empty `included`, which must
    NOT be treated as success (it would yield a hollow profile whose name is just
    the handle).
    """
    if not payload:
        return False
    for item in payload.get("included", []) or []:
        if isinstance(item, dict) and item.get("$type", "").endswith(_PROFILE_TYPE_TAILS):
            return True
    return False


async def _fetch_profile_html(handle: str) -> httpx.Response:
    """Strategy 3 network call, factored out so it can be mocked in tests. Uses a
    browser-navigation header set (not the JSON-API headers) to dodge the 999 block."""
    async with httpx.AsyncClient(
        timeout=settings.REQUEST_TIMEOUT, follow_redirects=True, proxy=_proxy()
    ) as html_client:
        return await html_client.get(
            f"https://www.linkedin.com/in/{quote(handle)}/", headers=build_html_headers()
        )


async def fetch_profile_payload(handle: str) -> dict:
    """
    Fetch the raw normalized JSON for a profile, trying strategies in order:

      1. GraphQL identity query   (requires LINKEDIN_PROFILE_QUERY_ID)
      2. Legacy profileView REST  (deprecated; best-effort)
      3. Embedded HTML JSON       (GET the profile page, extract Voyager blocks)

    Each API strategy only "succeeds" on a 200 whose payload is _usable() — an
    empty/soft-blocked 200 falls through instead of returning a hollow profile.
    All strategies yield the same normalized envelope ({"data", "included"}),
    which parse_voyager_json() understands.
    """
    query_id = clean_token(settings.LINKEDIN_PROFILE_QUERY_ID)
    last_status = "n/a"
    last_body = ""

    # API strategies share a cookie jar so JSESSIONID rotation is handled once.
    async with make_voyager_client() as client:
        # ---- Strategy 1: GraphQL --------------------------------------------
        if query_id:
            gql_url = (
                f"{VOYAGER_BASE}/graphql?includeWebMetadata=true"
                f"&variables=(vanityName:{quote(handle)})"
                f"&queryId={query_id}"
            )
            resp = await voyager_get(client, gql_url)
            logger.info("GraphQL profile fetch -> %s", resp.status_code)
            payload = _json_or_none(resp) if resp.status_code == 200 else None
            if _usable(payload):
                return payload
            last_status, last_body = resp.status_code, resp.text[:200]
            logger.warning("GraphQL fetch not usable (status %s)", resp.status_code)

        # ---- Strategy 2: legacy profileView ---------------------------------
        legacy_url = f"{VOYAGER_BASE}/identity/profiles/{quote(handle)}/profileView"
        resp = await voyager_get(client, legacy_url)
        logger.info("profileView fetch -> %s", resp.status_code)
        payload = _json_or_none(resp) if resp.status_code == 200 else None
        if _usable(payload):
            return payload
        last_status, last_body = resp.status_code, resp.text[:200]

    # ---- Strategy 3: Embedded HTML JSON (separate browser-style client) ------
    html_resp = await _fetch_profile_html(handle)
    logger.info("HTML profile fetch -> %s", html_resp.status_code)
    if html_resp.status_code == 200:
        payload = extract_embedded_payload(html_resp.text)
        if _usable(payload):
            logger.info("Extracted %d embedded objects from HTML", len(payload["included"]))
            return payload
        logger.warning("HTML fetched but no usable profile JSON found (login wall / soft block).")
    if last_status == "n/a":
        last_status = html_resp.status_code

    hint = (
        " Set LINKEDIN_PROFILE_QUERY_ID in .env to a current GraphQL queryId, or "
        "verify cookies via GET /api/v1/debug/auth."
    )
    raise HTTPException(
        status_code=502,
        detail=f"Could not fetch profile via any strategy (last status {last_status}).{hint} Body: {last_body}",
    )


# --------------------------------------------------------------------------- #
# Embedded-JSON extraction (Strategy 2)
# --------------------------------------------------------------------------- #
_CODE_BLOCK_RE = re.compile(r'<code[^>]*>(.*?)</code>', re.DOTALL)


def extract_embedded_payload(html_text: str) -> dict:
    """
    LinkedIn server-renders Voyager API responses into <code> blocks in the
    profile HTML. Each block is HTML-escaped JSON; the profile block carries an
    "included" array of the same normalized objects the JSON API returns.

    We decode every block, then merge the "included" arrays from any block that
    has one, de-duplicating by entityUrn.
    """
    merged: list[dict] = []
    seen: set = set()

    for raw in _CODE_BLOCK_RE.findall(html_text):
        decoded = html_lib.unescape(raw).strip()
        if '"included"' not in decoded:
            continue
        try:
            obj = json.loads(decoded)
        except (ValueError, json.JSONDecodeError):
            continue
        for entry in obj.get("included", []) or []:
            if not isinstance(entry, dict):
                continue
            key = entry.get("entityUrn") or entry.get("*entityUrn") or id(entry)
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)

    return {"data": {}, "included": merged}


# --------------------------------------------------------------------------- #
# Parsing — normalized payload -> ProfileResponse
# --------------------------------------------------------------------------- #
def _date_range(item: dict) -> dict:
    """The date container differs by schema: classic uses `timePeriod`, dash uses
    `dateRange`. Return whichever is present."""
    rng = item.get("dateRange") or item.get("timePeriod") or {}
    return rng if isinstance(rng, dict) else {}


def _date_node(rng: dict, which: str) -> dict:
    """Get the start/end node. dash: start/end; classic: startDate/endDate."""
    node = rng.get(which) or rng.get(f"{which}Date") or {}
    return node if isinstance(node, dict) else {}


def _fmt_date(d: dict) -> str | None:
    if not d:
        return None
    year, month = d.get("year"), d.get("month")
    if year and month:
        return f"{year}-{month:02d}"
    return str(year) if year else None


def _time_period(item: dict) -> tuple[str | None, str | None]:
    """Extract (start, end) date strings, handling both classic and dash schemas.
    A missing end means an ongoing role -> 'Present'."""
    rng = _date_range(item)
    start = _fmt_date(_date_node(rng, "start"))
    end = _fmt_date(_date_node(rng, "end"))
    return start, (end or "Present")


def _year(item: dict, which: str) -> int | None:
    """Year for start/end. Accepts 'start'/'end' or legacy 'startDate'/'endDate'."""
    which = which.replace("Date", "")
    node = _date_node(_date_range(item), which)
    year = node.get("year")
    return int(year) if year else None


def _vector_image_url(vector: dict | None) -> str | None:
    """Build a full image URL from a Voyager VectorImage (rootUrl + artifacts)."""
    if not vector:
        return None
    root = vector.get("rootUrl")
    artifacts = vector.get("artifacts") or []
    if not root or not artifacts:
        return None
    # Pick the largest available artifact.
    best = max(artifacts, key=lambda a: a.get("width", 0))
    segment = best.get("fileIdentifyingUrlPathSegment", "")
    return f"{root}{segment}" if segment else None


def _extract_picture(item: dict) -> str | None:
    """
    Profile pictures live in different places across shapes:
      - classic MiniProfile: item["picture"]["com.linkedin.common.VectorImage"]
      - dash Profile:        item["profilePicture"]["displayImageReference"]["vectorImage"]
                        or   item["profilePicture"]["displayImageReferenceResolutionResult"]["vectorImage"]
    """
    if not isinstance(item, dict):
        return None

    pic = item.get("picture") or {}
    if isinstance(pic, dict):
        vector = pic.get("com.linkedin.common.VectorImage") or pic.get("vectorImage")
        url = _vector_image_url(vector)
        if url:
            return url

    profile_pic = item.get("profilePicture") or {}
    if isinstance(profile_pic, dict):
        ref = (
            profile_pic.get("displayImageReferenceResolutionResult")
            or profile_pic.get("displayImageReference")
            or {}
        )
        if isinstance(ref, dict):
            return _vector_image_url(ref.get("vectorImage"))
    return None


def parse_voyager_json(profile_url: str, handle: str, data: dict) -> ProfileResponse:
    """
    Turn a normalized Voyager payload into a ProfileResponse.

    Dispatches on the tail of each item's $type so it works for both the classic
    (`com.linkedin.voyager.identity.profile.*`) and dash
    (`com.linkedin.voyager.dash.identity.profile.*`) schemas.
    """
    included = [it for it in (data.get("included", []) or []) if isinstance(it, dict)]
    # dash objects reference each other by URN; build a lookup to resolve them.
    by_urn = {it["entityUrn"]: it for it in included if it.get("entityUrn")}

    def _resolve_company(item: dict) -> str | None:
        """Company name may be an inline literal (`companyName`, classic) or come
        via `company` (a nested dict, or a URN string that resolves against
        `included`, dash)."""
        name = item.get("companyName")
        if isinstance(name, str) and name:
            return name  # classic: literal name, NOT a URN
        val = item.get("company")
        if isinstance(val, dict):
            return val.get("name")
        if isinstance(val, str):
            return (by_urn.get(val) or {}).get("name") or None  # dash: URN -> Company
        return None

    profile_obj: dict = {}
    picture_url: str | None = None
    experiences, educations, skills, certifications, languages = [], [], [], [], []

    for item in included:
        type_str = item.get("$type", "")

        if type_str.endswith(".Profile"):
            # Prefer the entry that actually carries name fields.
            if not profile_obj or item.get("firstName"):
                profile_obj = item
            picture_url = picture_url or _extract_picture(item)

        elif type_str.endswith(".MiniProfile"):
            picture_url = picture_url or _extract_picture(item)

        elif type_str.endswith("profile.Position"):
            title = item.get("title")
            company = _resolve_company(item)
            # Skip bare stub positions (e.g. the top-card query's reference-only
            # Position) that carry no title and no company.
            if not title and not company:
                continue
            start, end = _time_period(item)
            experiences.append(ExperienceItem(
                title=title,
                company_name=company,
                location=item.get("locationName"),
                start_date=start,
                end_date=end,
                description=item.get("description"),
            ))

        elif type_str.endswith("profile.Education"):
            institution = item.get("schoolName")
            degree = item.get("degreeName")
            field = item.get("fieldOfStudy")
            if not institution and not degree and not field:
                continue
            educations.append(EducationItem(
                institution=institution,
                degree=degree,
                field_of_study=field,
                start_year=_year(item, "startDate"),
                end_year=_year(item, "endDate"),
            ))

        elif type_str.endswith("profile.Skill"):
            if item.get("name"):
                skills.append(item["name"])

        elif type_str.endswith("profile.Certification"):
            if item.get("name"):
                certifications.append(item["name"])

        elif type_str.endswith("profile.Language"):
            if item.get("name"):
                languages.append(item["name"])

    first_name = profile_obj.get("firstName")
    last_name = profile_obj.get("lastName")
    full_name = f"{first_name or ''} {last_name or ''}".strip() or handle
    picture_url = picture_url or _extract_picture(profile_obj)

    geo = profile_obj.get("geoLocation")
    if isinstance(geo, str):  # dash: URN reference
        geo = by_urn.get(geo) or {}
    geo = geo if isinstance(geo, dict) else {}
    geo_inner = geo.get("geo") if isinstance(geo.get("geo"), dict) else {}
    raw_loc = (
        profile_obj.get("locationName")
        or profile_obj.get("geoLocationName")
        or geo_inner.get("defaultLocalizedName")
        or geo.get("defaultLocalizedName")
    )

    return ProfileResponse(
        profile_url=profile_url,
        profile_handle=handle,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        headline=profile_obj.get("headline"),
        location=Location(raw_location=raw_loc) if raw_loc else None,
        about=profile_obj.get("summary"),
        profile_image_url=picture_url,
        experience=experiences,
        education=educations,
        skills=skills,
        certifications=certifications,
        languages=languages,
    )


async def fetch_linkedin_profile_voyager(profile_url: str) -> ProfileResponse:
    """Top-level entry: URL -> fetched payload -> parsed ProfileResponse."""
    handle = extract_handle_from_url(profile_url)
    data = await fetch_profile_payload(handle)
    return parse_voyager_json(profile_url, handle, data)


# --------------------------------------------------------------------------- #
# Diagnostics (used by the /api/v1/debug/* routes)
# --------------------------------------------------------------------------- #
async def check_auth() -> dict:
    """Preflight the current cookies against the stable /me endpoint."""
    async with make_voyager_client() as client:
        response = await voyager_get(client, f"{VOYAGER_BASE}/me")

    authenticated = response.status_code == 200
    logger.info("Auth preflight /me -> %s", response.status_code)
    return {
        "authenticated": authenticated,
        "status_code": response.status_code,
        "interpretation": (
            "Session cookies are valid." if authenticated
            else "Session rejected — refresh li_at / JSESSIONID (or LinkedIn is blocking this IP)."
        ),
        "body_preview": response.text[:300],
    }


async def debug_html(handle: str) -> dict:
    """
    Diagnostic for Strategy 2: fetch the profile HTML and report whether usable
    embedded Voyager JSON is present (vs a login wall / empty shell).
    """
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT, follow_redirects=True) as client:
        resp = await _get(
            client, f"https://www.linkedin.com/in/{quote(handle)}/", headers=build_html_headers()
        )

    text = resp.text
    payload = extract_embedded_payload(text) if resp.status_code == 200 else {"included": []}
    types = sorted({e.get("$type", "") for e in payload["included"]})
    looks_like_login = "authwall" in resp.url.path or "/login" in resp.url.path or "session_key" in text[:5000]

    return {
        "status_code": resp.status_code,
        "final_url": str(resp.url),
        "looks_like_login_wall": looks_like_login,
        "code_blocks_found": len(_CODE_BLOCK_RE.findall(text)),
        "embedded_objects_extracted": len(payload["included"]),
        "distinct_types_sample": types[:25],
        "usable": bool(payload["included"]),
    }


async def probe_graphql(handle: str, query_id: str, var: str = "vanityName", follow: bool = False) -> dict:
    """
    Diagnostic: try a specific GraphQL queryId + variable name and report whether
    it returns a usable profile. Lets you test candidate queryIds without editing
    .env and restarting between each attempt.
    """
    url = (
        f"{VOYAGER_BASE}/graphql?includeWebMetadata=true"
        f"&variables=({var}:{quote(handle)})&queryId={query_id}"
    )
    # follow=True uses the csrf-resyncing manual redirect handler; follow=False
    # does a single raw request so you can still inspect the initial 302/Location.
    async with make_voyager_client() as client:
        if follow:
            resp = await voyager_get(client, url)
        else:
            resp = await client.get(url, headers=_api_headers(_current_csrf(client)))

    logger.info("GraphQL probe queryId=%s var=%s -> %s", query_id, var, resp.status_code)
    # Which cookies does our client hold / did the response try to set? This is the
    # key signal for a self-redirect: LinkedIn often 302s to set a routing cookie.
    set_cookies = resp.headers.get_list("set-cookie")
    result = {
        "query_id": query_id,
        "var": var,
        "status_code": resp.status_code,
        "url": url,
        "location": resp.headers.get("location"),
        "final_url": str(resp.url),
        "redirect_chain": [
            {"status": r.status_code, "location": r.headers.get("location")}
            for r in resp.history
        ],
        "csrf_sent": _current_csrf(client),
        "set_cookie": [c.split(";")[0] for c in set_cookies],  # name=value only, no attrs
        "response_headers": {k: v for k, v in resp.headers.items() if k.lower() != "set-cookie"},
        "jar_cookie_names": list(client.cookies.keys()),
    }

    if resp.status_code != 200:
        result["body_preview"] = resp.text[:300]
        result["usable"] = False
        return result

    try:
        data = resp.json()
    except Exception:
        result["body_preview"] = resp.text[:300]
        result["usable"] = False
        return result

    included = data.get("included", []) or []
    types = sorted({e.get("$type", "") for e in included})
    result["included_count"] = len(included)
    result["distinct_types_sample"] = types[:25]
    result["data_keys"] = list(data.keys())

    try:
        parsed = parse_voyager_json("probe", handle, data)
    except Exception as exc:
        # Surface parser failures instead of a bare 500, and include a small sample
        # of the raw graph so we can see the real response shape to fix the mapping.
        logger.exception("parse_voyager_json failed in probe")
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        result["included_sample"] = included[:3]
        result["usable"] = False
        return result

    result.update({
        "parsed_name": parsed.full_name,
        "parsed_headline": parsed.headline,
        "experience_count": len(parsed.experience),
        # Usable if the query returned a graph AND we resolved a name beyond the handle.
        "usable": bool(included) and parsed.full_name != handle,
    })
    return result


DUMP_PATH = Path(__file__).resolve().parent.parent / "debug_dump.json"


async def debug_raw_graphql(query_id: str, variables: str) -> dict:
    """
    Diagnostic: run ANY GraphQL query (arbitrary queryId + raw variables string,
    e.g. "(profileUrn:urn:li:fsd_profile:XXXX)") and write the full JSON response
    to debug_dump.json for offline inspection. Returns a small summary.

    Used to capture the profile-cards/components response so its component-tree
    shape can be mapped without pasting a huge payload.
    """
    url = f"{VOYAGER_BASE}/graphql?includeWebMetadata=true&variables={variables}&queryId={query_id}"
    async with make_voyager_client() as client:
        resp = await voyager_get(client, url)

    result = {"status_code": resp.status_code, "url": url}
    if resp.status_code != 200:
        result["body_preview"] = resp.text[:400]
        return result

    try:
        data = resp.json()
    except Exception:
        result["body_preview"] = resp.text[:400]
        return result

    included = [x for x in (data.get("included") or []) if isinstance(x, dict)]
    DUMP_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "status_code": 200,
        "included_count": len(included),
        "distinct_types": sorted({x.get("$type", "") for x in included}),
        "data_keys": list(data.keys()),
        "wrote_to": str(DUMP_PATH),
    }


async def debug_components(member_id: str, section: str, query_id: str) -> dict:
    """
    Diagnostic for ProfileComponents: builds the variables string server-side from
    a URL-safe member id (the `ACoAA...` part of a profile URN), so no parentheses
    or colons need to survive a browser URL bar. Dumps the full response to file.
    """
    profile_urn = f"urn:li:fsd_profile:{member_id}"
    variables = f"(profileUrn:{profile_urn},sectionType:{section})"
    result = await debug_raw_graphql(query_id, variables)
    result["variables_used"] = variables
    return result


async def debug_cards(member_id: str, query_id: str) -> dict:
    """
    Diagnostic for ProfileCards (or any query taking just profileUrn). Builds the
    variables server-side from a URL-safe member id and dumps the full response.
    """
    variables = f"(profileUrn:urn:li:fsd_profile:{member_id})"
    result = await debug_raw_graphql(query_id, variables)
    result["variables_used"] = variables
    return result


_PROBE_ENDPOINTS = {
    "profileView": VOYAGER_BASE + "/identity/profiles/{handle}/profileView",
    "dash": VOYAGER_BASE + "/identity/dash/profiles?q=memberIdentity&memberIdentity={handle}",
}


async def fetch_raw(handle: str, variant: str = "dash", follow: bool = False) -> dict:
    """Diagnostic: hit one candidate endpoint and return raw status/headers/body."""
    if variant not in _PROBE_ENDPOINTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown probe variant '{variant}'. Choose one of {list(_PROBE_ENDPOINTS)}.",
        )

    endpoint = _PROBE_ENDPOINTS[variant].format(handle=quote(handle))
    async with make_voyager_client() as client:
        if follow:
            response = await voyager_get(client, endpoint)
        else:
            response = await client.get(endpoint, headers=_api_headers(_current_csrf(client)))

    logger.info("Probe %s -> %s", variant, response.status_code)
    try:
        body = response.json()
    except Exception:
        body = {"_raw_text": response.text[:1000]}

    return {
        "variant": variant,
        "endpoint": endpoint,
        "status_code": response.status_code,
        "location": response.headers.get("location"),
        "final_url": str(response.url),
        "redirect_chain": [
            {"status": r.status_code, "location": r.headers.get("location")}
            for r in response.history
        ],
        "content_type": response.headers.get("content-type"),
        "body": body,
    }
