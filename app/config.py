from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # App Information
    APP_NAME: str = "LinkedIn Profile Data API"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # Security & Credentials (loaded from .env or environment)
    LINKEDIN_LI_AT_COOKIE: str = ""
    LINKEDIN_JSESSIONID: str = "ajax:1234567890"

    # Optional: the FULL raw Cookie header copied from a real browser request
    # (DevTools > Network > a voyager request > Request Headers > Cookie). When set,
    # this takes precedence over LINKEDIN_LI_AT_COOKIE/JSESSIONID and is sent as-is,
    # so routing/security cookies (lidc, bcookie, liap, ...) are included — which is
    # often required to get past LinkedIn's anti-automation on protected endpoints.
    LINKEDIN_COOKIE: str = ""

    # GraphQL queryId for the identity profile query. LinkedIn rotates this every
    # few weeks, so it lives in config instead of the code. Capture the current
    # one from your browser (DevTools > Network > filter "graphql" > copy the
    # queryId=... value) and paste it here. If empty, the service falls back to
    # the legacy profileView REST endpoint.
    LINKEDIN_PROFILE_QUERY_ID: str = ""

    # How long (seconds) to wait on LinkedIn before giving up.
    REQUEST_TIMEOUT: float = 15.0

    # Optional outbound proxy (e.g. a residential proxy) for all LinkedIn requests.
    # LinkedIn flags IPs that send automated traffic; routing through a rotating
    # residential proxy is how this is run reliably. Format: http://user:pass@host:port
    # Empty = direct connection. (A proxy is network routing, not a browser engine,
    # so this stays within the "httpx only, no browser" constraint.)
    LINKEDIN_PROXY: str = ""

    # Expose the /api/v1/debug/* routes. These let a caller drive arbitrary
    # Voyager requests using the server's session cookie, so they MUST stay off
    # on any public deployment. Off by default; enable only for local debugging.
    ENABLE_DEBUG_ROUTES: bool = False

    # Serve a cached profile for this many seconds (keyed on handle). Protects the
    # backing LinkedIn account from repeated hits on a public endpoint.
    CACHE_TTL_SECONDS: int = 900  # 15 minutes

    # Max parse-profile requests per client IP per minute (basic abuse guard).
    RATE_LIMIT_PER_MINUTE: int = 20

    # Pydantic settings config to auto-read .env file
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

# Global settings instance
settings = Settings()