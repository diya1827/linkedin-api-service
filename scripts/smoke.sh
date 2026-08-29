#!/usr/bin/env bash
# Smoke test against a running instance. Usage:
#   scripts/smoke.sh                      # local (http://localhost:8000)
#   scripts/smoke.sh https://your.host    # deployed
# Makes at most 4 LinkedIn-backed calls (two are served from cache / are 4xx).
set -u
BASE="${1:-http://localhost:8000}"
P="${2:-https://www.linkedin.com/in/diya-singh-478988269/}"
pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
post() { curl -s -m 90 -w '\n%{http_code}' -X POST "$BASE/api/v1/parse-profile" -H 'Content-Type: application/json' -d "$1"; }

echo "== $BASE"
h=$(curl -s -m 20 "$BASE/health"); echo "  health: $h"
echo "$h" | grep -q '"linkedin_session":"ok"' && ok "session ok" || bad "session not ok (set cookies / check /health)"
curl -s -o /dev/null -w '%{http_code}' "$BASE/docs" | grep -q 200 && ok "/docs served" || bad "/docs missing"

r=$(post '{"profile_url":"https://google.com/x"}'); [ "${r##*$'\n'}" = 400 ] && ok "bad URL -> 400" || bad "bad URL -> ${r##*$'\n'}"
r=$(post '{"profile_url":"https://www.linkedin.com/in/this-handle-does-not-exist-9f8e7d6c5/"}'); [ "${r##*$'\n'}" = 404 ] && ok "unknown handle -> 404" || bad "unknown handle -> ${r##*$'\n'}"

r=$(post "{\"profile_url\":\"$P\"}"); code="${r##*$'\n'}"; body="${r%$'\n'*}"
[ "$code" = 200 ] && ok "parse-profile -> 200" || { bad "parse-profile -> $code: $(echo "$body" | head -c 200)"; }
if [ "$code" = 200 ]; then
  python3 - "$body" <<'PY'
import json,sys
d=json.loads(sys.argv[1]); m=d.get("meta") or {}
def chk(c,msg): print(("  PASS  " if c else "  FAIL  ")+msg)
chk(bool(d.get("full_name")) and d["full_name"]!=d["profile_handle"], f"name: {d.get('full_name')}")
chk(bool(d.get("headline")), f"headline: {(d.get('headline') or '')[:60]}")
chk(bool(d.get("profile_image_url")), "profile image present")
chk(bool(d.get("location")), f"location: {(d.get('location') or {}).get('raw_location')}")
chk(len(d.get("experience",[]))>0, f"experience: {len(d.get('experience',[]))} roles, first = {(d.get('experience') or [{}])[0].get('title')} @ {(d.get('experience') or [{}])[0].get('company_name')}")
chk(all(e.get("start_date") for e in d.get("experience",[])), "every role has a start date")
chk(len(d.get("education",[]))>0, f"education: {[e.get('institution') for e in d.get('education',[])]}")
print(f"  INFO  skills={len(d.get('skills',[]))} certs={len(d.get('certifications',[]))} langs={len(d.get('languages',[]))} volunteering={len(d.get('volunteering',[]))} extra={list((d.get('additional_sections') or {}).keys())} pronouns={d.get('pronouns')} connections={d.get('connections')} followers={d.get('follower_count')}")
chk(m.get("section_cards_fetched"), f"section cards fetched: {len(m.get('section_cards_fetched',[]))} in {m.get('elapsed_ms')} ms")
PY
  r2=$(post "{\"profile_url\":\"$P\"}"); echo "$r2" | grep -q '"cached":true' && ok "second call served from cache" || bad "second call not cached"
fi
echo "== $pass passed, $fail failed"
