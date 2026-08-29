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
│   ├── main.py              # FastAPI app + routes (UI, parse, health, debug)
│   ├── schemas.py           # Pydantic request/response models
│   ├── linkedin_service.py  # Voyager fetch + raw payload -> Profile mapping
│   ├── config.py            # env-based settings
│   └── static/index.html    # single-page frontend (calls the parse endpoint)
├── tests/                   # pytest, no network (MockTransport)
├── .env.example
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## Approach

LinkedIn's web app is backed by an internal JSON API under
`https://www.linkedin.com/voyager/api/...`. Authenticated browser sessions carry
two cookies — `li_at` (the session token) and `JSESSIONID` (also used as the
CSRF token). This service replays those cookies from server-side config to call
Voyager directly:

1. **Extract** the vanity handle from the input URL (`/in/<handle>`).
2. **Fetch** the normalized profile payload, trying three strategies in order and
   only accepting a response that actually contains a profile entity (a soft-block
   `200` with empty `included` is rejected, not returned as a hollow profile):
   1. **Primary — GraphQL:** `GET /voyager/api/graphql?variables=(vanityName:<handle>)&queryId=<queryId>`.
      The `queryId` is a versioned identifier LinkedIn rotates, so it is supplied
      via `LINKEDIN_PROFILE_QUERY_ID` (see setup) rather than hardcoded.
   2. **Fallback — legacy REST:** `GET /voyager/api/identity/profiles/<handle>/profileView`
      (deprecated — usually `410` — best-effort only).
   3. **Fallback — server-rendered Voyager JSON:** fetch the profile page and
      extract the Voyager API responses LinkedIn **embeds in the HTML** (inside
      `<code>` blocks). This is *not* HTML/DOM scraping — it reads the same
      normalized Voyager JSON, just delivered inline in the document. Still no
      browser engine.
3. **Parse** the normalized `included[]` graph into a typed `ProfileResponse`.
   The parser dispatches on the tail of each object's `$type`, so it handles both
   the classic (`...voyager.identity.profile.*`) and current dash
   (`...voyager.dash.identity.profile.*`) schemas, resolving dash's URN references
   (e.g. a position's company URN) against `included[]`.

### Getting the `queryId` (one-time, ~30s)

LinkedIn retired the old REST profile endpoint (it now returns `410 Gone`), and
the GraphQL endpoint requires a current `queryId`. To capture it:

1. Log into LinkedIn in your browser and open any profile.
2. Open DevTools (F12) → **Network** tab → filter for `graphql`.
3. Reload the page and click the request returning the profile JSON.
4. Copy the `queryId=voyagerIdentityDashProfiles.<hash>` value from its URL.
5. Paste it into `.env` as `LINKEDIN_PROFILE_QUERY_ID=...`.

## Setup

**Requires Python 3.10+** (the code uses `X | None` type syntax).

```bash
python -m venv venv
venv\Scripts\activate         # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env         # then fill in credentials (see below)
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/ for the UI, or http://127.0.0.1:8000/docs for Swagger.
Run tests with `pip install -r requirements-dev.txt && pytest`.

### Required environment variables (`.env`)

| Variable                     | Description                                              |
| ---------------------------- | ------------------------------------------------------- |
| `LINKEDIN_COOKIE`            | **Recommended.** The full raw `Cookie:` header from a logged-in browser request. Includes routing/security cookies (`lidc`, `bcookie`, `liap`) that LinkedIn requires on protected endpoints. Overrides the two vars below. |
| `LINKEDIN_LI_AT_COOKIE`      | `li_at` cookie value (used only if `LINKEDIN_COOKIE` is blank). |
| `LINKEDIN_JSESSIONID`        | `JSESSIONID` cookie value, e.g. `ajax:1234567890` (used only if `LINKEDIN_COOKIE` is blank). |
| `LINKEDIN_PROFILE_QUERY_ID`  | Current GraphQL `queryId` (see above). Blank → falls back to legacy REST/HTML. |
| `LINKEDIN_PROXY`             | Optional outbound proxy (e.g. residential) — `http://user:pass@host:port`. Blank = direct. |
| `ENABLE_DEBUG_ROUTES`        | Expose `/api/v1/debug/*` (drives Voyager with the server cookie). Keep `false` in production. |
| `CACHE_TTL_SECONDS`          | Per-handle response cache TTL (default `900`).          |
| `RATE_LIMIT_PER_MINUTE`      | Per-IP request cap (default `20`).                      |
| `REQUEST_TIMEOUT`            | Upstream request timeout in seconds (default `15.0`).   |

Secrets live only in `.env`, which is git-ignored and never committed. Do **not**
set `PORT` on the host — the container binds the platform-provided `$PORT`.

## Run with Docker

```bash
docker build -t linkedin-api-service .
docker run -p 8000:8000 --env-file .env linkedin-api-service
```

## API

### `GET /`
Serves the single-page UI (paste a URL, see the parsed profile).

### `GET /health`
Liveness + session status → `{"status": "ok", "service": "LinkedIn Parser API", "linkedin_session": "ok" | "expired" | "not_configured" | "unreachable"}`.
Use `linkedin_session` to tell at a glance whether the backing cookies still work
(it leaks no cookie material). `expired` is the usual reason `parse-profile` 502s.

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
| `429` | Per-IP rate limit exceeded.                                        |
| `502` | All fetch strategies failed or returned no usable profile (expired cookies / stale `queryId` / soft block). |

### Debug endpoints (OFF by default)
The `GET /api/v1/debug/*` routes drive arbitrary Voyager requests using the
server's session cookie, so they are **disabled unless `ENABLE_DEBUG_ROUTES=true`**.
Enable them only for local debugging; they return `404` on a default/public deploy.

## If this returns `502` when you test it

The most likely reason is an **expired session** — `li_at`/`JSESSIONID` cookies
expire and LinkedIn rotates the GraphQL `queryId` every few weeks. This is
inherent to a reverse-engineered integration, not a bug.

1. **Check** `GET /health`. If `linkedin_session` is `expired` (or `not_configured`),
   that's the cause.
2. **Refresh the cookie.** In a logged-in browser, open DevTools → Network → click
   any `voyager` request → copy the full **Cookie** request header. Set it as
   `LINKEDIN_COOKIE` in your environment (it includes `li_at`, `JSESSIONID`,
   `lidc`, etc.).
3. **Refresh the `queryId`** if needed. DevTools → search `voyagerIdentityDashProfiles`
   → copy the current `voyagerIdentityDashProfiles.<hash>` → set as
   `LINKEDIN_PROFILE_QUERY_ID`.
4. **Confirm** `GET /health` now reports `linkedin_session: ok`, then retry
   `parse-profile`.

Responses are cached per handle (`CACHE_TTL_SECONDS`, default 15 min) and requests
are rate-limited per IP (`RATE_LIMIT_PER_MINUTE`, default 20) to avoid hammering —
and restricting — the backing LinkedIn account from a public endpoint.

## Known limitations

- **Unofficial API.** Voyager is private and undocumented. Endpoints, the
  `queryId`, and response shapes change without notice; expect periodic upkeep.
- **`queryId` rotation.** When GraphQL starts returning `4xx`/redirects, refresh
  `LINKEDIN_PROFILE_QUERY_ID` (see above). The legacy `profileView` endpoint is
  deprecated (`410 Gone`) and is only a best-effort fallback.
- **Cookie expiry & rate limiting.** `li_at`/`JSESSIONID` expire, and LinkedIn
  rate-limits/soft-blocks automated traffic (HTTP `429`/`999`), especially from
  datacenter IPs. Refresh cookies as needed; keep request volume modest.
- **Profile *sections* (experience, education, skills, certs, languages) are
  limited.** LinkedIn migrated the profile detail sections to **SDUI** (Server-
  Driven UI / React Server Components, served from `flagship-web/rsc-action`),
  which returns a serialized *component tree* rather than queryable entities. The
  clean Voyager GraphQL path reliably returns the **intro card** — name, headline,
  location, about, profile image, current role. The full section lists require
  parsing the SDUI render tree (fragile, view-coupled) and are out of scope here.
  The parser already handles `Position`/`Education`/`Skill` entities, so if a
  future query returns them as data, they populate automatically.
- **Visibility scope.** Only data visible to the logged-in account is returned;
  some fields depend on connection degree and the profile's privacy settings.
- **Terms of Service.** Automated access to LinkedIn may violate its ToS. This
  project is for educational/demonstration purposes.

## Production scaling (how this is run reliably at volume)

Reverse-engineered LinkedIn access is an adversarial target; single-machine,
single-IP operation *will* get flagged. A production deployment layers on:

- **Rotating residential/mobile proxies**, rotated **per session** (not per
  request, so cookies stay sticky). Empirically ~**20–30 profile requests per IP
  per hour** is the safe ceiling before rate-limiting. Wired in here via
  `LINKEDIN_PROXY` (httpx-native — a proxy is network routing, not a browser).
- **A pool of aged, warmed-up accounts** with managed cookie sessions, rotated
  and health-checked (`/health` derives session validity). Fresh accounts get
  force-logged-out fastest.
- **Browser-grade TLS/HTTP-2 fingerprints.** Plain `httpx` has a non-browser JA3
  signature LinkedIn can detect. Libraries like `curl_cffi`/`uTLS` impersonate
  Chrome. *Intentionally not used here* — it would break the "httpx only, no
  browser" constraint — but it is the standard production mitigation.
- **Request budgeting + caching** (implemented: per-IP rate limit + per-handle
  TTL cache) to bound volume and protect the backing accounts.
- **`queryId`/cookie rotation automation** — detect `expired` via `/health` and
  swap in fresh credentials without redeploying.
