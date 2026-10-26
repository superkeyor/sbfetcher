# sbfetcher

A **headed Chrome behind a CDP WebSocket relay**, backed by
[SeleniumBase](https://seleniumbase.io/) Pure CDP Mode + [FastAPI](https://fastapi.tiangolo.com/),
designed as a stealthy Playwright browser for [changedetection.io](https://github.com/dgtlmoon/changedetection.io).

Supports **linux/amd64** and **linux/arm64** (OCI/Oracle Cloud, Raspberry Pi, M-series Mac via Rosetta).

How it works inside (architecture, concurrency, gotchas, development): see [AGENTS.md](AGENTS.md).

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
  -p 3001:6080 \
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
| `WARMUP` | `true` | Master switch for the whole warm-up feature (see below). `false` = no warm-up, no learning, no state file; `/cdp` is a plain relay. The other `WARMUP_*` settings only apply while this is `true` |
| `WARMUP_TIMEOUT` | `30` | Warm-up only: seconds to load the site root and get through the bot check |
| `WARMUP_COOLDOWN` | `300` | A needed warm-up is skipped if the host was warmed this many seconds ago (`0` = no limit) |
| `WARMUP_ALWAYS_AFTER` | `2` | This many failed direct visits within `WARMUP_ALWAYS_DAYS` ... |
| `WARMUP_ALWAYS_DAYS` | `90` | ... makes a host "frequent": it is warmed before every check |
| `WARMUP_STATE_FILE` | `<profile dir>/sbfetcher-hosts.json` | Per-host memory (empty = memory only) |
| `CDP_SHARE_DEFAULT_CONTEXT` | `true` | Map Playwright's `new_context()` onto the persistent profile |
| `USER_AGENT` | `` | Override Chrome's UA (empty = real UA; recommended) |
| `PROFILE_MAX_MB` | unset (= off) | Optional size limit for the Chrome profile. If it is bigger when the container starts, the cache folders are deleted (cached pages, images); cookies, site storage, extensions and the warm-up memory are kept. Leave unset or `0` to never trim |
| `WINDOW_WIDTH` | `1920` | Chrome window / Xvfb width (`start.sh` sets the base image's `SCREEN_GEOMETRY` from these) |
| `WINDOW_HEIGHT` | `1080` | Chrome window / Xvfb height |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `EXTRA_CHROME_ARGS` | `` | Space-separated extra Chrome flags, e.g. `--load-extension=/ext` (mount the unpacked extension folder at `/ext`) |
| `HEADED` | `true` | Run Chrome in a virtual Xvfb display instead of `--headless` |
| `CHROME_USER_DATA_DIR` | `/app/chromiumprofile` | Chrome profile dir; defaults to the folder baked into the image |
| `DEBUG_SNAPSHOT` | `false` | Save HTML + PNG of every warm-up visit to `DEBUG_SNAPSHOT_DIR` |
| `ENABLE_VNC` | `false` | Serve a live view of the headed Chrome on container port 6080 (see below). Needs `HEADED=true` |
| `VNC_PASSWORD` | `` | Password for the VNC viewer (empty = none) |

Playwright timeouts are controlled by changedetection.io (e.g. its `CONNECTION_TIMEOUT` env var), not by sbfetcher.

---

## Bot-protected sites (Cloudflare, DataDome, ...)

No configuration needed: sbfetcher learns per host whether it needs a warm-up.

- **First check of a host:** the relay pauses the navigation, visits the site root in the server's own tab (solving any challenge and scrolling a little, like a person), then lets the check through. The fresh cookies land in the shared profile that changedetection's Playwright pages use. This adds a few seconds, up to `WARMUP_TIMEOUT` if the site is slow.
- **Clean first response:** the next checks go **direct**, no warm-up.
- **A direct check gets HTTP 403 / 429 / 503** (cookie expired or invalidated): that check is lost, since changedetection already received the error. sbfetcher remembers it and renews the cookie in the background, and the following checks go direct again.
- **Frequent challengers:** a host with `WARMUP_ALWAYS_AFTER` such failures within `WARMUP_ALWAYS_DAYS` is warmed before every check. Old failures age out, after which it drifts back to direct.
- **Never warmed:** non-http(s) URLs, `localhost`, private/LAN addresses, dotless names and `*.local`.
- **Memory:** `sbfetcher-hosts.json` in the Chrome profile folder. Keep that folder on a volume; wiping it resets the memory together with the cookies.
- **A page that always returns 403** (genuinely forbidden) costs one extra root visit per check.
- **Stopping the container within ~30 s of a warm-up** can lose the fresh cookie, because Chrome writes cookies to disk lazily. That costs one failed check, which is then learned and repaired.

Also:
1. **Run from a residential IP.** Cloudflare hard-blocks many datacenter ranges with a 403 ("Sorry, you have been blocked") that no browser can get past.
2. **Don't set a custom User-Agent on the watch.** `cf_clearance` is bound to the UA (and IP); a different UA throws it away.
3. **Keep check intervals moderate** (ResearchGate: 6 h or more).

---

## Watching Chrome (VNC)

Set `ENABLE_VNC=true`, publish the viewer's container port 6080 as 3001 (`-p 3001:6080`; already in
`docker-compose.yml`) and open `http://<host>:3001`. The viewer comes from the
[chromiumkit](https://hub.docker.com/r/superkeyor/chromiumkit) base image (x11vnc + noVNC). The page (noVNC) shows the Xvfb display with all Chrome
windows: warm-ups, CAPTCHA solving, changedetection's pages. You can also click and type in it, for example to
solve a challenge or log in to a site once (the cookies land in the shared profile).

- Port 6080 (3001 on the host) serves only the viewer; x11vnc itself listens only inside the container. With
  `ENABLE_VNC=false` nothing listens there.
- Anyone who can reach port 3001 can control Chrome (as with `/cdp` on 3000). Set `VNC_PASSWORD` if the port is not
  private, and keep it off untrusted networks either way: the VNC password is not encryption.
- Mouse and keyboard input during a warm-up can disturb it. Leave `ENABLE_VNC=false` when you do not need it.

---

## Notes

- **Pure CDP Mode**: Chrome is launched and driven over the Chrome DevTools Protocol — no WebDriver, no chromedriver, no `--enable-automation`.
- **`/cdp` relay**: forwards CDP messages between Playwright and Chrome. With `CDP_SHARE_DEFAULT_CONTEXT=true` it answers `Target.createBrowserContext` with Chrome's default context, so each check reuses the persistent profile instead of a fresh incognito context; pages the client opened are closed when it disposes the context or disconnects. Contexts requested with their own proxy are left alone.
- **Profile size**: the profile grows with every site changedetection visits (cache, cookies, site storage). Check it with `du -sh` on the profile volume. `PROFILE_MAX_MB` trims the caches at container start when you set it; it never touches cookies, site storage or extensions, so if the profile is still over the limit after a trim, the log says so.
- **One window per page**: with the shared context, every page a client opens gets its own Chrome window. A tab hidden behind another tab renders no frames, which made `Page.screenshot` time out when the warm-up tab came to the front.
- **One browser, many clients**: each `/cdp` client gets its own CDP connection to the same Chrome. With the shared context, concurrent clients see each other's pages (Playwright auto-attaches to every page in the context); use `FETCH_WORKERS=1` in changedetection.io if that causes trouble.
- **Self-healing**: `/health` and new `/cdp` connections restart Chrome if it has died. Docker itself does not restart unhealthy containers.
- **Browser**: Debian's Chromium from the [chromiumkit](https://hub.docker.com/r/superkeyor/chromiumkit) base image, on both amd64 and arm64 (Google does not ship ARM64 `.deb` packages, so one browser for both). Google Chrome is no longer used.
- `SYS_ADMIN` capability is required for Chrome's namespace sandbox inside Docker. If your host policy forbids it, add `--no-sandbox` via `EXTRA_CHROME_ARGS`.
