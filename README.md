# linkedin-api-service

A FastAPI service that accepts a LinkedIn profile URL and returns the profile's
public information as structured JSON. It is a **purely reverse-engineered**
solution: it talks directly to LinkedIn's private Voyager HTTP endpoints using
`httpx` — no headless browser, Selenium, or Playwright involved.

## Layout

```
linkedin-api-service/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + routes
│   ├── schemas.py           # Pydantic request/response models
│   ├── linkedin_service.py  # Voyager fetch + raw payload -> Profile mapping
│   └── config.py            # env-based settings
├── .env.example
├── Dockerfile
├── requirements.txt
└── README.md
```

## Approach

LinkedIn's web app is backed by an internal JSON API under
`https://www.linkedin.com/voyager/api/...`. Authenticated browser sessions carry
two cookies — `li_at` (the session token) and `JSESSIONID` (also used as the
CSRF token). This service replays those cookies from server-side config to call
Voyager directly:

1. **Extract** the vanity handle from the input URL (`/in/<handle>`).
2. **Fetch** the normalized profile payload:
   - **Primary — GraphQL:** `GET /voyager/api/graphql?variables=(vanityName:<handle>)&queryId=<queryId>`.
     The `queryId` is a versioned identifier LinkedIn rotates, so it is supplied
     via `LINKEDIN_PROFILE_QUERY_ID` (see setup) rather than hardcoded.
   - **Fallback — legacy REST:** `GET /voyager/api/identity/profiles/<handle>/profileView`
     (used automatically if no `queryId` is configured).
3. **Parse** the normalized `included[]` graph into a typed `ProfileResponse`.
   The parser dispatches on the tail of each object's `$type`, so it handles both
   the classic (`...voyager.identity.profile.*`) and current dash
   (`...voyager.dash.identity.profile.*`) schemas.

### Getting the `queryId` (one-time, ~30s)

LinkedIn retired the old REST profile endpoint (it now returns `410 Gone`), and
the GraphQL endpoint requires a current `queryId`. To capture it:

1. Log into LinkedIn in your browser and open any profile.
2. Open DevTools (F12) → **Network** tab → filter for `graphql`.
3. Reload the page and click the request returning the profile JSON.
4. Copy the `queryId=voyagerIdentityDashProfiles.<hash>` value from its URL.
5. Paste it into `.env` as `LINKEDIN_PROFILE_QUERY_ID=...`.

## Setup

```bash
python -m venv venv
venv\Scripts\activate         # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env         # then fill in credentials (see below)
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive Swagger docs.

### Required environment variables (`.env`)

| Variable                     | Description                                              |
| ---------------------------- | ------------------------------------------------------- |
| `LINKEDIN_LI_AT_COOKIE`      | `li_at` cookie value from a logged-in session.          |
| `LINKEDIN_JSESSIONID`        | `JSESSIONID` cookie value (e.g. `ajax:1234567890`).     |
| `LINKEDIN_PROFILE_QUERY_ID`  | Current GraphQL `queryId` (see above). Optional; blank falls back to legacy REST. |
| `REQUEST_TIMEOUT`            | Upstream request timeout in seconds (default `15.0`).   |

Secrets live only in `.env`, which is git-ignored and never committed.

## Run with Docker

```bash
docker build -t linkedin-api-service .
docker run -p 8000:8000 --env-file .env linkedin-api-service
```

## API

### `GET /`
Liveness check → `{"status": "ok", "service": "LinkedIn Parser API"}`.

### `POST /api/v1/parse-profile`
**Request**
```json
{ "profile_url": "https://www.linkedin.com/in/<handle>/" }
```
**Response `200` — `ProfileResponse`**
```json
{
  "profile_url": "https://www.linkedin.com/in/<handle>/",
  "profile_handle": "<handle>",
  "full_name": "Diya Singh",
  "first_name": "Diya",
  "last_name": "Singh",
  "headline": "Software Engineer",
  "location": { "city": null, "country": null, "raw_location": "Delhi, India" },
  "about": "…",
  "profile_image_url": "https://media.licdn.com/…",
  "experience": [
    { "title": "Engineer", "company_name": "Acme", "location": "Delhi",
      "start_date": "2022-01", "end_date": "Present", "description": null }
  ],
  "education": [
    { "institution": "IIT", "degree": "BTech", "field_of_study": "CS",
      "start_year": 2018, "end_year": 2022 }
  ],
  "skills": ["Python"],
  "certifications": ["AWS"],
  "languages": ["English"]
}
```
Any field LinkedIn does not expose is returned as `null` or an empty list rather
than causing an error.

**Error codes**
| Code  | Meaning                                                              |
| ----- | ------------------------------------------------------------------- |
| `400` | Malformed LinkedIn profile URL.                                     |
| `422` | Request body failed validation (e.g. missing `profile_url`).       |
| `500` | Credentials missing from server config.                            |
| `502` | LinkedIn rejected both fetch strategies (expired cookies / stale `queryId`). |

### Debug endpoints (local diagnostics — disable before public deploy)
- `GET /api/v1/debug/auth` — verifies the configured cookies still authenticate.
- `GET /api/v1/debug/probe?profile_url=…&variant=dash|profileView&follow=false` —
  returns the raw status/headers/body from a candidate endpoint.

## Known limitations

- **Unofficial API.** Voyager is private and undocumented. Endpoints, the
  `queryId`, and response shapes change without notice; expect periodic upkeep.
- **`queryId` rotation.** When GraphQL starts returning `4xx`/redirects, refresh
  `LINKEDIN_PROFILE_QUERY_ID` (see above). The legacy `profileView` endpoint is
  deprecated (`410 Gone`) and is only a best-effort fallback.
- **Cookie expiry & rate limiting.** `li_at`/`JSESSIONID` expire, and LinkedIn
  rate-limits/soft-blocks automated traffic (HTTP `429`/`999`), especially from
  datacenter IPs. Refresh cookies as needed; keep request volume modest.
- **Visibility scope.** Only data visible to the logged-in account is returned;
  some fields depend on connection degree and the profile's privacy settings.
- **Terms of Service.** Automated access to LinkedIn may violate its ToS. This
  project is for educational/demonstration purposes.
