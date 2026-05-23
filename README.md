# sbfetcher

A **headless Chrome REST API** backed by
[SeleniumBase](https://seleniumbase.io/) Pure CDP Mode + [FastAPI](https://fastapi.tiangolo.com/),
designed as a drop-in fetcher for [changedetection.io](https://github.com/dgtlmoon/changedetection.io).

Supports **linux/amd64** and **linux/arm64** (OCI/Oracle Cloud, Raspberry Pi, M-series Mac via Rosetta).

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/content` | Render URL → return full HTML |
| `POST` | `/chromium/content` | Alias (browserless v1) |
| `POST` | `/screenshot` | Render URL → return PNG/JPEG |
| `POST` | `/chromium/screenshot` | Alias (browserless v1) |
| `POST` | `/v1` | FlareSolverr-compatible endpoint |
| `WS`   | `/cdp` | Chrome CDP proxy (for `PLAYWRIGHT_DRIVER_URL`) |
| `GET`  | `/health` | Liveness probe |
| `GET`  | `/metrics` | Request/error counters |
| `GET`  | `/docs` | Swagger UI |

### Request body (both `/content` and `/screenshot`)

```json
{
  "url": "https://example.com",
  "gotoOptions": {
    "waitUntil": "domcontentloaded",
    "timeout": 30000
  },
  "waitForSelector": "#content",
  "waitForTimeout": 2000,
  "userAgent": "Mozilla/5.0 ...",
  "cookies": [{ "name": "session", "value": "abc", "domain": ".example.com" }],
  "headers": { "Accept-Language": "en-US" },
  "javascript": "window.scrollTo(0, document.body.scrollHeight)",
  "viewport": { "width": 1920, "height": 1080 }
}
```

Screenshot-only extras:

```json
{
  "fullPage": true,
  "type": "png",
  "quality": 85
}
```

---

## Quick start

### docker compose (with changedetection.io)

```bash
docker compose up -d
```

- changedetection.io UI: http://localhost:5000
- Fetcher API:           http://localhost:3000
- Swagger docs:          http://localhost:3000/docs

### changedetection.io integration

Both fetch methods work simultaneously — choose per watch in the cd.io UI:

| Method | cd.io setting | Value |
|--------|--------------|-------|
| **Playwright** (stealthy Chrome CDP) | `PLAYWRIGHT_DRIVER_URL` env var | `ws://sbfetcher:3000/cdp` |
| **FlareSolverr** (bypass bot detection) | `FLARESOLVERR_URL` env var | `http://sbfetcher:3000` |

Both are pre-configured in the included `docker-compose.yml`.

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
| `PAGE_TIMEOUT` | `30` | Navigation timeout (seconds) |
| `WAIT_AFTER_LOAD` | `1` | Extra settle time after load (seconds) |
| `WINDOW_WIDTH` | `1280` | Chrome viewport width |
| `WINDOW_HEIGHT` | `900` | Chrome viewport height |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `EXTRA_CHROME_ARGS` | `` | Space-separated extra Chrome flags |
| `HEADED` | `false` | Run Chrome in a virtual Xvfb display (`true` for debugging) |
| `CHROME_USER_DATA_DIR` | `/app/chromiumprofile` | Chrome profile dir; defaults to the folder baked into the image |

---

## Timeout configuration

`PAGE_TIMEOUT` (env var, default `30` s) is the global navigation timeout. Per-request fields override it with no upper cap.

| Integration | Where to set | Field / mechanism |
|-------------|-------------|-------------------|
| **FlareSolverr** `/v1` | cd.io UI → watch → **Requests** tab → *Request timeout* | `maxTimeout` (ms) sent automatically by cd.io |
| **Playwright** `/cdp` | cd.io env var `CONNECTION_TIMEOUT` (ms) | Playwright `connect_over_cdp(timeout=…)` — handled client-side |
| **`/content` / `/screenshot`** direct API | request body | `gotoOptions.timeout` (ms) — navigation; `waitForTimeout` (ms) — extra settle |

### changedetection.io example

For a slow page using the FlareSolverr fetcher, set **Request timeout = 120 s** in the watch's Requests tab. cd.io will send `maxTimeout: 120000` and sbfetcher will honour it.

For the Playwright fetcher, set `CONNECTION_TIMEOUT=120000` on the changedetection service:

```yaml
changedetection:
  environment:
    CONNECTION_TIMEOUT: "120000"
    PLAYWRIGHT_DRIVER_URL: "ws://sbfetcher:3000/cdp"
```

---

## Notes

- **Pure CDP Mode**: Chrome is driven directly via the Chrome DevTools Protocol — no WebDriver, no chromedriver. This makes detection extremely difficult.
- **`/cdp` WebSocket proxy**: exposes Chrome’s internal CDP endpoint at a fixed address. changedetection.io’s `connect_over_cdp()` connects through it. The proxy transparently forwards all CDP messages and automatically reconnects on Chrome restart.
- **FlareSolverr `/v1`**: implements the FlareSolverr v1 API contract (`request.get`, `sessions.*`). Set `FLARESOLVERR_URL=http://sbfetcher:3000` in changedetection.io.
- **`HEADED=true`**: starts an Xvfb virtual framebuffer (`:99`) and runs Chrome non-headlessly inside it — useful for debugging bot-detection issues without a real display.
- **ARM64**: uses `chromium` from apt (Google does not ship ARM64 `.deb` packages). amd64 uses `google-chrome-stable` from Google’s apt repo.
- The CDP driver is serialized for the `/content`, `/screenshot`, and `/v1` endpoints (one request at a time). The `/cdp` WebSocket proxy is independent and does not go through the lock. Scale horizontally with multiple containers + a load balancer if throughput matters.
- `SYS_ADMIN` capability is required for Chrome’s namespace sandbox inside Docker. If your host policy forbids it, add `--no-sandbox` via `EXTRA_CHROME_ARGS`.
