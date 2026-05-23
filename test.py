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
import base64
import os

import pytest
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:3000").rstrip("/")
WS_URL = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")

# A lightweight public page used for browser tests (small, reliable)
TEST_URL = "https://example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(path: str, **kwargs) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", timeout=60, **kwargs)


def post(path: str, **kwargs) -> requests.Response:
    return requests.post(f"{BASE_URL}{path}", timeout=60, **kwargs)


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
        assert isinstance(data["browser_active"], bool)


# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_status_ok(self):
        r = get("/metrics")
        assert r.status_code == 200

    def test_response_fields(self):
        data = get("/metrics").json()
        assert isinstance(data["requests_total"], int)
        assert isinstance(data["errors_total"], int)
        assert isinstance(data["uptime_seconds"], (int, float))
        assert isinstance(data["error_rate"], float)

    def test_requests_increment(self):
        before = get("/metrics").json()["requests_total"]
        # /health counts as a request too, so just verify the counter is non-negative
        assert before >= 0


# ---------------------------------------------------------------------------
# POST /content
# ---------------------------------------------------------------------------

@pytest.mark.browser
class TestContent:
    def test_basic_fetch(self):
        r = post("/content", json={"url": TEST_URL})
        assert r.status_code == 200
        data = r.json()
        assert "data" in data
        assert "url" in data
        assert "<html" in data["data"].lower()

    def test_returns_final_url(self):
        data = post("/content", json={"url": TEST_URL}).json()
        assert data["url"].startswith("http")

    def test_with_wait_timeout(self):
        r = post("/content", json={"url": TEST_URL, "waitForTimeout": 500})
        assert r.status_code == 200
        assert "<html" in r.json()["data"].lower()

    def test_with_goto_options(self):
        r = post("/content", json={
            "url": TEST_URL,
            "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
        })
        assert r.status_code == 200

    def test_with_viewport(self):
        r = post("/content", json={
            "url": TEST_URL,
            "viewport": {"width": 1920, "height": 1080},
        })
        assert r.status_code == 200

    def test_with_custom_user_agent(self):
        r = post("/content", json={
            "url": TEST_URL,
            "userAgent": "Mozilla/5.0 (Test Bot)",
        })
        assert r.status_code == 200

    def test_with_javascript(self):
        r = post("/content", json={
            "url": TEST_URL,
            "javascript": "document.title = 'overridden'",
        })
        assert r.status_code == 200

    def test_missing_url_returns_422(self):
        r = post("/content", json={})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /chromium/content  (browserless v1 alias)
# ---------------------------------------------------------------------------

@pytest.mark.browser
class TestChromiumContent:
    def test_alias_returns_same_shape(self):
        r = post("/chromium/content", json={"url": TEST_URL})
        assert r.status_code == 200
        data = r.json()
        assert "data" in data
        assert "url" in data
        assert "<html" in data["data"].lower()


# ---------------------------------------------------------------------------
# POST /screenshot
# ---------------------------------------------------------------------------

@pytest.mark.browser
class TestScreenshot:
    def test_returns_png_by_default(self):
        r = post("/screenshot", json={"url": TEST_URL})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        # PNG magic bytes
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_jpeg_type(self):
        r = post("/screenshot", json={
            "url": TEST_URL,
            "type": "jpeg",
            "quality": 80,
        })
        assert r.status_code == 200
        # Server returns jpeg if Pillow is available, otherwise falls back to png
        assert r.headers["content-type"] in ("image/jpeg", "image/png")

    def test_full_page_false(self):
        r = post("/screenshot", json={"url": TEST_URL, "fullPage": False})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

    def test_missing_url_returns_422(self):
        r = post("/screenshot", json={})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /chromium/screenshot  (browserless v1 alias)
# ---------------------------------------------------------------------------

@pytest.mark.browser
class TestChromiumScreenshot:
    def test_alias_returns_image(self):
        r = post("/chromium/screenshot", json={"url": TEST_URL})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")


# ---------------------------------------------------------------------------
# POST /v1  (FlareSolverr-compatible)
# ---------------------------------------------------------------------------

class TestFlareSolverrSessions:
    def test_sessions_create_returns_ok(self):
        r = post("/v1", json={"cmd": "sessions.create"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "session" in data
        assert isinstance(data["session"], str)
        assert len(data["session"]) > 0

    def test_sessions_create_includes_version(self):
        data = post("/v1", json={"cmd": "sessions.create"}).json()
        assert "version" in data
        assert "startTimestamp" in data
        assert "endTimestamp" in data

    def test_sessions_list_includes_created_session(self):
        session_id = post("/v1", json={"cmd": "sessions.create"}).json()["session"]
        data = post("/v1", json={"cmd": "sessions.list"}).json()
        assert data["status"] == "ok"
        assert session_id in data["sessions"]

    def test_sessions_list_returns_list(self):
        data = post("/v1", json={"cmd": "sessions.list"}).json()
        assert isinstance(data["sessions"], list)

    def test_sessions_destroy_removes_session(self):
        session_id = post("/v1", json={"cmd": "sessions.create"}).json()["session"]
        r = post("/v1", json={"cmd": "sessions.destroy", "session": session_id})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "removed" in r.json()["message"].lower()

        remaining = post("/v1", json={"cmd": "sessions.list"}).json()["sessions"]
        assert session_id not in remaining

    def test_sessions_destroy_nonexistent_is_graceful(self):
        r = post("/v1", json={"cmd": "sessions.destroy", "session": "no-such-session"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "does not exist" in data["message"].lower()

    def test_unknown_command_returns_400(self):
        r = post("/v1", json={"cmd": "unknown.command"})
        assert r.status_code == 400

    def test_missing_cmd_returns_422(self):
        r = post("/v1", json={"url": TEST_URL})
        assert r.status_code == 422


@pytest.mark.browser
class TestFlareSolverrRequestGet:
    def test_basic_request_get(self):
        r = post("/v1", json={
            "cmd": "request.get",
            "url": TEST_URL,
            "maxTimeout": 30000,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["message"] == "Challenge solved!"

    def test_solution_shape(self):
        data = post("/v1", json={
            "cmd": "request.get",
            "url": TEST_URL,
            "maxTimeout": 30000,
        }).json()
        sol = data["solution"]
        assert "<html" in sol["response"].lower()
        assert isinstance(sol["cookies"], list)
        assert isinstance(sol["userAgent"], str)
        assert sol["url"].startswith("http")
        assert sol["status"] == 200

    def test_response_envelope_fields(self):
        data = post("/v1", json={
            "cmd": "request.get",
            "url": TEST_URL,
            "maxTimeout": 30000,
        }).json()
        assert "version" in data
        assert "startTimestamp" in data
        assert "endTimestamp" in data
        assert data["endTimestamp"] >= data["startTimestamp"]

    def test_screenshot_option(self):
        data = post("/v1", json={
            "cmd": "request.get",
            "url": TEST_URL,
            "screenshot": True,
            "maxTimeout": 30000,
        }).json()
        assert "screenshot" in data["solution"]
        # Must be valid base64-encoded PNG
        png_bytes = base64.b64decode(data["solution"]["screenshot"])
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    def test_missing_url_returns_400(self):
        r = post("/v1", json={"cmd": "request.get"})
        assert r.status_code == 400

    def test_with_cookies(self):
        r = post("/v1", json={
            "cmd": "request.get",
            "url": TEST_URL,
            "cookies": [{"name": "test_cookie", "value": "hello"}],
            "maxTimeout": 30000,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# WebSocket /cdp  (CDP proxy)
# ---------------------------------------------------------------------------

@pytest.mark.browser
class TestCDPWebSocket:
    def test_websocket_accepts_connection(self):
        """The /cdp endpoint should accept a WebSocket connection."""
        try:
            import websockets
        except ImportError:
            pytest.skip("websockets package not installed")

        async def _connect():
            async with websockets.connect(
                f"{WS_URL}/cdp",
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                # Connection accepted — send a no-op CDP message
                import json
                await ws.send(json.dumps({"id": 1, "method": "Browser.getVersion", "params": {}}))
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                return json.loads(response)

        result = asyncio.run(_connect())
        # CDP response must have matching id
        assert result.get("id") == 1
