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
│   ├── sdui_sections.py     # Experience/Education/Skills/... via the SDUI card endpoint
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
      extract any Voyager API responses LinkedIn embeds in `<code>` blocks. This
      is the *legacy* (Ember) page shell; the current SDUI page has no such
      blocks, so this only helps if LinkedIn serves the old shell (it still does
      for some accounts/regions). Kept because it costs nothing and never scrapes
      the DOM.
3. **Parse** the normalized `included[]` graph into a typed `ProfileResponse`.
   The parser dispatches on the tail of each object's `$type`, so it handles both
   the classic (`...voyager.identity.profile.*`) and current dash
   (`...voyager.dash.identity.profile.*`) schemas, resolving dash's URN references
   (e.g. a position's company URN) against `included[]`.
4. **Sections** (`include_sections`, default `true`). The GraphQL card only
   carries the intro; LinkedIn moved Experience / Education / Skills /
   Certifications / Languages to **SDUI** (server-driven UI). The profile
   document ships every section as an *empty placeholder* plus, in its hydration
   blob, the exact `AsyncComponentRequest` the browser would POST to
   `/flagship-web/rsc-action/actions/component` to fill it. `app/sdui_sections.py`
   lifts those requests out of the HTML and replays them with `httpx` (one POST per
   card, ~9 per profile, at most 4 in flight), then parses the returned React-Server-
   Components flight payload: visible text is walked in render order and split
   into entries by the card's collection items. No JavaScript, no scrolling, no
   browser engine. Section fetches are best-effort: any failure logs and yields
   empty lists rather than failing the request.

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
{ "profile_url": "https://www.linkedin.com/in/<handle>/", "include_sections": true }
```
`include_sections` (default `true`) controls the SDUI section fetch. `false`
returns the intro card only (1-2 LinkedIn calls instead of ~11) and is cached
separately. Interactive docs: `GET /docs` (OpenAPI).
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
    { "title": "Engineer", "company_name": "Acme", "location": "Delhi, India",
      "start_date": "Jan 2022", "end_date": "Present", "description": "…",
      "employment_type": "Full-time" }
  ],
  "education": [
    { "institution": "IIT", "degree": "BTech", "field_of_study": "CS",
      "start_year": 2018, "end_year": 2022 }
  ],
  "skills": ["Python"],
  "volunteering": [ { "title": "Fellow", "company_name": "Some Org", "start_date": "Aug 2025", "end_date": "Jan 2026" } ],
  "certifications": ["AWS"],
  "languages": ["English"],
  "meta": { "fetched_at": "2026-08-30T10:00:00+00:00", "elapsed_ms": 3200, "cached": false,
            "sections_requested": true, "section_cards_fetched": ["profileCardsExperienceOnly", "..."] }
}
```
`meta` is provenance for the caller: when the data was fetched, how long LinkedIn
took, whether this response came from the TTL cache, and which section cards were
actually read (an empty list with `sections_requested: true` means the section
fetch was blocked and the intro card alone was returned).
Any field LinkedIn does not expose is returned as `null` or an empty list rather
than causing an error.

**Error codes**
| Code  | Meaning                                                              |
| ----- | ------------------------------------------------------------------- |
| `400` | Malformed LinkedIn profile URL.                                     |
| `404` | No profile for that handle: it does not exist, is not visible to the backing account, or LinkedIn soft-blocked the lookup. |
| `422` | Request body failed validation (e.g. missing `profile_url`).       |
| `429` | Per-IP rate limit exceeded.                                        |
| `500` | Credentials missing from server config.                            |
| `503` | LinkedIn rejected the session (expired/revoked cookies, `/health` says `expired`) or its anti-bot layer answered `999`. Refresh cookies / back off. |
| `502` | Every strategy failed for another reason (usually a stale `queryId`); the detail names the last status. |

### Debug endpoints (OFF by default)
The `GET /api/v1/debug/*` routes drive arbitrary Voyager requests using the
server's session cookie, so they are **disabled unless `ENABLE_DEBUG_ROUTES=true`**.
Enable them only for local debugging; they return `404` on a default/public deploy.

## If this returns `503` or `502` when you test it

`503` means the **session** is the problem (`li_at`/`JSESSIONID` expired or
revoked, or a `999` bot block); `502` usually means the GraphQL `queryId` rotated.
Both are inherent to a reverse-engineered integration, not bugs in the service.

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

## How the sections endpoint was found

The intro card was easy (a versioned GraphQL query). The sections were not, and
the path to them is the interesting part of this repo:

1. **The GraphQL hypothesis was wrong.** Four HAR captures of a profile page load
   contained zero `Position`/`Education`/`Skill` entities and no section
   `queryId`. The only `voyagerIdentityDashProfiles` call on the page is the
   nav-bar *self*-profile fetch. There is no section query to capture.
2. **The document HTML is also empty.** The authenticated page (`~950 KB`)
   server-renders only the intro card. Every section is an empty placeholder
   `<div id="profileCardsExperienceOnly<handle>">`, with no `<h2>` and no entry
   markup. So the sections are neither an XHR nor in the page.
3. **The placeholders carry their own fetch recipe.** The page's hydration blob
   (`window.__como_rehydration__`, a JSON array of React Server Components
   "flight" rows) contains, per placeholder, a
   `proto.sdui.actions.core.ReplaceComponent` whose `asyncContent` is a
   `proto.sdui.actions.core.AsyncComponentRequest`: `newComponentId`
   (`com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly`)
   plus `requestedArguments` (`vanityName`, `vieweeProfileId`, feature flags).
4. **The endpoint is in the client bundle.** The SDUI runtime builds
   `` `rsc-action/actions/component?componentId=${id}&sduiid=${id}` `` and POSTs
   `{clientArguments: sr(requestedArguments)}`; `/flagship-web` is the service
   base (a `500` there vs a `404` at the root confirmed it).
5. **The body has to be reshaped.** Posting the raw `RequestedArguments` returns
   `500`. The runtime's `sr()` drops `$type`/`requestedStateKeys` and adds
   `states: []`, `screenId: ""`, `knownTemplateIds: []`. With that, the endpoint
   returns `200` and a `~170 KB` flight payload holding the whole card.
6. **The headers that matter** are `csrf-token` (= unquoted `JSESSIONID`),
   `x-li-rsc-stream: true`, `x-li-page-instance` (lifted from the same page),
   `x-li-track` and a User-Agent / client-hints set that matches the browser the
   cookies were minted in. A mismatched fingerprint is treated as session
   hijacking and revokes the cookie globally; this bit cost several logins.
7. **Parsing** walks the React element tree in render order. Visible text is
   plain strings in `children` arrays; entries are the card's `initialItems`;
   section headings are `ProfileNullStateCardAnchor_<Section>` component keys;
   media captions are identified by a sibling `a11yText: "Thumbnail for …"`.
   Grouped multi-role companies come flat inside one item and are split on date
   lines.

Everything above is `httpx` only. No JavaScript is executed at any point.

## Known limitations

- **Unofficial API.** Voyager is private and undocumented. Endpoints, the
  `queryId`, and response shapes change without notice; expect periodic upkeep.
- **`queryId` rotation.** When GraphQL starts returning `4xx`/redirects, refresh
  `LINKEDIN_PROFILE_QUERY_ID` (see above). The legacy `profileView` endpoint is
  deprecated (`410 Gone`) and is only a best-effort fallback.
- **Cookie expiry & rate limiting.** `li_at`/`JSESSIONID` expire, and LinkedIn
  rate-limits/soft-blocks automated traffic (HTTP `429`/`999`), especially from
  datacenter IPs. Refresh cookies as needed; keep request volume modest.
- **Sections come from a rendered component tree, not entities.** The SDUI
  card payload is a React render tree, so `sdui_sections.py` classifies visible
  lines by shape (date ranges, `Company · Employment type`, location, grouped
  multi-role companies, `Issued <date>`). It is verified against real profiles
  but is view-coupled: a LinkedIn layout change can degrade a field (it never
  breaks the intro card). Skills return what the profile card shows (the top
  entries); the full list lives on `/details/skills/` and is not fetched.
- **Fingerprint consistency matters.** The User-Agent, `sec-ch-ua*` hints and
  `x-li-track` all describe the same browser the cookies were minted in. LinkedIn
  treats a cookie used from a "different" browser as session hijacking and revokes
  it globally; keep those headers in `linkedin_service.py` matched to the browser
  you copied the cookies from.
- **Visibility scope.** Only data visible to the logged-in account is returned;
  some fields depend on connection degree and the profile's privacy settings.
- **Terms of Service.** Automated access to LinkedIn may violate its ToS. This
  project is for educational/demonstration purposes.

## Hardening for volume (not implemented here, by design)

Reverse-engineered LinkedIn access is an adversarial target; single-machine,
single-IP operation *will* get flagged at volume. This submission runs on one
account and one IP on purpose (keep the test surface small). A real deployment
would layer on:

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
