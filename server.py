"""
SeleniumBase Pure CDP Mode + FastAPI server.

Launches ONE headed Chrome via seleniumbase.cdp_driver (async CDP, no
WebDriver/chromedriver) and exposes its DevTools endpoint at a fixed
WebSocket address for changedetection.io's Playwright fetcher.

Endpoints:
  WS   /cdp     -> Chrome CDP relay (PLAYWRIGHT_DRIVER_URL=ws://<host>:3000/cdp)
  GET  /health  -> { status: "ok", ... }  (503 + restart attempt when Chrome is down)

Stealth on the /cdp path:
  - Chrome reports its real user-agent (no Windows UA on a Linux browser).
  - Playwright's new_context() is mapped onto Chrome's default (persistent)
    browser context, so every check reuses the profile's cookies, including
    Cloudflare's cf_clearance (CDP_SHARE_DEFAULT_CONTEXT).
  - A background warm-up task visits WARMUP_URLS with the server-side
    CAPTCHA solver to keep cf_clearance fresh.
"""

import asyncio
import itertools
import json
import logging
import os
import random
import re
import tempfile
import time
import urllib.request
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from typing import Any

import websockets
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from seleniumbase import cdp_driver

# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------

G_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
HOST           = os.getenv("HOST", "0.0.0.0")
PORT           = int(os.getenv("PORT", "3000"))
WARMUP_TIMEOUT = int(os.getenv("WARMUP_TIMEOUT", "30"))      # warm-up navigation + Cloudflare wait (seconds)
WINDOW_W       = int(os.getenv("WINDOW_WIDTH", "1920"))
WINDOW_H       = int(os.getenv("WINDOW_HEIGHT", "1080"))
CHROME_ARGS    = os.getenv("EXTRA_CHROME_ARGS", "")        # space-separated extra flags
# Empty        = Chrome's real UA, which matches navigator.platform and client hints.
# cf_clearance is bound to the UA, so keep it stable once set.
USER_AGENT     = os.getenv("USER_AGENT", "") or None
# Set HEADED   = true to run Chrome in a virtual Xvfb display (less detectable than --headless)
HEADED         = os.getenv("HEADED", "true").lower() in ("1", "true", "yes")
# Persistent Chrome profile dir; matches CHROME_USER_DATA_DIR in Dockerfile
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "/app/chromiumprofile") or None
# Map Playwright's new_context() onto the default (persistent) browser context.
CDP_SHARE_DEFAULT_CONTEXT = os.getenv("CDP_SHARE_DEFAULT_CONTEXT", "true").lower() in ("1", "true", "yes")
# Comma-separated URLs revisited every WARMUP_INTERVAL minutes to keep cf_clearance fresh.
WARMUP_URLS     = [u.strip() for u in os.getenv("WARMUP_URLS", "").split(",") if u.strip()]
WARMUP_INTERVAL = float(os.getenv("WARMUP_INTERVAL", "60"))
# Set DEBUG_SNAPSHOT=true to save HTML+PNG after every warm-up visit.
DEBUG_SNAPSHOT     = os.getenv("DEBUG_SNAPSHOT", "false").lower() in ("1", "true", "yes")
DEBUG_SNAPSHOT_DIR = os.getenv("DEBUG_SNAPSHOT_DIR", "/tmp/sbfetcher")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sbfetcher")

# ---------------------------------------------------------------------------
# CDP browser state
#
# A SINGLE Chrome process is launched at server startup (lifespan) and lives
# until shutdown.  Playwright clients reach it through the /cdp relay; each
# client gets its own CDP connection to the same browser.
#
# The server keeps one tab (_tab) of its own, used by the warm-up task and as
# a liveness ping.  _driver_lock serializes warm-up visits and restarts.
#
# The browser is relaunched when the tab appears dead (_get_or_restart_browser),
# triggered by /health, a /cdp connect, or the warm-up task.  The profile in
# CHROME_USER_DATA_DIR persists cookies (incl. cf_clearance) across restarts.
# ---------------------------------------------------------------------------

_cdp_driver: Any = None        # cdp_driver instance (the browser process)
_tab: Any = None               # the server's own CDP tab
_driver_lock = asyncio.Lock()
_xvfb_proc: Any = None         # Xvfb subprocess (only when HEADED=True)
_chrome_debug_port: int | None = None  # Chrome remote debugging port
_restart_task: asyncio.Task | None = None
_warmup_task: asyncio.Task | None = None
_start_time = time.time()


def _fetch_ws_url() -> str:
    """Return Chrome's current browser-level CDP WebSocket URL (blocking)."""
    if not _chrome_debug_port:
        raise RuntimeError("Chrome is not running")
    with urllib.request.urlopen(
        f"http://127.0.0.1:{_chrome_debug_port}/json/version", timeout=3
    ) as r:
        return json.loads(r.read())["webSocketDebuggerUrl"]


async def _build_cdp_browser() -> tuple[Any, Any]:
    """
    Launch a new Pure CDP Mode browser and return (driver, tab).
    Extra Chrome flags from EXTRA_CHROME_ARGS are forwarded to the browser.
    """
    global _chrome_debug_port
    extra = CHROME_ARGS.split() if CHROME_ARGS else []
    # cdp_driver.start_async() launches Chrome with CDP enabled.
    # no_sandbox=True is required inside Docker containers.
    driver = await cdp_driver.start_async(
        headless=not HEADED,
        no_sandbox=True,
        agent=USER_AGENT,
        lang="en-US",
        # browser_executable_path can be set if Chrome is not on PATH;
        # the Dockerfile symlinks chromium-browser onto PATH.
        user_data_dir=CHROME_USER_DATA_DIR,
        extra_chromium_args=extra,
    )
    tab = await driver.get("about:blank")
    await tab.set_window_size(0, 0, WINDOW_W, WINDOW_H)
    _chrome_debug_port = urlparse(driver.get_endpoint_url()).port
    log.info("Pure CDP browser started (no WebDriver/chromedriver)")
    log.info("Chrome CDP WebSocket endpoint: %s", await asyncio.to_thread(_fetch_ws_url))
    return driver, tab


async def _get_or_restart_browser() -> Any:
    """
    Return the server's CDP tab. If the browser has crashed, restart it.
    Must be called while holding _driver_lock.
    """
    global _cdp_driver, _tab
    if _tab is None or _cdp_driver is None:
        _cdp_driver, _tab = await _build_cdp_browser()
        return _tab

    # Ping the tab — if it raises or hangs, the browser has died
    try:
        await asyncio.wait_for(_tab.get_current_url(), timeout=5)
    except Exception:
        log.warning("CDP tab appears dead — restarting browser")
        try:
            _cdp_driver.stop()
        except Exception:
            pass
        _cdp_driver, _tab = await _build_cdp_browser()

    return _tab


async def _ensure_browser() -> None:
    """Restart Chrome if it has died."""
    async with _driver_lock:
        await _get_or_restart_browser()


def _schedule_restart() -> None:
    """Start a background _ensure_browser() unless one is already running."""
    global _restart_task
    if _restart_task is None or _restart_task.done():
        _restart_task = asyncio.create_task(_ensure_browser())


# ---------------------------------------------------------------------------
# Lifespan: start browser on startup, stop on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _xvfb_proc, _warmup_task
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
    if WARMUP_URLS:
        _warmup_task = asyncio.create_task(_warmup_loop())
        log.info("Warm-up every %s min: %s", WARMUP_INTERVAL, ", ".join(WARMUP_URLS))
    log.info("Server ready")
    yield
    # Shutdown: stop the warm-up task and the browser process
    if _warmup_task is not None:
        _warmup_task.cancel()
        await asyncio.gather(_warmup_task, return_exceptions=True)
        _warmup_task = None
    global _cdp_driver, _tab
    if _cdp_driver:
        log.info("Stopping CDP browser")
        try:
            _cdp_driver.stop()
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
        "Headed Chrome exposed over a CDP WebSocket relay for changedetection.io's "
        "Playwright fetcher, backed by SeleniumBase Pure CDP Mode (no WebDriver/chromedriver)"
    ),
    version="3.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Cloudflare / CAPTCHA handling (used by the warm-up task)
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


def _classify_page(title: str, html: str) -> str:
    """Return "blocked", "challenge" or "ok" for a loaded page (Cloudflare or DataDome)."""
    if (
        "Sorry, you have been blocked" in html
        or ("cf-error-details" in html and "_cf_chl_opt" not in html)
        or "'t':'bv'" in html  # DataDome hard block
    ):
        return "blocked"
    if (
        title.startswith("Just a moment")
        or "_cf_chl_opt" in html
        or 'id="challenge-' in html
        or "captcha-delivery.com" in html  # DataDome device check / captcha
    ):
        return "challenge"
    return "ok"


async def _solve_challenge(tab: Any) -> bool:
    """Click a CAPTCHA, preferring SeleniumBase's native async solver."""
    if hasattr(tab, "solve_captcha"):
        try:
            return bool(await tab.solve_captcha())
        except Exception as exc:
            log.debug("native solve_captcha failed, using fallback: %s", exc)
    return await _solve_captcha_if_present(tab)


async def _wait_for_cloudflare(tab: Any, timeout_s: float) -> str:
    """
    Poll the current page until Cloudflare/DataDome lets it through.
    Waits passively for the first few seconds (managed challenges and device
    checks often clear on their own), then clicks the CAPTCHA every few seconds.
    Returns the final page state from _classify_page().
    """
    start = time.monotonic()
    last_click: float | None = None
    seen_challenge = False
    ok_streak = 0
    while True:
        elapsed = time.monotonic() - start
        try:
            title = await tab.get_title() or ""
            html = await tab.get_page_source() or ""
            state = _classify_page(title, html)
        except Exception as exc:
            # Page is mid-navigation (e.g. the challenge just redirected)
            log.debug("_wait_for_cloudflare: page not readable yet: %s", exc)
            state = "challenge"
        if state == "challenge" and not seen_challenge:
            seen_challenge = True
            log.info("challenge detected on %s — waiting", await tab.get_current_url())
        ok_streak = ok_streak + 1 if state == "ok" else 0
        # After a challenge, the reload can briefly show an empty document: confirm "ok" twice
        if state == "ok" and seen_challenge and ok_streak < 2 and elapsed < timeout_s:
            await tab.sleep(1)
            continue
        if state != "challenge" or elapsed >= timeout_s:
            if seen_challenge:
                log.info("challenge ended as %r after %.1fs", state, elapsed)
            return state
        if elapsed >= 6 and (last_click is None or time.monotonic() - last_click >= 8):
            last_click = time.monotonic()
            await _solve_challenge(tab)
        await tab.sleep(1)


async def _warm_url(tab: Any, url: str) -> None:
    """Visit url and get through any Cloudflare check, refreshing cf_clearance."""
    # tab.get() has no timeout of its own (extra kwargs only change its code path)
    await asyncio.wait_for(tab.get(url), timeout=WARMUP_TIMEOUT)
    state = await _wait_for_cloudflare(tab, WARMUP_TIMEOUT)
    final_url = await tab.get_current_url()
    if state == "ok":
        log.info("warm-up: %s ok", final_url)
    else:
        log.warning("warm-up: %s still %s after %ss", url, state, WARMUP_TIMEOUT)
    await _save_debug_snapshot(tab, final_url)
    # Park the tab so it isn't running the site's scripts between rounds
    await tab.get("about:blank")


async def _warmup_loop() -> None:
    """Revisit WARMUP_URLS every WARMUP_INTERVAL minutes (jittered)."""
    while True:
        for url in WARMUP_URLS:
            try:
                async with _driver_lock:
                    tab = await _get_or_restart_browser()
                    await _warm_url(tab, url)
            except Exception:
                log.exception("warm-up failed for %s", url)
            await asyncio.sleep(random.uniform(2, 8))
        await asyncio.sleep(WARMUP_INTERVAL * 60 * random.uniform(0.9, 1.1))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    """API root — service info and links."""
    return {
        "service": "sbfetcher",
        "cdp": "/cdp",
        "health": "/health",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """Liveness/readiness probe. Returns 503 and kicks off a restart if Chrome is down."""
    uptime = round(time.time() - _start_time, 1)
    try:
        await asyncio.to_thread(_fetch_ws_url)
    except Exception as exc:
        _schedule_restart()
        return JSONResponse(
            status_code=503,
            content={
                "status": "down",
                "error": str(exc),
                "uptime_seconds": uptime,
                "browser_active": False,
                "mode": "pure-cdp",
            },
        )
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "browser_active": True,
        "mode": "pure-cdp",
    }


# ---------------------------------------------------------------------------
# WebSocket /cdp relay
# ---------------------------------------------------------------------------

# Message ids for relay-originated CDP calls; Playwright counts up from 1.
_PRIVATE_ID_BASE = 1_000_000_000
# Chrome serializes replies as {"id":N,...}; lets us spot ids without parsing big frames.
_REPLY_ID = re.compile(r'^\{"id":(\d+),')


async def _default_context_id(chrome_ws: Any) -> str | None:
    """
    Ask Chrome for the default browser context id, i.e. the context of the
    server's own tab. Must run before the relay starts forwarding.
    """
    msg_id = _PRIVATE_ID_BASE - 1
    await chrome_ws.send(json.dumps({"id": msg_id, "method": "Target.getTargets"}))
    while True:
        reply = json.loads(await asyncio.wait_for(chrome_ws.recv(), timeout=5))
        if reply.get("id") == msg_id:
            break
    infos = reply.get("result", {}).get("targetInfos", [])
    own_id = getattr(getattr(_tab, "target", None), "target_id", None)
    for info in infos:
        if info.get("targetId") == own_id:
            return info.get("browserContextId")
    for info in infos:
        if info.get("type") == "page" and info.get("browserContextId"):
            return info["browserContextId"]
    return None


@app.websocket("/cdp")
async def cdp_ws_proxy(client_ws: WebSocket):
    """
    WebSocket relay: exposes Chrome's CDP endpoint at a fixed address.
    Playwright connect_over_cdp() connects here.

    With CDP_SHARE_DEFAULT_CONTEXT, Playwright's new_context() is answered with
    Chrome's default browser context instead of a fresh incognito one, so its
    pages reuse the persistent profile (cookies, cf_clearance).  Pages the
    client opened are closed when it disposes that context or disconnects.
    Contexts requested with their own proxy are left alone.
    Everything else is forwarded unchanged.

    changedetection.io: set PLAYWRIGHT_DRIVER_URL=ws://sbfetcher:3000/cdp
    """
    await client_ws.accept()
    try:
        cdp_url = await asyncio.to_thread(_fetch_ws_url)
    except Exception:
        log.warning("CDP proxy: Chrome unreachable — restarting browser")
        try:
            await _ensure_browser()
            cdp_url = await asyncio.to_thread(_fetch_ws_url)
        except Exception as e:
            log.warning("CDP proxy: browser restart failed: %s", e)
            await client_ws.close(code=1013)  # service unavailable, try again later
            return
    log.info("CDP proxy: client connected, forwarding to %s", cdp_url)
    try:
        async with websockets.connect(cdp_url, ping_interval=None, max_size=2**28) as chrome_ws:
            default_ctx = None
            if CDP_SHARE_DEFAULT_CONTEXT:
                try:
                    default_ctx = await _default_context_id(chrome_ws)
                except Exception as e:
                    log.warning("CDP proxy: could not read default context: %s", e)
                if not default_ctx:
                    log.warning("CDP proxy: default context unknown — not sharing it")

            private_ids = itertools.count(_PRIVATE_ID_BASE)
            swallow: set[int] = set()            # replies to relay-originated calls
            create_target_ids: set[int] = set()  # client ids of Target.createTarget
            opened_targets: set[str] = set()     # pages this client opened

            async def reply(req: dict, result: dict) -> None:
                msg = {"id": req["id"], "result": result}
                if "sessionId" in req:
                    msg["sessionId"] = req["sessionId"]
                await client_ws.send_text(json.dumps(msg))

            async def close_opened_targets() -> None:
                for target_id in list(opened_targets):
                    msg_id = next(private_ids)
                    swallow.add(msg_id)
                    await chrome_ws.send(json.dumps({
                        "id": msg_id,
                        "method": "Target.closeTarget",
                        "params": {"targetId": target_id},
                    }))
                opened_targets.clear()

            async def intercept(msg: str) -> str | None:
                """Map context calls onto the default context. None = answered here."""
                try:
                    req = json.loads(msg)
                except ValueError:
                    return msg
                method = req.get("method")
                params = req.get("params") or {}
                if method == "Target.createBrowserContext" and not params.get("proxyServer"):
                    await reply(req, {"browserContextId": default_ctx})
                    return None
                if method == "Target.disposeBrowserContext" and params.get("browserContextId") == default_ctx:
                    await close_opened_targets()
                    await reply(req, {})
                    return None
                if params.get("browserContextId") == default_ctx:
                    if method == "Target.createTarget":
                        create_target_ids.add(req["id"])
                    # Chrome addresses the default context by omitting the id
                    del params["browserContextId"]
                    return json.dumps(req)
                return msg

            async def forward_in():
                try:
                    while True:
                        data = await client_ws.receive()
                        if data["type"] == "websocket.disconnect":
                            break
                        elif data["type"] == "websocket.receive":
                            msg = data.get("text") or data.get("bytes")
                            if msg is not None and default_ctx and isinstance(msg, str):
                                msg = await intercept(msg)
                            if msg is not None:
                                await chrome_ws.send(msg)
                except Exception:
                    pass

            async def forward_out():
                try:
                    async for msg in chrome_ws:
                        if isinstance(msg, str) and (swallow or create_target_ids):
                            m = _REPLY_ID.match(msg)
                            msg_id = int(m.group(1)) if m else None
                            if msg_id in swallow:
                                swallow.discard(msg_id)
                                continue
                            if msg_id in create_target_ids:
                                create_target_ids.discard(msg_id)
                                target_id = json.loads(msg).get("result", {}).get("targetId")
                                if target_id:
                                    opened_targets.add(target_id)
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

            # Client left without disposing its context: close its pages ourselves
            if opened_targets:
                log.info("CDP proxy: closing %d page(s) left open by client", len(opened_targets))
                try:
                    await close_opened_targets()

                    async def drain():
                        async for msg in chrome_ws:
                            m = isinstance(msg, str) and _REPLY_ID.match(msg)
                            if m:
                                swallow.discard(int(m.group(1)))
                            if not swallow:
                                return

                    await asyncio.wait_for(drain(), timeout=3)
                except Exception as e:
                    log.debug("CDP proxy: closing leftover pages failed: %s", e)
    except Exception as e:
        log.warning("CDP proxy error: %s", e)
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass
    log.info("CDP proxy: client disconnected")


# ---------------------------------------------------------------------------
# Global exception handler — return JSON instead of HTML 500s
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
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
