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
  NOT supported by changedetection.io in any form — see notes below.
  Intended for direct API consumers that speak the FlareSolverr API
  (e.g. Prowlarr, Radarr, or any app with native FlareSolverr support).
  POST /v1  -> { cmd: "request.get", url, [maxTimeout, cookies, screenshot] }
            -> { status, message, solution: { url, response, cookies, userAgent, screenshot? } }
  POST /v1  -> { cmd: "sessions.create" }  -> { session }
  POST /v1  -> { cmd: "sessions.list" }    -> { sessions }
  POST /v1  -> { cmd: "sessions.destroy", session }

changedetection.io integration:
  Playwright CDP:   PLAYWRIGHT_DRIVER_URL=ws://<host>:3000/cdp
                    Select "Playwright Chromium/Javascript" fetch method per watch
  Note: FLARESOLVERR_URL is NOT supported by changedetection.io in any form; proxy-based
        FlareSolverr integration is not available — use PLAYWRIGHT_DRIVER_URL.
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
FLARESOLVERR_VERSION = "v3.4.6"

# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------

LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()
HOST         = os.getenv("HOST", "0.0.0.0")
PORT         = int(os.getenv("PORT", "3000"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "30"))      # seconds
WAIT_AFTER   = float(os.getenv("WAIT_AFTER_LOAD", "5"))  # extra settle time (seconds)
WINDOW_W     = int(os.getenv("WINDOW_WIDTH", "1920"))
WINDOW_H     = int(os.getenv("WINDOW_HEIGHT", "1080"))
CHROME_ARGS  = os.getenv("EXTRA_CHROME_ARGS", "")        # space-separated extra flags
# Set HEADED=true to run Chrome in a virtual Xvfb display (useful for debugging)
HEADED       = os.getenv("HEADED", "true").lower() in ("1", "true", "yes")
# Persistent Chrome profile dir; matches CHROME_USER_DATA_DIR in Dockerfile
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "/app/chromiumprofile") or None
# Set DEBUG_SNAPSHOT=false to disable; snapshots land in DEBUG_SNAPSHOT_DIR.
DEBUG_SNAPSHOT     = os.getenv("DEBUG_SNAPSHOT", "false").lower() in ("1", "true", "yes")
DEBUG_SNAPSHOT_DIR = os.getenv("DEBUG_SNAPSHOT_DIR", "/tmp/sbfetcher")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sbfetcher")

# ---------------------------------------------------------------------------
# CDP browser state
#
# A SINGLE Chrome process is launched at server startup (lifespan) and reused
# for every request — no new browser is spawned per request.
#
# All requests share ONE tab (_tab), which is simply navigated to the new URL
# via tab.get(url) for each request.  _driver_lock (asyncio.Lock) serializes
# concurrent requests so only one navigation runs at a time.
#
# The browser is only relaunched when the tab appears dead (detected in
# _get_or_restart_browser by a failed get_current_url() ping).  Because the
# session persists across requests, cookies and profile state are retained.
#
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


async def _solve_captcha_if_present(tab: Any) -> bool:
    """
    Detect and click common CAPTCHA challenges after page load.
    Mimics CDPMethods.solve_captcha() / __click_captcha(use_cdp=True).
    Uses raw async tab API to avoid event-loop conflict with FastAPI.

    Handles:
        - Cloudflare Turnstile (CSS alignment fix + offset click 25,32)
        - reCAPTCHA v2 (skips invisible bottom-right widget, offset 26,35)
        - hCaptcha / Incapsula hCaptcha (offset 30,36)
        - FriendlyCaptcha (offset 27,34 with mouse_move)
        - DataDome slider (detected, not solved — requires GUI drag)

    Returns True if a challenge was found and a click was attempted.
    """

    # --- async helpers (async equivalents of CDPMethods private helpers) ----

    async def _is_present(sel: str) -> bool:
        try:
            return (await tab.select(sel, timeout=0.1)) is not None
        except Exception:
            return False

    async def _is_visible(sel: str) -> bool:
        try:
            el = await tab.select(sel, timeout=0.1)
            if el is None:
                return False
            pos = await el.get_position_async()
            return pos.width != 0 or pos.height != 0
        except Exception:
            return False

    async def _cdp_click_offset(sel: str, x_off: int, y_off: int) -> bool:
        """Async equiv of: bring_active_window_to_front() + click_with_offset(sel,x,y)"""
        try:
            el = await tab.select(sel, timeout=2)
            if el is None:
                return False
            await el.scroll_into_view_async()
            await tab.bring_to_front()
            await tab.sleep(0.056)
            await el.mouse_click_with_offset_async(x_off, y_off)
            await tab.sleep(0.2)
            return True
        except Exception as exc:
            log.debug(
                "solve_captcha: click_offset(%r, %s, %s) failed: %s",
                sel, x_off, y_off, exc,
            )
            return False

    # --- initial settle (mirrors __click_captcha preamble) ------------------
    await tab.sleep(0.1)
    await tab.sleep(0.1)

    try:
        source = await tab.get_page_source()
    except Exception:
        return False

    # --- CF Turnstile detection (mirrors _on_a_cf_turnstile_page) -----------
    is_cf = (
        "cf-turnstile-" in source
        or "/challenge-platform/h/b/" in source
        or 'id="challenge-widget-' in source
        or "challenges.cloudf" in source
        or (
            'data-callback="onCaptchaSuccess"' in source
            and 'title="reCAPTCHA"' not in source
            and 'id="recaptcha-token"' not in source
        )
    )

    if not is_cf:
        # --- reCAPTCHA detection (mirrors _on_a_g_recaptcha_page) -----------
        # reCAPTCHA may need a moment to appear after page load
        await tab.sleep(0.4)
        await tab.sleep(0.1)
        source = await tab.get_page_source()
        recaptcha_in_source = (
            'id="recaptcha-token"' in source or 'title="reCAPTCHA"' in source
        )
        is_recaptcha = (
            recaptcha_in_source
            and await _is_visible('iframe[title="reCAPTCHA"]')
        )
        if not is_recaptcha and "com/recaptcha/api.js" in source:
            # API loaded but widget not yet rendered
            await tab.sleep(1.2)
            await tab.sleep(0.1)
            is_recaptcha = await _is_visible('iframe[title="reCAPTCHA"]')

        if is_recaptcha:
            log.info("solve_captcha: reCAPTCHA detected")
            await tab.sleep(0.25)
            await tab.sleep(0.1)
            await tab.sleep(0.25)
            # Skip invisible reCAPTCHA in bottom-right corner
            try:
                el = await tab.select('iframe[title="reCAPTCHA"]', timeout=0.5)
                pos = await el.get_position_async()
                win_w = await tab.evaluate("window.innerWidth")
                win_h = await tab.evaluate("window.innerHeight")
                if (
                    pos.x > 1040 and pos.y > 640
                    and abs(win_w - pos.x) < 110
                    and abs(win_h - pos.y) < 110
                ):
                    log.debug(
                        "solve_captcha: skipping invisible reCAPTCHA "
                        "(bottom-right corner)"
                    )
                    return False
            except Exception:
                pass
            await tab.sleep(0.08)
            await tab.sleep(0.03)
            result = await _cdp_click_offset('iframe[title="reCAPTCHA"]', 26, 35)
            if result:
                log.info("solve_captcha: clicked reCAPTCHA")
            return result

        # --- hCaptcha detection (mirrors _on_an_incapsula_hcaptcha_page) ---
        await tab.sleep(0.1)
        is_hcaptcha = (
            await _is_visible('iframe[src*="_Incapsula_Resource?"]')
            or await _is_visible("iframe[data-hcaptcha-widget-id]")
        )
        if is_hcaptcha:
            log.info("solve_captcha: hCaptcha detected")
            await tab.sleep(0.05)
            await tab.sleep(0.1)
            await tab.sleep(0.05)
            if await _is_visible('iframe[src*="_Incapsula_Resource?"]'):
                try:
                    outer = await tab.select(
                        'iframe[src*="_Incapsula_Resource?"]', timeout=0.5
                    )
                    inner = await outer.query_selector_async(
                        "iframe[data-hcaptcha-widget-id]"
                    )
                    if not inner:
                        return False
                    await tab.bring_to_front()
                    await tab.sleep(0.056)
                    await inner.mouse_click_with_offset_async(30, 36)
                    await tab.sleep(0.075)
                    await tab.sleep(0.75)
                    log.info("solve_captcha: clicked nested hCaptcha")
                    return True
                except Exception as exc:
                    log.debug(
                        "solve_captcha: nested hCaptcha click failed: %s", exc
                    )
                    return False
            else:
                el = None
                try:
                    el = await tab.select(
                        "iframe[data-hcaptcha-widget-id]", timeout=0.5
                    )
                except Exception:
                    pass
                if not el:
                    return False
                await tab.bring_to_front()
                await tab.sleep(0.056)
                await el.mouse_click_with_offset_async(30, 36)
                await tab.sleep(0.075)
                await tab.sleep(0.75)
                log.info("solve_captcha: clicked hCaptcha")
                return True

        # --- DataDome: detected but not handled (requires slider drag) ------
        await tab.sleep(0.05)
        if await _is_visible(
            'body > iframe[src*="/geo.captcha-delivery.com/captcha/"]',
        ):
            log.warning(
                "solve_captcha: DataDome slider detected — "
                "not supported (requires GUI drag)"
            )
            return False

        # --- FriendlyCaptcha (mirrors __gui_click_friendly_captcha(use_cdp=True)) ---
        await tab.sleep(0.05)
        if await _is_visible("iframe[data--frc-frame-id]"):
            log.info("solve_captcha: FriendlyCaptcha detected")
            await tab.sleep(0.05)
            await tab.sleep(0.1)
            await tab.sleep(0.05)
            try:
                el = await tab.select("iframe[data--frc-frame-id]", timeout=0.5)
                if not el:
                    return False
                pos = await el.get_position_async()
                win_w = await tab.evaluate("window.innerWidth")
                win_h = await tab.evaluate("window.innerHeight")
                if (
                    pos.x > 1040 and pos.y > 640
                    and abs(win_w - pos.x) < 110
                    and abs(win_h - pos.y) < 110
                ):
                    log.debug(
                        "solve_captcha: skipping bottom-right FriendlyCaptcha"
                    )
                    return False
                await tab.sleep(0.08)
                await tab.sleep(0.03)
                await tab.bring_to_front()
                await tab.sleep(0.06)
                await el.mouse_move_async()
                await tab.sleep(0.08)
                await el.mouse_click_with_offset_async(27, 34)
                await tab.sleep(0.08)
                await el.mouse_move_async()
                await tab.sleep(0.25)
                log.info("solve_captcha: clicked FriendlyCaptcha")
                return True
            except Exception as exc:
                log.debug(
                    "solve_captcha: FriendlyCaptcha click failed: %s", exc
                )
                return False

        return False  # nothing detected

    # --- Cloudflare Turnstile handler (only reached when is_cf) -------------
    log.info("solve_captcha: CF Turnstile detected")

    # Find the right selector (mirrors __click_captcha if/elif chain)
    cf_selector = None
    for candidate in [
        '[class="cf-turnstile"]',
        "#challenge-form div > div",
        '[style="display: grid;"] div div',
        '[class*=spacer] + div div',
        ".spacer div:not([class])",
        '[data-testid*="challenge-"] div',
        "div#turnstile-widget div:not([class])",
        "ngx-turnstile div:not([class])",
        'form div:not([class]):has(input[name*="cf-turn"])',
        "body > div#check > div:not([class])",
        ".cf-turnstile-wrapper",
        '[id*="turnstile"] div:not([class])',
        '[class*="turnstile"] div:not([class])',
        "iframe[data-hcaptcha-widget-id]",
        '[data-callback="onCaptchaSuccess"]',
        '[class*="captcha"] div:not([class])',
        "form div:not(:has(*))",
        (
            "div:not([class]):not([id]):not([aria-label]) > "
            "div:not([class]):not([id]):not([aria-label])"
        ),
    ]:
        if await _is_present(candidate):
            cf_selector = candidate
            # Inline CSS fix for grid layout
            if candidate == '[style="display: grid;"] div div':
                try:
                    await tab.evaluate(
                        "var $e=document.querySelectorAll("
                        "'[style=\"display: grid;\"] div div');"
                        "for(var i=0;i<$e.length;i++){"
                        "$e[i].setAttribute('style','text-align:left;');}"
                    )
                    await tab.sleep(0.025)
                except Exception:
                    pass
            break

    if not cf_selector:
        log.warning(
            "solve_captcha: CF Turnstile detected in source "
            "but no clickable element found"
        )
        return False

    # CSS alignment fixes (center/right → left) — mirrors elif chain
    try:
        if (
            await _is_present('form[class*="center"]')
            or await _is_present('form[class*="right"]')
            or await _is_present('form div[class*="center"]')
            or await _is_present('form div[class*="right"]')
        ):
            await tab.evaluate(
                "var $e=document.querySelectorAll('form[class], form div[class]');"
                "for(var i=0;i<$e.length;i++){"
                "var c=$e[i].getAttribute('class');"
                "c=c.replaceAll('center','left').replaceAll('right','left');"
                "$e[i].setAttribute('class',c);}"
            )
            await tab.sleep(0.1)
        elif (
            await _is_present('form div[style*="center"]')
            or await _is_present('form div[style*="right"]')
        ):
            await tab.evaluate(
                "var $e=document.querySelectorAll('form[style], form div[style]');"
                "for(var i=0;i<$e.length;i++){"
                "var s=$e[i].getAttribute('style');"
                "s=s.replaceAll('center','left').replaceAll('right','left');"
                "$e[i].setAttribute('style',s);}"
            )
            await tab.sleep(0.1)
        elif (
            await _is_present('form [id*="turnstile"] div:not([class])')
            or await _is_present('form [class*="turnstile"] div:not([class])')
        ):
            await tab.evaluate(
                "var $e=document.querySelectorAll('form [id*=\"turnstile\"]');"
                "for(var i=0;i<$e.length;i++){$e[i].setAttribute('align','left');}"
                "var $e=document.querySelectorAll('form [class*=\"turnstile\"]');"
                "for(var i=0;i<$e.length;i++){$e[i].setAttribute('align','left');}"
            )
            await tab.sleep(0.1)
        elif await _is_present('[style*="text-align: center;"] div:not([class])'):
            await tab.evaluate(
                "var $e=document.querySelectorAll('[style*=\"text-align: center;\"]');"
                "for(var i=0;i<$e.length;i++){"
                "var s=$e[i].getAttribute('style');"
                "s=s.replaceAll('center','left');"
                "$e[i].setAttribute('style',s);}"
            )
            await tab.sleep(0.1)
    except Exception as exc:
        log.debug(
            "solve_captcha: CSS alignment fix failed (non-fatal): %s", exc
        )

    await tab.sleep(0.05)
    await tab.sleep(0.03)
    result = await _cdp_click_offset(cf_selector, 25, 32)
    if result:
        await tab.sleep(5)  # wait for challenge resolution
        log.info("solve_captcha: clicked CF Turnstile (%r)", cf_selector)
    return result

async def _save_debug_snapshot(
    tab: Any, url: str, html: str | None = None, png_bytes: bytes | None = None
) -> None:
    """
    Save an HTML page source and a PNG screenshot to DEBUG_SNAPSHOT_DIR.
    Non-fatal — any exception is logged at WARNING level only.
    Controlled by the DEBUG_SNAPSHOT env var (default: true).
    """
    if not DEBUG_SNAPSHOT:
        return
    try:
        os.makedirs(DEBUG_SNAPSHOT_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        host = urlparse(url).hostname or "unknown"
        safe_host = "".join(c if c.isalnum() or c in "-_" else "_" for c in host)
        base = os.path.join(DEBUG_SNAPSHOT_DIR, f"{ts}_{safe_host}")

        # -- HTML ---------------------------------------------------------
        if html is None:
            try:
                html = await tab.get_page_source()
            except Exception:
                pass
        if html:
            html_path = f"{base}.html"
            with open(html_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(html)
            log.debug("Debug snapshot HTML: %s", html_path)

        # -- Screenshot ---------------------------------------------------
        if png_bytes is None:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                await tab.save_screenshot(filename=tmp_path)
                with open(tmp_path, "rb") as fh:
                    png_bytes = fh.read()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if png_bytes:
            png_path = f"{base}.png"
            with open(png_path, "wb") as fh:
                fh.write(png_bytes)
            log.debug("Debug snapshot PNG: %s", png_path)

    except Exception as exc:
        log.warning("_save_debug_snapshot failed (non-fatal): %s", exc)


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

    # Attempt to solve any CAPTCHA challenge present after page load
    await _solve_captcha_if_present(tab)

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
    await _save_debug_snapshot(tab, final_url, html=html)
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

    await _save_debug_snapshot(tab, req.url, png_bytes=png_bytes)

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

    # Attempt to solve any CAPTCHA challenge present
    await _solve_captcha_if_present(tab)

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

    await _save_debug_snapshot(tab, final_url, html=html)
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

    Intended for direct API consumers that speak the FlareSolverr API
    (e.g. Prowlarr, Radarr, or any app with native FlareSolverr support).

    changedetection.io does NOT support FlareSolverr in any form:
      - FLARESOLVERR_URL env var is silently ignored (dgtlmoon/changedetection.io#2445).
      - Configuring http://<host>:3000 as an HTTP proxy also fails
        (ERR_TUNNEL_CONNECTION_FAILED) — FlareSolverr is an API, not an
        HTTP CONNECT proxy.
    Use PLAYWRIGHT_DRIVER_URL=ws://<host>:3000/cdp instead.
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
