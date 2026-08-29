import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from app.config import settings
from app.schemas import ProfileRequest, ProfileResponse
from app.linkedin_service import (
    fetch_linkedin_profile_voyager,
    check_auth,
    fetch_raw,
    debug_html,
    probe_graphql,
    debug_raw_graphql,
    debug_components,
    debug_cards,
    extract_handle_from_url,
)

app = FastAPI(
    title="Reverse-Engineered LinkedIn Parser API",
    description="Direct endpoint integration with LinkedIn Voyager API without browser engines.",
    version="1.0.0"
)

# Enable CORS for public access.
# NOTE: allow_origins=["*"] cannot be combined with allow_credentials=True
# (browsers reject that combo). This API doesn't rely on browser-sent cookies,
# so credentials are disabled here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# In-memory cache + rate limit (single-instance; see README for the caveat)
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, ProfileResponse]] = {}
_hits: dict[str, list[float]] = {}


def _cache_get(handle: str) -> ProfileResponse | None:
    entry = _cache.get(handle)
    if entry and (time.time() - entry[0]) < settings.CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _cache_set(handle: str, resp: ProfileResponse) -> None:
    _cache[handle] = (time.time(), resp)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < 60]
    recent.append(now)
    _hits[ip] = recent
    return len(recent) > settings.RATE_LIMIT_PER_MINUTE


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def frontend():
    """Serve the single-page UI. It calls POST /api/v1/parse-profile — no logic of its own."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health_check():
    """
    Liveness + LinkedIn session status. The session field is the single most
    useful signal for a reviewer: it says whether the backing cookies still work
    without leaking any cookie material or response bodies.
    """
    have_creds = bool(settings.LINKEDIN_COOKIE or
                      (settings.LINKEDIN_LI_AT_COOKIE and settings.LINKEDIN_JSESSIONID))
    if not have_creds:
        session = "not_configured"
    else:
        try:
            auth = await check_auth()
            session = "ok" if auth.get("authenticated") else "expired"
        except Exception:
            session = "unreachable"
    return {"status": "ok", "service": "LinkedIn Parser API", "linkedin_session": session}


@app.post("/api/v1/parse-profile", response_model=ProfileResponse)
async def parse_profile(payload: ProfileRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again shortly.")

    # Validate/extract handle up front so the cache key is stable and bad URLs 400 fast.
    handle = extract_handle_from_url(str(payload.profile_url))
    cache_key = f"{handle}|sections={int(payload.include_sections)}"

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        response_data = await fetch_linkedin_profile_voyager(
            str(payload.profile_url), include_sections=payload.include_sections
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(exc)}")

    _cache_set(cache_key, response_data)
    return response_data


# --------------------------------------------------------------------------- #
# Debug routes — OFF unless ENABLE_DEBUG_ROUTES=true. They can drive arbitrary
# Voyager requests with the server's session cookie, so never enable in prod.
# --------------------------------------------------------------------------- #
if settings.ENABLE_DEBUG_ROUTES:

    @app.get("/api/v1/debug/auth")
    async def debug_auth():
        """Check whether the configured LinkedIn cookies still authenticate."""
        return await check_auth()

    @app.get("/api/v1/debug/probe")
    async def debug_probe(
        profile_url: str = Query(..., description="A LinkedIn profile URL, e.g. https://www.linkedin.com/in/<handle>"),
        variant: str = Query("dash", description="Which endpoint to probe: 'dash' or 'profileView'"),
        follow: bool = Query(False, description="Follow redirects and report the final destination"),
    ):
        """Hit a candidate endpoint and return the raw status + body for inspection."""
        handle = extract_handle_from_url(profile_url)
        return await fetch_raw(handle, variant, follow)

    @app.get("/api/v1/debug/html")
    async def debug_html_route(
        profile_url: str = Query(..., description="A LinkedIn profile URL, e.g. https://www.linkedin.com/in/<handle>"),
    ):
        """Check whether the profile HTML contains usable embedded Voyager JSON."""
        handle = extract_handle_from_url(profile_url)
        return await debug_html(handle)

    @app.get("/api/v1/debug/graphql")
    async def debug_graphql_route(
        profile_url: str = Query(..., description="A LinkedIn profile URL, e.g. https://www.linkedin.com/in/<handle>"),
        query_id: str = Query(..., description="A voyagerIdentityDashProfiles.<hash> queryId to test"),
        var: str = Query("vanityName", description="GraphQL variable name: vanityName or memberIdentity"),
        follow: bool = Query(False, description="Follow redirects and report the final destination"),
    ):
        """Test a candidate GraphQL queryId and report whether it returns a real profile."""
        handle = extract_handle_from_url(profile_url)
        return await probe_graphql(handle, query_id, var, follow)

    @app.get("/api/v1/debug/raw-graphql")
    async def debug_raw_graphql_route(
        query_id: str = Query(..., description="Full queryId, e.g. voyagerIdentityDashProfileCards.<hash>"),
        variables: str = Query(..., description="Raw variables string, e.g. (profileUrn:urn:li:fsd_profile:XXXX)"),
    ):
        """Run any GraphQL query and dump the full response to debug_dump.json for inspection."""
        return await debug_raw_graphql(query_id, variables)

    @app.get("/api/v1/debug/components")
    async def debug_components_route(
        member_id: str = Query(..., description="The URL-safe member id, e.g. ACoAAEHRfJwBFRk-j7PMbGPTjbKFHocu_mIdtLw"),
        query_id: str = Query(..., description="A voyagerIdentityDashProfileComponents.<hash> queryId"),
        section: str = Query("experience", description="sectionType, e.g. experience / education / skills"),
    ):
        """Test a ProfileComponents queryId for a section; dumps full response to debug_dump.json."""
        return await debug_components(member_id, section, query_id)

    @app.get("/api/v1/debug/cards")
    async def debug_cards_route(
        member_id: str = Query(..., description="The URL-safe member id, e.g. ACoAAEHRfJwBFRk-j7PMbGPTjbKFHocu_mIdtLw"),
        query_id: str = Query(..., description="A queryId taking only profileUrn, e.g. voyagerIdentityDashProfileCards.<hash>"),
    ):
        """Test a query that takes only profileUrn (e.g. ProfileCards); dumps full response."""
        return await debug_cards(member_id, query_id)
