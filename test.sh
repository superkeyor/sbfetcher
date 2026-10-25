#!/usr/bin/env bash
# Manual commands for the sbfetcher API.
# Paste any block directly into a terminal to see raw output.
# Override the base URL:  BASE_URL=http://my-host:3000

BASE_URL="${BASE_URL:-http://localhost:3000}"
WS_URL="${BASE_URL/http/ws}"

# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
curl -s "$BASE_URL/health" | jq .

# ---------------------------------------------------------------------------
# WS /cdp — Browser.getVersion through the relay
# ---------------------------------------------------------------------------
python3 - "$WS_URL/cdp" <<'EOF'
import asyncio, json, sys
import websockets

async def main(url):
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        print(json.dumps(json.loads(await ws.recv()), indent=2))

asyncio.run(main(sys.argv[1]))
EOF

# ---------------------------------------------------------------------------
# WS /cdp — fetch a page the way changedetection.io does (needs: pip install playwright)
# ---------------------------------------------------------------------------
python3 - "$WS_URL/cdp" "https://www.researchgate.net/profile/John-Mace-2" <<'EOF'
import asyncio, sys
from playwright.async_api import async_playwright

async def main(cdp_url, url):
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        context = await browser.new_context()
        page = await context.new_page()
        resp = await page.goto(url, wait_until="domcontentloaded")
        print("status:", resp.status if resp else None)
        print("title: ", await page.title())
        print("ua:    ", await page.evaluate("navigator.userAgent"))
        print("platform:", await page.evaluate("[navigator.platform, navigator.userAgentData && navigator.userAgentData.platform]"))
        print("cf_clearance:", any(c["name"] == "cf_clearance" for c in await context.cookies()))
        await context.close()
        await browser.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
EOF
