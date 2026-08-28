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

    # Pydantic settings config to auto-read .env file
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

# Global settings instance
settings = Settings()