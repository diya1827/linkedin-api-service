from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

@app.get("/", include_in_schema=False)
def frontend():
    """Serve the single-page UI. It calls POST /api/v1/parse-profile — no logic of its own."""
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "LinkedIn Parser API"}

@app.post("/api/v1/parse-profile", response_model=ProfileResponse)
async def parse_profile(payload: ProfileRequest):
    try:
        response_data = await fetch_linkedin_profile_voyager(str(payload.profile_url))
        return response_data
    except HTTPException as e:
        raise e
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(exc)}")


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