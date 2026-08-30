# Changelog

## 2026-08-30 — profile sections, deploy hardening

**Added**
- Experience / Education / Skills / Certifications / Languages / Volunteering via
  LinkedIn's SDUI lazy-card endpoint (`app/sdui_sections.py`). The profile page
  ships each section as an empty placeholder plus the `AsyncComponentRequest`
  the browser would POST; we replay it with `httpx`. No browser engine. See
  README → "How the sections endpoint was found".
- `additional_sections` (honors, projects, publications, …), `pronouns`,
  `connections`, `followers`, `employment_type` on roles, `about` from the
  AboveActivity card, top-card `location` fallback.
- `meta` block on every response: `fetched_at`, `elapsed_ms`, `cached`,
  `section_cards_fetched`.
- `include_sections` request flag (default `true`); intro-card-only path when `false`.
- `LINKEDIN_BROWSER_PLATFORM` (`windows` | `macos`): one browser identity used on
  every request (JSON API, document HTML, SDUI cards). Must match the browser
  the cookies were copied from.
- Error taxonomy: `404` unknown/not-visible profile, `503` session rejected or
  `999` bot block, `502` residual. Previously everything was a `502` that blamed
  the cookies.
- `scripts/smoke.sh [base_url] [profile_url]` end-to-end check; 25 offline tests.

**Fixed**
- `.dockerignore` excludes local capture files (HAR, saved HTML, card dumps) so
  session material can never be baked into the image.
- Sections path honours `LINKEDIN_COOKIE` (full jar) like the API path.
- Section cards fetched concurrently (max 4 in flight): ~2–4 s per uncached
  profile instead of ~11 s.

**Deploy**
- Railway service `Linkedin API Service`, region `asia-southeast1` (Singapore),
  deployed with `railway up` from a checkout (not GitHub-linked). Variables:
  `LINKEDIN_LI_AT_COOKIE`, `LINKEDIN_JSESSIONID`, `LINKEDIN_PROFILE_QUERY_ID`,
  `LINKEDIN_BROWSER_PLATFORM`, `ENABLE_DEBUG_ROUTES=false`.
