"""
Integration tests for the sbfetcher API.

Requires a running server. Default base URL: http://localhost:3000
Override with the BASE_URL environment variable.

Install test dependencies (if not already present):
    pip install pytest requests websockets

Run all tests:
    pytest test.py -v

Run only the fast (non-browser) tests:
    pytest test.py -v -m "not browser"

Run against a remote host:
    BASE_URL=http://my-host:3000 pytest test.py -v
"""

import asyncio
import json
import os

import pytest
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:3000").rstrip("/")
WS_URL = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(path: str, **kwargs) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", timeout=60, **kwargs)


def post(path: str, **kwargs) -> requests.Response:
    return requests.post(f"{BASE_URL}{path}", timeout=60, **kwargs)


async def cdp_call(ws, msg_id: int, method: str, params: dict | None = None) -> dict:
    """Send one CDP command and return its reply, skipping events."""
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if reply.get("id") == msg_id:
            return reply


def run_cdp(fn):
    """Open a /cdp WebSocket, run fn(ws), and return its result."""
    websockets = pytest.importorskip("websockets")

    async def _run():
        async with websockets.connect(
            f"{WS_URL}/cdp",
            open_timeout=10,
            close_timeout=5,
            max_size=2**28,
        ) as ws:
            return await fn(ws)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_status_ok(self):
        r = get("/health")
        assert r.status_code == 200

    def test_response_fields(self):
        data = get("/health").json()
        assert data["status"] == "ok"
        assert data["mode"] == "pure-cdp"
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["browser_active"] is True


# ---------------------------------------------------------------------------
# Removed REST endpoints
# ---------------------------------------------------------------------------

class TestRemovedEndpoints:
    @pytest.mark.parametrize("path", ["/content", "/screenshot", "/chromium/content", "/v1"])
    def test_post_returns_404(self, path):
        r = post(path, json={"url": "https://example.com"})
        assert r.status_code == 404

    def test_metrics_returns_404(self):
        assert get("/metrics").status_code == 404


# ---------------------------------------------------------------------------
# WebSocket /cdp  (CDP proxy)
# ---------------------------------------------------------------------------

@pytest.mark.browser
class TestCDPWebSocket:
    def test_websocket_accepts_connection(self):
        """The /cdp endpoint should relay a basic CDP command."""
        result = run_cdp(lambda ws: cdp_call(ws, 1, "Browser.getVersion"))
        assert result.get("id") == 1
        assert "product" in result["result"]

    def test_shared_context_lifecycle(self):
        """
        new_context() as Playwright sends it: createBrowserContext is answered
        with the default context, pages open in it, and dispose closes them.
        """
        async def _flow(ws):
            ctx = await cdp_call(ws, 1, "Target.createBrowserContext", {"disposeOnDetach": True})
            ctx_id = ctx["result"]["browserContextId"]
            page = await cdp_call(ws, 2, "Target.createTarget",
                                  {"url": "about:blank", "browserContextId": ctx_id})
            target_id = page["result"]["targetId"]
            targets = await cdp_call(ws, 3, "Target.getTargets")
            dispose = await cdp_call(ws, 4, "Target.disposeBrowserContext",
                                     {"browserContextId": ctx_id})
            await asyncio.sleep(1)  # let Chrome close the page
            after = await cdp_call(ws, 5, "Target.getTargets")
            return ctx_id, target_id, targets, dispose, after

        ctx_id, target_id, targets, dispose, after = run_cdp(_flow)
        infos = {t["targetId"]: t for t in targets["result"]["targetInfos"]}
        # The new page lives in the default context, i.e. shares the persistent profile
        assert infos[target_id]["browserContextId"] == ctx_id
        assert dispose["result"] == {}
        assert target_id not in {t["targetId"] for t in after["result"]["targetInfos"]}

    def test_leftover_pages_closed_on_disconnect(self):
        """Pages a client opens but never closes are cleaned up when it disconnects."""
        async def _open(ws):
            ctx = await cdp_call(ws, 1, "Target.createBrowserContext")
            page = await cdp_call(ws, 2, "Target.createTarget",
                                  {"url": "about:blank",
                                   "browserContextId": ctx["result"]["browserContextId"]})
            return page["result"]["targetId"]

        target_id = run_cdp(_open)

        async def _list(ws):
            await asyncio.sleep(1)
            return await cdp_call(ws, 1, "Target.getTargets")

        after = run_cdp(_list)
        assert target_id not in {t["targetId"] for t in after["result"]["targetInfos"]}
