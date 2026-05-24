#!/usr/bin/env bash
# Manual curl commands for sbfetcher API.
# Paste any block directly into a terminal to see raw output.
# Override the base URL:  BASE_URL=http://my-host:3000

BASE_URL="${BASE_URL:-http://localhost:3000}"
URL="https://bot.sannysoft.com"   # "https://nowsecure.nl/#relax"

# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
curl -s "$BASE_URL/health" | jq .

# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------
curl -s "$BASE_URL/metrics" | jq .

# ---------------------------------------------------------------------------
# POST /content
# ---------------------------------------------------------------------------
curl -s -X POST "$BASE_URL/content" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$URL\"}" | jq '{url, length: (.data | length)}'

# with extra options
curl -s -X POST "$BASE_URL/content" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$URL\", \"waitForTimeout\": 1000, \"javascript\": \"document.title\"}" | jq '{url, length: (.data | length)}'

# ---------------------------------------------------------------------------
# POST /screenshot  (save to ./snapshot.png)
# ---------------------------------------------------------------------------
curl -s -X POST "$BASE_URL/screenshot" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$URL\"}" \
  -o ./snapshot.png && echo "Saved ./snapshot.png ($(wc -c < ./snapshot.png) bytes)"

# ---------------------------------------------------------------------------
# POST /v1 — sessions.create
# ---------------------------------------------------------------------------
curl -s -X POST "$BASE_URL/v1" \
  -H "Content-Type: application/json" \
  -d '{"cmd": "sessions.create"}' | jq .

# POST /v1 — sessions.list
curl -s -X POST "$BASE_URL/v1" \
  -H "Content-Type: application/json" \
  -d '{"cmd": "sessions.list"}' | jq .

# POST /v1 — sessions.destroy  (replace SESSION_ID)
curl -s -X POST "$BASE_URL/v1" \
  -H "Content-Type: application/json" \
  -d '{"cmd": "sessions.destroy", "session": "SESSION_ID"}' | jq .

# ---------------------------------------------------------------------------
# POST /v1 — request.get
# ---------------------------------------------------------------------------
curl -s -X POST "$BASE_URL/v1" \
  -H "Content-Type: application/json" \
  -d "{\"cmd\": \"request.get\", \"url\": \"$URL\", \"maxTimeout\": 30000}" \
  | jq '{status, message, version, url: .solution.url, cookies: (.solution.cookies | length), ua: .solution.userAgent}'

# with screenshot (base64 PNG in .solution.screenshot)
curl -s -X POST "$BASE_URL/v1" \
  -H "Content-Type: application/json" \
  -d "{\"cmd\": \"request.get\", \"url\": \"$URL\", \"screenshot\": true, \"maxTimeout\": 30000}" \
  | jq '{status, has_screenshot: (.solution.screenshot != null)}'
