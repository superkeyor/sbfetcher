# sbfetcher

A **headed Chrome behind a CDP WebSocket relay**, backed by
[SeleniumBase](https://seleniumbase.io/) Pure CDP Mode + [FastAPI](https://fastapi.tiangolo.com/),
designed as a stealthy Playwright browser for [changedetection.io](https://github.com/dgtlmoon/changedetection.io).

Supports **linux/amd64** and **linux/arm64** (OCI/Oracle Cloud, Raspberry Pi, M-series Mac via Rosetta).

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `WS`   | `/cdp` | Chrome CDP relay (for `PLAYWRIGHT_DRIVER_URL`) |
| `GET`  | `/health` | Liveness probe — `503` (and a restart attempt) when Chrome is down |
| `GET`  | `/docs` | Swagger UI |

---

## Quick start

### docker compose (with changedetection.io)

```bash
docker compose up -d
```

- changedetection.io UI: http://localhost:5000
- Fetcher:               http://localhost:3000
- Swagger docs:          http://localhost:3000/docs

### changedetection.io integration

| cd.io setting | Value |
|--------------|-------|
| `PLAYWRIGHT_DRIVER_URL` env var | `ws://sbfetcher:3000/cdp` |

Pre-configured in the included `docker-compose.yml`. Select **Playwright Chromium/Javascript** as the fetch method per watch.

### Standalone

```bash
docker run -d \
  --name sbfetcher \
  --shm-size=512m \
  --cap-add=SYS_ADMIN \
  -p 3000:3000 \
  superkeyor/sbfetcher:latest
```

---

## Build

### Single-arch (native)

```bash
docker build -t superkeyor/sbfetcher:latest .
```

### Multi-arch (amd64 + arm64)

```bash
./upload
```

`upload` uses `docker buildx` to build and push `linux/amd64` + `linux/arm64` to Docker Hub in one step.
Requires a multi-platform builder:

```bash
docker buildx create --use --name multiarch-builder
docker login
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | Listening port |
| `HOST` | `0.0.0.0` | Bind address |
| `WARMUP_URLS` | `` | Comma-separated Cloudflare sites revisited to keep `cf_clearance` fresh (empty = off) |
| `WARMUP_INTERVAL` | `60` | Minutes between warm-up rounds |
| `WARMUP_TIMEOUT` | `30` | Warm-up only: seconds to load a page and get through Cloudflare |
| `CDP_SHARE_DEFAULT_CONTEXT` | `true` | Map Playwright's `new_context()` onto the persistent profile |
| `USER_AGENT` | `` | Override Chrome's UA (empty = real UA; recommended) |
| `WINDOW_WIDTH` | `1920` | Chrome window / Xvfb width |
| `WINDOW_HEIGHT` | `1080` | Chrome window / Xvfb height |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `EXTRA_CHROME_ARGS` | `` | Space-separated extra Chrome flags |
| `HEADED` | `true` | Run Chrome in a virtual Xvfb display instead of `--headless` |
| `CHROME_USER_DATA_DIR` | `/app/chromiumprofile` | Chrome profile dir; defaults to the folder baked into the image |
| `DEBUG_SNAPSHOT` | `false` | Save HTML + PNG of every warm-up visit to `DEBUG_SNAPSHOT_DIR` |

Playwright timeouts are controlled by changedetection.io (e.g. its `CONNECTION_TIMEOUT` env var), not by sbfetcher.

---

## Cloudflare-protected sites

1. **Run from a residential IP.** Cloudflare hard-blocks many datacenter ranges with a 403 ("Sorry, you have been blocked") that no browser can get past.
2. **Add the site to `WARMUP_URLS`** (e.g. `https://www.researchgate.net/`). The warm-up task solves the challenge in the server's own tab, and the `cf_clearance` cookie lands in the shared profile that changedetection's Playwright pages use.
3. **Don't set a custom User-Agent on the watch.** `cf_clearance` is bound to the UA (and IP); a different UA throws it away.
4. **Keep check intervals moderate** (ResearchGate: 6 h or more).

---

## Notes

- **Pure CDP Mode**: Chrome is launched and driven over the Chrome DevTools Protocol — no WebDriver, no chromedriver, no `--enable-automation`.
- **`/cdp` relay**: forwards CDP messages between Playwright and Chrome. With `CDP_SHARE_DEFAULT_CONTEXT=true` it answers `Target.createBrowserContext` with Chrome's default context, so each check reuses the persistent profile instead of a fresh incognito context; pages the client opened are closed when it disposes the context or disconnects. Contexts requested with their own proxy are left alone.
- **One browser, many clients**: each `/cdp` client gets its own CDP connection to the same Chrome. With the shared context, concurrent clients see each other's pages (Playwright auto-attaches to every page in the context); use `FETCH_WORKERS=1` in changedetection.io if that causes trouble.
- **Self-healing**: `/health` and new `/cdp` connections restart Chrome if it has died. Docker itself does not restart unhealthy containers.
- **ARM64**: uses Playwright's Chromium build (Google does not ship ARM64 `.deb` packages). amd64 uses `google-chrome-stable` from Google's apt repo.
- `SYS_ADMIN` capability is required for Chrome's namespace sandbox inside Docker. If your host policy forbids it, add `--no-sandbox` via `EXTRA_CHROME_ARGS`.
