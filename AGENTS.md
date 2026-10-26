# sbfetcher: guide for coding agents and developers

A headed Chrome behind a CDP WebSocket relay (SeleniumBase Pure CDP Mode + FastAPI) used as a stealthy Playwright
browser for changedetection.io. User documentation (endpoints, settings, behavior, deployment) is in
[README.md](README.md); this file holds the design notes. Read it before changing `server.py`.

## Design

### Architecture

```
changedetection.io (Playwright)
   |  ws://sbfetcher:3000/cdp            connect_over_cdp(PLAYWRIGHT_DRIVER_URL)
   v
sbfetcher relay (FastAPI websocket in server.py)    forwards every CDP message, with a few changes (below)
   |  ws://127.0.0.1:<port>/devtools/browser/<id>
   v
ONE Chrome process tree, ONE profile  <-- SeleniumBase tab (the server's own CDP connection, used for warm-ups)
```

- **Who picks the WebSocket:** changedetection picks Playwright and the address (`PLAYWRIGHT_DRIVER_URL`, then
  `browser_type.connect_over_cdp(url)`). The Chrome DevTools Protocol itself is defined over a WebSocket. The `/cdp`
  path is ours: Chrome's real endpoint has a random port and ID that change on every restart.
- **One Chrome, many windows:** all clients share one Chrome instance and one profile. Each page a client opens
  gets its own *window* (not a separate instance). A tab hidden behind another tab renders no frames and a
  screenshot of it hangs, so pages must not share a window with the warm-up tab or with each other.

### How SeleniumBase is used

Only `seleniumbase.cdp_driver` (Pure CDP mode). No WebDriver, no chromedriver, no SeleniumBase test framework.
It (1) launches Chrome (`start_async`: profile dir, language, flags, headed under the Xvfb display that `start.sh`
starts via the base image's `vnc-start`), (2) provides the server's own tab (`_tab`) for warm-ups: navigate, read the page, scroll, bring to front,
click CAPTCHAs (`solve_captcha`, with `_solve_captcha_if_present` as a fallback), and (3) manages the process
(`get_endpoint_url()` gives the debugging port, `stop()` shuts it down). Two CDP clients drive the same Chrome at
once, the SeleniumBase tab and Playwright; Chrome allows that.

### What the relay changes (`cdp_ws_proxy` in `server.py`)

| Client message / event | Relay behavior |
|---|---|
| `Target.createBrowserContext` | answered with Chrome's default context (shared persistent profile), not forwarded |
| `Target.createTarget` (default context) | forwarded with `newWindow: true`; the new target is tracked |
| `Target.disposeBrowserContext` | answered locally; the pages this connection opened are closed |
| `Page.navigate` | held while a warm-up runs for that host (other messages keep flowing, see Gotchas) |
| `Network.responseReceived` (Document) | a 403/429/503 for a host this client navigated to is recorded as a failure |
| everything else | forwarded unchanged |

### Other ways to connect

Any client that accepts a CDP WebSocket URL can use `ws://<host>:3000/cdp`. Tested: Playwright (Python) and raw
`websockets`. Expected to work but untested: Playwright JS (`connectOverCDP`), Puppeteer
(`connect({browserWSEndpoint})`), chrome-remote-interface. Selenium and `puppeteer.connect({browserURL})` do
**not** work: they need Chrome's HTTP discovery pages (`/json/version`), which the relay does not expose. There is
no authentication, so keep the port off untrusted networks. All clients get the relay's rules (shared profile,
one window per page, warm-up hold, failure learning).

### Concurrency and queueing

- Playwright connections are **not queued**: each is its own async task and its own CDP connection, running in
  parallel. The practical limit is changedetection's `FETCH_WORKERS` and the machine's resources.
- Only the server's own tab is serialized, by `_driver_lock` (a plain `asyncio.Lock`, FIFO). It guards warm-ups, the
  background repair warm-up and Chrome restarts. FastAPI/uvicorn do no queueing (one uvicorn worker, one event loop).
- Checks that do not need a warm-up never take the lock. With two workers at most one check waits, for at most
  about `WARMUP_TIMEOUT` plus a few seconds of scrolling.

### Can a site tell it is Playwright?

Possibly. The warm-up only obtains clean cookies from the SeleniumBase tab; Playwright's tab then uses them.
Verified: `navigator.webdriver` is false, UA and platform agree (Linux), ResearchGate returned 200 on every
Playwright fetch after a warm-up (one site, one IP). Not addressed: Playwright sends `Runtime.enable` (detectable
side effect that SeleniumBase's tab avoids), injects its own scripts and worlds, sets a 1280x720 viewport,
bypass-CSP and ignore-certificate-errors, and behaves mechanically. Cookies are also bound to IP and user agent.
If a site starts challenging again, the next step is a patched Playwright inside changedetection (Patchright or
rebrowser-patches).

### Gotchas (learned the hard way)

- `tab.get()` has no timeout and ignores extra keyword arguments (they only change its code path): wrap it in
  `asyncio.wait_for`.
- On Linux, `cdp_driver.start_async` starts its own 1366x768 Xvfb (`:1001`) unless it gets `headed=True`;
  `headless=False` alone is not enough. Chrome then ignores the base image's `:99` and the VNC viewer shows an
  empty screen. Check with `pgrep -a Xvfb`: there must be exactly one.
- `cdp_driver.start_async` silently ignores unknown keyword arguments. Chrome flags go in `browser_args`;
  `extra_chromium_args` is not a parameter, which made `EXTRA_CHROME_ARGS` a no-op until it was fixed.
- Scroll gestures and screenshots need a *visible* tab. `bring_to_front()` in the warm-up hides other tabs of the
  same window, hence one window per client page. changedetection's chunked screenshot (enlarged viewport,
  `request_gc`, JPEG chunks) hung with `Page.screenshot: Timeout 30000ms exceeded` before that fix.
- Never block the client's incoming messages while warming. Chrome pauses new targets until the client sends its
  resume message; holding all messages deadlocks. Only the `Page.navigate` message is held.
- DataDome answers a request without a cookie with 403 and a device-check page that clears itself in ~2 s and
  reloads into a 200. changedetection 0.60.9 judges a fetch on the *latest* main-frame response, so that transient
  403 can still be a successful check, but the relay records it as a failure (harmless extra repair warm-up).
- changedetection's default user agent for browser fetches is `None` (no override). A UA set in its settings or in a
  watch's headers would contradict the real Chrome and invalidate UA-bound cookies.
- Chrome writes cookies to disk lazily (~30 s): stopping the container right after a warm-up can lose the cookie.
- Chrome clears `extensions_crx_cache` itself at startup; the profile trim never touches extensions.
- Status-based failure learning needs the relay to see `Network.responseReceived`; verified for Playwright pages.

### Where things are

- `server.py`: config (env), Chrome launch/restart, profile trim, CAPTCHA/Cloudflare/DataDome handling and the
  warm-up (`_warm_url`, `_wait_for_cloudflare`, `_classify_page`), per-host memory (`_hosts`, `_should_warm`,
  `_note_document_response`), the `/cdp` relay (`cdp_ws_proxy`), `/health`.
- `Dockerfile`: `superkeyor/chromiumkit` (sibling repo: Debian Chromium, Xvfb, x11vnc/noVNC via `vnc-start`) plus
  Miniforge and tini, laid out like the sibling `dockerapp_template`. `start.sh` runs `vnc-start`, then the server.
- `test.py` (pytest against a running server), `test.sh` (manual commands), `docker-compose.yml`
  (+ override for local dev), `upload` (git commit/push plus multi-arch Docker Hub push), `start.sh`.

### Developing and testing

Build a dev image once (`docker build -t sbfetcher:dev .`), then iterate without rebuilding by mounting the file:
`docker run -d --shm-size=2g --cap-add=SYS_ADMIN -p 3300:3000 -v $PWD/server.py:/app/server.py:ro sbfetcher:dev`.
Run `BASE_URL=http://localhost:3300 pytest test.py`. For protocol-level checks run a real Playwright script with
`DEBUG=pw:protocol` (it prints every CDP message, which also proves what the relay forwards). Use a fresh profile
(new container) for cold-start behavior, and test screenshots, not just page text: hidden-tab bugs only show there.
Dependencies in `environment.yml` are deliberately unpinned (`>=`): a rebuild takes the newest SeleniumBase, FastAPI,
uvicorn and websockets. When something needs fixing, rebuild first so you test against the then-latest versions, and raise the
minimum in `environment.yml` to the version you verified. Last verified: Python 3.14.9, SeleniumBase 4.56.4, websockets 17.3.
Lint with `uvx pyflakes server.py`. Do not run `./upload` unless the user asks (it commits and pushes).
