"""
SeleniumBase Pure CDP Mode + FastAPI server.

Uses seleniumbase.cdp_driver (async CDP, no WebDriver/chromedriver) for maximum
stealth — Chrome is driven directly via the Chrome DevTools Protocol.

Endpoints (browserless v1/v2 compatible):
  POST /content              -> { url, [...options] } -> { data: "<html>..." }
  POST /screenshot           -> { url, [...options] } -> PNG bytes
  POST /chromium/content     -> alias for /content (browserless v1)
  POST /chromium/screenshot  -> alias for /screenshot (browserless v1)
  GET  /health               -> { status: "ok", ... }
  GET  /metrics              -> basic stats (requests, errors, uptime)

FlareSolverr-compatible endpoint:
  POST /v1  -> { cmd: "request.get", url, [maxTimeout, cookies, screenshot] }
           -> { status, message, solution: { url, response, cookies, userAgent, screenshot? } }
  POST /v1  -> { cmd: "sessions.create" }  -> { session }
  POST /v1  -> { cmd: "sessions.list" }    -> { sessions }
  POST /v1  -> { cmd: "sessions.destroy", session }

changedetection.io integration:
  FlareSolverr path: set FLARESOLVERR_URL=http://<host>:3000
                     use "FlareSolverr" fetch method per watch
"""

import asyncio
import base64
import io
import logging
import os
import tempfile
import time
import json
import urllib.request
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from typing import Any

import mycdp.network
import mycdp.page
import websockets
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from seleniumbase import cdp_driver

# FlareSolverr version string reported in /v1 responses
FLARESOLVERR_VERSION = "v3.3.21"

# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------

LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()
HOST         = os.getenv("HOST", "0.0.0.0")
PORT         = int(os.getenv("PORT", "3000"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "30"))      # seconds
WAIT_AFTER   = float(os.getenv("WAIT_AFTER_LOAD", "1"))  # extra settle time (seconds)
WINDOW_W     = int(os.getenv("WINDOW_WIDTH", "1280"))
WINDOW_H     = int(os.getenv("WINDOW_HEIGHT", "900"))
CHROME_ARGS  = os.getenv("EXTRA_CHROME_ARGS", "")        # space-separated extra flags
# Set HEADED=true to run Chrome in a virtual Xvfb display (useful for debugging)
HEADED       = os.getenv("HEADED", "false").lower() in ("1", "true", "yes")
# Optional persistent Chrome profile dir; empty string = ephemeral temp profile each restart
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "") or None

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sbfetcher")

# ---------------------------------------------------------------------------
# CDP browser state
# Pure CDP Mode: one persistent driver + tab, serialized via asyncio.Lock.
# cdp_driver speaks directly to Chrome over CDP — no WebDriver, no chromedriver.
# ---------------------------------------------------------------------------

_cdp_driver: Any = None        # cdp_driver instance (the browser process)
_tab: Any = None               # active CDP tab
_driver_lock = asyncio.Lock()
_xvfb_proc: Any = None         # Xvfb subprocess (only when HEADED=True)
_chrome_cdp_ws_url: str | None = None  # CDP WebSocket endpoint (ws://127.0.0.1:PORT/devtools/browser/UUID)
_chrome_debug_port: int | None = None  # Chrome remote debugging port (for URL refresh)

# stats
_stats: dict[str, Any] = {
    "requests": 0,
    "errors": 0,
    "start_time": time.time(),
}

# FlareSolverr session store: session_id -> { created_at }
_sessions: dict[str, dict] = {}


# Stealth JS injected before ANY page script runs (via Page.addScriptToEvaluateOnNewDocument).
# This patches the most common Cloudflare/bot-detection vectors at the Chrome level.
_STEALTH_JS = """
// Remove WebDriver automation flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
// Ensure window.chrome.runtime exists (absent in headless mode)
if (!window.chrome) { window.chrome = {}; }
if (!window.chrome.runtime) { window.chrome.runtime = {}; }
// Fix Notification permissions query (headless returns 'denied' for 'notifications')
const __origPermQuery__ = window.navigator.permissions.query.bind(navigator.permissions);
window.navigator.permissions.query = (p) =>
    p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : __origPermQuery__(p);
// Set realistic language list
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en'], configurable: true});
// Spoof non-zero plugin count (headless Chrome has 0 plugins)
if (navigator.plugins.length === 0) {
    Object.defineProperty(navigator, 'plugins', {
        get: () => { const p = [1,2,3]; p.refresh=()=>{}; return p; },
        configurable: true,
    });
}
"""


async def _build_cdp_browser() -> tuple[Any, Any]:
    """
    Launch a new Pure CDP Mode browser and return (driver, tab).
    Extra Chrome flags from EXTRA_CHROME_ARGS are forwarded to the browser.
    """
    extra = CHROME_ARGS.split() if CHROME_ARGS else []
    # cdp_driver.start_async() launches Chrome with CDP enabled.
    # no_sandbox=True is required inside Docker containers.
    # agent: Windows Chrome UA is less suspicious than Linux/aarch64.
    driver = await cdp_driver.start_async(
        headless=not HEADED,
        no_sandbox=True,
        agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        lang="en-US",
        # browser_executable_path can be set if Chrome is not on PATH;
        # the Dockerfile symlinks chromium-browser onto PATH.
        user_data_dir=CHROME_USER_DATA_DIR,
        extra_chromium_args=extra,
    )
    tab = await driver.get("about:blank")
    await tab.set_window_size(0, 0, WINDOW_W, WINDOW_H)
    # Inject stealth patches that run before every page's own scripts.
    try:
        await tab._send_oneshot(
            mycdp.page.add_script_to_evaluate_on_new_document(source=_STEALTH_JS)
        )
        log.debug("Stealth JS injected via addScriptToEvaluateOnNewDocument")
    except Exception as _e:
        log.warning("Stealth JS injection failed (non-fatal): %s", _e)
    global _chrome_cdp_ws_url, _chrome_debug_port
    _http_url = driver.get_endpoint_url()  # returns http://127.0.0.1:PORT
    _chrome_debug_port = urlparse(_http_url).port
    def _fetch_ws_url_startup():
        with urllib.request.urlopen(f"{_http_url.rstrip('/')}/json/version", timeout=5) as _r:
            return json.loads(_r.read()).get("webSocketDebuggerUrl")
    _chrome_cdp_ws_url = await asyncio.to_thread(_fetch_ws_url_startup)
    log.info("Pure CDP browser started (no WebDriver/chromedriver)")
    log.info("Chrome CDP WebSocket endpoint: %s", _chrome_cdp_ws_url)
    return driver, tab


async def _get_or_restart_browser() -> Any:
    """
    Return the active CDP tab. If the browser has crashed, restart it.
    Must be called while holding _driver_lock.
    """
    global _cdp_driver, _tab
    if _tab is None or _cdp_driver is None:
        _cdp_driver, _tab = await _build_cdp_browser()
        return _tab

    # Ping the tab — if it raises, the browser has died
    try:
        await _tab.get_current_url()
    except Exception:
        log.warning("CDP tab appears dead — restarting browser")
        try:
            await _cdp_driver.stop()
        except Exception:
            pass
        _cdp_driver, _tab = await _build_cdp_browser()

    return _tab


# ---------------------------------------------------------------------------
# Lifespan: start browser on startup, stop on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _xvfb_proc
    # Startup: launch Xvfb virtual display when running headed
    if HEADED:
        import subprocess
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", f"{WINDOW_W}x{WINDOW_H}x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        await asyncio.sleep(0.5)  # give Xvfb time to initialise
        log.info("Xvfb started on :99 (%dx%d, headed mode)", WINDOW_W, WINDOW_H)
    # Startup: warm up the CDP browser
    log.info("Starting Pure CDP Mode browser (headless=%s)...", not HEADED)
    async with _driver_lock:
        await _get_or_restart_browser()
    log.info("Server ready")
    yield
    # Shutdown: stop the browser process cleanly
    global _cdp_driver, _tab
    if _cdp_driver:
        log.info("Stopping CDP browser")
        try:
            await _cdp_driver.stop()
        except Exception:
            pass
        _cdp_driver = None
        _tab = None
    # Shutdown: stop Xvfb if we started it
    if _xvfb_proc is not None:
        _xvfb_proc.terminate()
        _xvfb_proc = None
        log.info("Xvfb stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SeleniumBase CDP Fetcher",
    description=(
        "Browserless-compatible + FlareSolverr-compatible headless Chrome REST API "
        "backed by SeleniumBase Pure CDP Mode (no WebDriver/chromedriver)"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class GotoOptions(BaseModel):
    waitUntil: str = Field(
        default="domcontentloaded",
        description="Puppeteer-style wait strategy (informational; CDP navigates and settles)",
    )
    timeout: int = Field(default=PAGE_TIMEOUT * 1000, description="Timeout in ms")


class FetchRequest(BaseModel):
    url: str
    gotoOptions: GotoOptions = Field(default_factory=GotoOptions)
    # Extra options passed by changedetection.io / generic callers
    waitForSelector: str | None = Field(default=None, description="CSS selector to wait for before returning")
    waitForTimeout: int | None = Field(default=None, description="Extra wait in ms after page load")
    userAgent: str | None = None
    cookies: list[dict] | None = None
    headers: dict[str, str] | None = None
    javascript: str | None = Field(default=None, description="JS snippet to execute after page load")
    ignoreHTTPSErrors: bool = True
    viewport: dict | None = None   # { width, height }


class ScreenshotRequest(FetchRequest):
    fullPage: bool = True
    type: str = Field(default="png", description="png or jpeg")
    quality: int | None = None    # only for jpeg


class FetchResponse(BaseModel):
    data: str   # full HTML string
    url: str    # final URL after redirects


# ---------------------------------------------------------------------------
# Core async CDP browser logic
# All functions must be called while holding _driver_lock.
# ---------------------------------------------------------------------------

async def _cdp_navigate(tab: Any, req: FetchRequest) -> None:
    """
    Apply request options (viewport, user-agent, headers, cookies) and
    navigate to req.url. Leaves the tab on the loaded page.
    """
    # Viewport resize
    if req.viewport:
        w = req.viewport.get("width", WINDOW_W)
        h = req.viewport.get("height", WINDOW_H)
        await tab.set_window_size(0, 0, w, h)

    # User-agent override via CDP evaluate
    if req.userAgent:
        await tab.evaluate(
            f"Object.defineProperty(navigator, 'userAgent', "
            f"{{get: () => {repr(req.userAgent)}}})"
        )

    # Compute timeout from gotoOptions (falls back to global PAGE_TIMEOUT)
    timeout_s = (req.gotoOptions.timeout / 1000) if req.gotoOptions else PAGE_TIMEOUT

    # Navigate
    await tab.get(req.url, timeout=timeout_s)

    # Inject cookies after initial load (must be on the target domain first)
    if req.cookies:
        for cookie in req.cookies:
            try:
                await tab.set_all_cookies([cookie])
            except Exception as e:
                log.warning("Cookie error: %s", e)
        # Reload so cookies take effect
        await tab.get(req.url, timeout=timeout_s)

    # Extra settle time
    extra_s = (req.waitForTimeout / 1000.0) if req.waitForTimeout else WAIT_AFTER
    if extra_s > 0:
        await tab.sleep(extra_s)

    # Wait for a CSS selector if requested
    if req.waitForSelector:
        try:
            await tab.wait_for(selector=req.waitForSelector, timeout=timeout_s)
        except Exception as e:
            log.warning("waitForSelector '%s' timed out: %s", req.waitForSelector, e)

    # Execute custom JS snippet
    if req.javascript:
        try:
            await tab.evaluate(req.javascript)
        except Exception as e:
            log.warning("JS execution error: %s", e)


async def _do_fetch(req: FetchRequest) -> tuple[str, str]:
    """
    Navigate to req.url via Pure CDP and return (html, final_url).
    Must be called while holding _driver_lock.
    """
    tab = await _get_or_restart_browser()
    await _cdp_navigate(tab, req)
    html = await tab.get_page_source()
    final_url = await tab.get_current_url()
    return html, final_url


async def _do_screenshot(req: ScreenshotRequest) -> bytes:
    """
    Navigate to req.url via Pure CDP and return a screenshot as raw bytes.
    Must be called while holding _driver_lock.
    """
    tab = await _get_or_restart_browser()
    await _cdp_navigate(tab, req)

    # Expand window height for full-page capture
    if req.fullPage:
        total_height = await tab.evaluate("document.body.scrollHeight")
        await tab.set_window_size(0, 0, WINDOW_W, int(total_height or WINDOW_H))
        await tab.sleep(0.3)

    # save_screenshot writes a PNG to a temp file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    await tab.save_screenshot(filename=tmp_path)

    with open(tmp_path, "rb") as f:
        png_bytes = f.read()

    os.unlink(tmp_path)

    # Convert to JPEG if requested
    if req.type == "jpeg":
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=req.quality or 85)
            return buf.getvalue()
        except ImportError:
            log.warning("Pillow not available — returning PNG despite jpeg request")

    return png_bytes


async def _do_flaresolverr_fetch(req: "FlareSolverrRequest") -> dict:
    """
    Execute a FlareSolverr request.get via Pure CDP.
    Returns the full FlareSolverr solution dict.
    Must be called while holding _driver_lock.
    """
    tab = await _get_or_restart_browser()

    # Apply user-agent override
    if req.userAgent:
        await tab.set_user_agent(req.userAgent)

    # Apply extra request headers by patching window.fetch
    if req.headers:
        headers_js = repr(req.headers)
        await tab.evaluate(
            f"""
            const _origFetch = window.fetch;
            window.fetch = (url, opts = {{}}) => {{
                opts.headers = Object.assign({headers_js}, opts.headers || {{}});
                return _origFetch(url, opts);
            }};
            """
        )

    # Navigate
    timeout_s = (req.maxTimeout or 60000) / 1000.0
    await tab.get(req.url, timeout=timeout_s)

    # Apply cookies
    if req.cookies:
        for cookie in req.cookies:
            try:
                await tab.send(
                    mycdp.network.set_cookie(
                        name=cookie.get("name", ""),
                        value=cookie.get("value", ""),
                        url=req.url,
                        domain=cookie.get("domain") or None,
                        path=cookie.get("path") or None,
                        secure=cookie.get("secure") or None,
                        http_only=cookie.get("httpOnly") or None,
                    )
                )
            except Exception as e:
                log.warning("FlareSolverr cookie error: %s", e)
        await tab.get(req.url, timeout=timeout_s)

    # Settle
    await tab.sleep(WAIT_AFTER)

    # Collect results
    html = await tab.get_page_source()
    final_url = await tab.get_current_url()
    user_agent = await tab.get_user_agent()

    # Normalize cookies to FlareSolverr format
    try:
        raw_cookies = await tab.send(mycdp.network.get_all_cookies())
    except Exception:
        raw_cookies = []
    fs_cookies = [
        {
            "name": c.name if hasattr(c, "name") else c.get("name", ""),
            "value": c.value if hasattr(c, "value") else c.get("value", ""),
            "domain": c.domain if hasattr(c, "domain") else c.get("domain", ""),
            "path": c.path if hasattr(c, "path") else c.get("path", "/"),
            "expires": c.expires if hasattr(c, "expires") else c.get("expires", -1),
            "size": len(c.name if hasattr(c, "name") else c.get("name", "")) + len(c.value if hasattr(c, "value") else c.get("value", "")),
            "httpOnly": c.http_only if hasattr(c, "http_only") else c.get("httpOnly", False),
            "secure": c.secure if hasattr(c, "secure") else c.get("secure", False),
            "session": (c.expires if hasattr(c, "expires") else c.get("expires", -1)) == -1,
            "sameSite": str(c.same_site) if hasattr(c, "same_site") else c.get("sameSite", "None"),
        }
        for c in (raw_cookies or [])
    ]

    solution: dict[str, Any] = {
        "url": final_url,
        "status": 200,
        "cookies": fs_cookies,
        "userAgent": user_agent,
        "headers": {},    # CDP doesn't expose response headers through this path
        "response": html,
    }

    # Optional screenshot (base64 PNG)
    if req.screenshot:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        await tab.save_screenshot(filename=tmp_path)
        with open(tmp_path, "rb") as f:
            solution["screenshot"] = base64.b64encode(f.read()).decode()
        os.unlink(tmp_path)

    return solution


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    """API root — service info and links to docs."""
    return {
        "service": "sbfetcher",
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    """Liveness/readiness probe."""
    uptime = time.time() - _stats["start_time"]
    return {
        "status": "ok",
        "uptime_seconds": round(uptime, 1),
        "browser_active": _tab is not None,
        "mode": "pure-cdp",
    }


@app.get("/metrics")
async def metrics():
    """Basic request counters."""
    uptime = time.time() - _stats["start_time"]
    return {
        "requests_total": _stats["requests"],
        "errors_total": _stats["errors"],
        "uptime_seconds": round(uptime, 1),
        "error_rate": (
            round(_stats["errors"] / _stats["requests"], 4) if _stats["requests"] else 0
        ),
    }


@app.websocket("/cdp")
async def cdp_ws_proxy(client_ws: WebSocket):
    """
    WebSocket proxy: exposes Chrome's CDP endpoint at a fixed address.
    Playwright connect_over_cdp() connects here; messages are forwarded
    transparently to Chrome's internal CDP WebSocket.

    changedetection.io: set PLAYWRIGHT_DRIVER_URL=ws://seleniumbase-fetcher:3000/cdp
    """
    await client_ws.accept()
    if not _chrome_cdp_ws_url:
        await client_ws.close(code=1013)  # service unavailable, try again later
        return
    # Refresh URL in case Chrome was restarted since last startup
    cdp_url = _chrome_cdp_ws_url
    if _chrome_debug_port:
        try:
            _port = _chrome_debug_port
            def _fetch_fresh_ws_url():
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{_port}/json/version", timeout=3
                ) as _r:
                    return json.loads(_r.read()).get("webSocketDebuggerUrl")
            fresh = await asyncio.to_thread(_fetch_fresh_ws_url)
            if fresh:
                cdp_url = fresh
        except Exception as _e:
            log.debug("CDP proxy: could not refresh URL: %s", _e)
    log.info("CDP proxy: client connected, forwarding to %s", cdp_url)
    try:
        async with websockets.connect(cdp_url, ping_interval=None, max_size=2**28) as chrome_ws:
            async def forward_in():
                try:
                    while True:
                        data = await client_ws.receive()
                        if data["type"] == "websocket.disconnect":
                            break
                        elif data["type"] == "websocket.receive":
                            msg = data.get("text") or data.get("bytes")
                            if msg is not None:
                                await chrome_ws.send(msg)
                except Exception:
                    pass

            async def forward_out():
                try:
                    async for msg in chrome_ws:
                        if isinstance(msg, bytes):
                            await client_ws.send_bytes(msg)
                        else:
                            await client_ws.send_text(msg)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_in()), asyncio.create_task(forward_out())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception as e:
        log.warning("CDP proxy error: %s", e)
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass
    log.info("CDP proxy: client disconnected")


@app.post("/content")
async def fetch_content(req: FetchRequest):
    """
    Fetch a URL using Pure CDP headless Chrome and return the rendered HTML.
    Compatible with the browserless /content endpoint.
    Response: { data: "<html>...", url: "<final-url>" }
    """
    _stats["requests"] += 1
    log.info("fetch_content: %s", req.url)
    try:
        async with _driver_lock:
            html, final_url = await _do_fetch(req)
        return FetchResponse(data=html, url=final_url)
    except Exception as e:
        _stats["errors"] += 1
        log.exception("fetch_content error for %s", req.url)
        raise HTTPException(status_code=500, detail=str(e)) from e


# Alias: changedetection.io may POST to /chromium/content (browserless v1 path)
@app.post("/chromium/content")
async def fetch_content_chromium(req: FetchRequest):
    """Alias for /content — browserless v1 compatibility."""
    return await fetch_content(req)


@app.post("/screenshot")
async def screenshot(req: ScreenshotRequest):
    """
    Navigate to a URL via Pure CDP and return a screenshot as image/png (or image/jpeg).
    Compatible with the browserless /screenshot endpoint.
    Response: raw image bytes.
    """
    _stats["requests"] += 1
    log.info("screenshot: %s (fullPage=%s, type=%s)", req.url, req.fullPage, req.type)
    try:
        async with _driver_lock:
            img_bytes = await _do_screenshot(req)
        media_type = "image/jpeg" if req.type == "jpeg" else "image/png"
        return Response(content=img_bytes, media_type=media_type)
    except Exception as e:
        _stats["errors"] += 1
        log.exception("screenshot error for %s", req.url)
        raise HTTPException(status_code=500, detail=str(e)) from e


# Alias: browserless v1 path
@app.post("/chromium/screenshot")
async def screenshot_chromium(req: ScreenshotRequest):
    """Alias for /screenshot — browserless v1 compatibility."""
    return await screenshot(req)


# ---------------------------------------------------------------------------
# FlareSolverr-compatible /v1 endpoint
# ---------------------------------------------------------------------------
# Request contract: POST /v1  { cmd, url, maxTimeout, cookies, session, screenshot }
# Response contract: { status, message, solution: { url, status, cookies, userAgent,
#                       headers, response, screenshot? }, startTimestamp, endTimestamp,
#                       version }

class FlareSolverrRequest(BaseModel):
    cmd: str = Field(description="Command: request.get | sessions.create | sessions.list | sessions.destroy")
    url: str | None = None
    maxTimeout: int = Field(default=60000, description="Timeout in ms")
    cookies: list[dict] | None = None
    session: str | None = Field(default=None, description="Session ID for persistent sessions")
    # If true, include a base64 PNG in solution.screenshot
    screenshot: bool = False
    userAgent: str | None = None
    headers: dict[str, str] | None = None


@app.post("/v1")
async def flaresolverr_v1(req: FlareSolverrRequest):
    """
    FlareSolverr-compatible /v1 endpoint.
    Supports: request.get, sessions.create, sessions.list, sessions.destroy

    changedetection.io usage:
      Set FLARESOLVERR_URL=http://<this-host>:3000
      Use the "FlareSolverr" fetch method per watch.
    """
    start_ts = int(time.time() * 1000)
    _stats["requests"] += 1
    log.info("flaresolverr_v1: cmd=%s url=%s", req.cmd, req.url)

    # --- sessions.create ---
    if req.cmd == "sessions.create":
        session_id = str(uuid.uuid4())
        _sessions[session_id] = {"created_at": time.time()}
        end_ts = int(time.time() * 1000)
        return {
            "status": "ok",
            "message": "Session created successfully.",
            "session": session_id,
            "startTimestamp": start_ts,
            "endTimestamp": end_ts,
            "version": FLARESOLVERR_VERSION,
        }

    # --- sessions.list ---
    if req.cmd == "sessions.list":
        end_ts = int(time.time() * 1000)
        return {
            "status": "ok",
            "message": "",
            "sessions": list(_sessions.keys()),
            "startTimestamp": start_ts,
            "endTimestamp": end_ts,
            "version": FLARESOLVERR_VERSION,
        }

    # --- sessions.destroy ---
    if req.cmd == "sessions.destroy":
        sid = req.session
        if sid and sid in _sessions:
            del _sessions[sid]
            msg = "The session has been removed."
        else:
            msg = "The session does not exist."
        end_ts = int(time.time() * 1000)
        return {
            "status": "ok",
            "message": msg,
            "startTimestamp": start_ts,
            "endTimestamp": end_ts,
            "version": FLARESOLVERR_VERSION,
        }

    # --- request.get ---
    if req.cmd == "request.get":
        if not req.url:
            raise HTTPException(status_code=400, detail="url is required for request.get")
        try:
            async with _driver_lock:
                solution = await _do_flaresolverr_fetch(req)
            end_ts = int(time.time() * 1000)
            return {
                "status": "ok",
                "message": "Challenge solved!",
                "solution": solution,
                "startTimestamp": start_ts,
                "endTimestamp": end_ts,
                "version": FLARESOLVERR_VERSION,
            }
        except Exception as e:
            _stats["errors"] += 1
            log.exception("flaresolverr_v1 request.get error for %s", req.url)
            end_ts = int(time.time() * 1000)
            return JSONResponse(
                status_code=500,
                content={
                    "status": "error",
                    "message": str(e),
                    "startTimestamp": start_ts,
                    "endTimestamp": end_ts,
                    "version": FLARESOLVERR_VERSION,
                },
            )

    # Unknown command
    raise HTTPException(status_code=400, detail=f"Unknown cmd: {req.cmd!r}")


# ---------------------------------------------------------------------------
# Global exception handler — return JSON instead of HTML 500s
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    _stats["errors"] += 1
    log.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"error": str(exc)})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Pure CDP Mode is async-native; use a single worker (asyncio event loop)
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        workers=1,     # Must be 1 — CDP browser lives in one event loop
        log_level=LOG_LEVEL.lower(),
    )
