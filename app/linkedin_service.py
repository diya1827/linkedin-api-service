import re
import json
import html as html_lib
import logging
from urllib.parse import quote

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


def _api_headers(csrf_token: str) -> dict:
    """
    Voyager JSON-API headers. Note: no Cookie header here — cookies are managed by
    the httpx client's cookie jar (see make_voyager_client) so that a JSESSIONID
    rotated mid-flight (via Set-Cookie on a 302) stays consistent with csrf-token.
    """
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/vnd.linkedin.normalized+json+2.0",
        "Accept-Language": "en-US,en;q=0.9",
        "x-li-lang": "en_US",
        "x-li-track": (
            '{"clientVersion":"1.13.0","osName":"web","timezoneOffset":0,'
            '"deviceFormFactor":"DESKTOP","mpName":"voyager-web"}'
        ),
        "x-li-page-instance": "urn:li:page:d_flagship3_profile_view_base;dummy",
        "x-restli-protocol-version": "2.0.0",
        "csrf-token": csrf_token,
        "Referer": "https://www.linkedin.com/feed/",
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


def make_voyager_client(follow: bool = False) -> httpx.AsyncClient:
    """
    Build an httpx client with a cookie jar seeded from credentials. Using the jar
    (instead of a manual Cookie header) lets LinkedIn rotate JSESSIONID via
    Set-Cookie on redirects and keeps subsequent requests consistent.
    """
    return httpx.AsyncClient(
        timeout=settings.REQUEST_TIMEOUT, follow_redirects=follow, cookies=_seed_jar()
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
            url = location  # jar already absorbed any Set-Cookie from this response
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


async def fetch_profile_payload(handle: str) -> dict:
    """
    Fetch the raw normalized JSON for a profile, trying strategies in order:

      1. GraphQL identity query   (requires LINKEDIN_PROFILE_QUERY_ID)
      2. Embedded HTML JSON       (GET the profile page, extract Voyager blocks)
      3. Legacy profileView REST  (deprecated; best-effort)

    All three yield the same normalized envelope ({"data": ..., "included": [...]}),
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
            if resp.status_code == 200:
                return resp.json()
            last_status, last_body = resp.status_code, resp.text[:200]
            logger.warning("GraphQL fetch failed (%s)", resp.status_code)

        # ---- Strategy 3: legacy profileView ---------------------------------
        legacy_url = f"{VOYAGER_BASE}/identity/profiles/{quote(handle)}/profileView"
        resp = await voyager_get(client, legacy_url)
        logger.info("profileView fetch -> %s", resp.status_code)
        if resp.status_code == 200:
            return resp.json()
        last_status, last_body = resp.status_code, resp.text[:200]

    # ---- Strategy 2: Embedded HTML JSON (separate browser-style client) ------
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT, follow_redirects=True) as html_client:
        html_resp = await html_client.get(
            f"https://www.linkedin.com/in/{quote(handle)}/", headers=build_html_headers()
        )
    logger.info("HTML profile fetch -> %s", html_resp.status_code)
    if html_resp.status_code == 200:
        payload = extract_embedded_payload(html_resp.text)
        if payload.get("included"):
            logger.info("Extracted %d embedded objects from HTML", len(payload["included"]))
            return payload
        logger.warning("HTML fetched but no embedded profile JSON found (login wall?).")
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
def _time_period(item: dict) -> tuple[str | None, str | None]:
    """Extract start/end date strings from a Voyager timePeriod object."""
    tp = item.get("timePeriod") or {}
    start = tp.get("startDate") or {}
    end = tp.get("endDate") or {}

    def fmt(d: dict) -> str | None:
        if not d:
            return None
        year = d.get("year")
        month = d.get("month")
        if year and month:
            return f"{year}-{month:02d}"
        return str(year) if year else None

    return fmt(start), fmt(end) or "Present"


def _year(item: dict, key: str) -> int | None:
    tp = item.get("timePeriod") or {}
    node = tp.get(key) or {}
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
    """
    pic = item.get("picture") or {}
    vector = pic.get("com.linkedin.common.VectorImage") or pic.get("vectorImage")
    url = _vector_image_url(vector)
    if url:
        return url

    profile_pic = item.get("profilePicture") or {}
    ref = profile_pic.get("displayImageReference") or {}
    return _vector_image_url(ref.get("vectorImage"))


def parse_voyager_json(profile_url: str, handle: str, data: dict) -> ProfileResponse:
    """
    Turn a normalized Voyager payload into a ProfileResponse.

    Dispatches on the tail of each item's $type so it works for both the classic
    (`com.linkedin.voyager.identity.profile.*`) and dash
    (`com.linkedin.voyager.dash.identity.profile.*`) schemas.
    """
    included = data.get("included", []) or []
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
            company = item.get("companyName") or (item.get("company") or {}).get("name")
            start, end = _time_period(item)
            experiences.append(ExperienceItem(
                title=item.get("title"),
                company_name=company,
                location=item.get("locationName"),
                start_date=start,
                end_date=end,
                description=item.get("description"),
            ))

        elif type_str.endswith("profile.Education"):
            educations.append(EducationItem(
                institution=item.get("schoolName"),
                degree=item.get("degreeName"),
                field_of_study=item.get("fieldOfStudy"),
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

    raw_loc = (
        profile_obj.get("locationName")
        or profile_obj.get("geoLocationName")
        or (profile_obj.get("geoLocation") or {}).get("geo", {}).get("defaultLocalizedName")
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
    parsed = parse_voyager_json("probe", handle, data)
    result.update({
        "included_count": len(included),
        "distinct_types_sample": types[:25],
        "parsed_name": parsed.full_name,
        "parsed_headline": parsed.headline,
        "experience_count": len(parsed.experience),
        # Usable if the query returned a graph AND we resolved a name beyond the handle.
        "usable": bool(included) and parsed.full_name != handle,
    })
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
