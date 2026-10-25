# =============================================================================
# Base Image
# =============================================================================
# Miniforge3 (Ubuntu + mamba + conda-forge)
# https://hub.docker.com/r/condaforge/miniforge3/tags
# Fixed version to avoid unexpected rebuilds
FROM condaforge/miniforge3:26.1.1-2

# =============================================================================
# Build Args
# =============================================================================
# TARGETARCH is auto-injected by docker buildx for multi-platform builds.
# It must be declared here so the Chrome installation case statement below
# can read it. Values: amd64 | arm64
ARG TARGETARCH

# =============================================================================
# Labels
# =============================================================================
LABEL maintainer="superkeyor"
LABEL description="SeleniumBase Pure CDP + FastAPI — headed Chrome behind a CDP WebSocket relay for changedetection.io Playwright"
LABEL org.opencontainers.image.title="sbfetcher"

# =============================================================================
# Environment Variables
# =============================================================================
ENV TZ=America/Chicago
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Server defaults — all overridable at runtime via docker run -e or compose
ENV HOST=0.0.0.0
ENV PORT=3000
# Warm-up navigation + Cloudflare wait (seconds)
ENV WARMUP_TIMEOUT=30
# Comma-separated Cloudflare sites to revisit every WARMUP_INTERVAL minutes (empty = off)
ENV WARMUP_URLS=""
ENV WARMUP_INTERVAL=60
# Map Playwright's new_context() onto the persistent profile
ENV CDP_SHARE_DEFAULT_CONTEXT=true
# Empty = Chrome's real user-agent
ENV USER_AGENT=""
ENV WINDOW_WIDTH=1920
ENV WINDOW_HEIGHT=1080
ENV LOG_LEVEL=INFO
ENV HEADED=true
# Persistent Chrome profile directory.
# Default points to the chromiumprofile/ folder baked into the image.
# Override at runtime to use a host-mounted volume instead.
ENV CHROME_USER_DATA_DIR=/app/chromiumprofile
# Extra space-separated Chrome flags, e.g. --proxy-server=http://proxy:8080
ENV EXTRA_CHROME_ARGS=""
# Debug snapshots: set DEBUG_SNAPSHOT=true to save HTML+PNG after every warm-up visit.
# NOTE: DEBUG_SNAPSHOT_DIR defaults to /tmp/sbfetcher which lives on the tmpfs
# mount defined in docker-compose.yml — snapshots are RAM-only and lost on restart.
# Override DEBUG_SNAPSHOT_DIR to a volume-backed path to persist them.
ENV DEBUG_SNAPSHOT=false
ENV DEBUG_SNAPSHOT_DIR=/tmp/sbfetcher

# =============================================================================
# System Dependencies
# =============================================================================
# tzdata for timezone config; curl for HEALTHCHECK; gnupg/ca-certificates for
# the Google Chrome apt repo; remaining libs are Chrome shared-library deps.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
        curl \
        gnupg \
        wget \
        unzip \
        fonts-liberation \
        libasound2t64 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils \
        xvfb \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# Chrome Installation
# =============================================================================
# amd64 → Google Chrome stable (official .deb from Google)
# arm64 → Playwright's pre-built Chromium binary.
#          Ubuntu Noble arm64 ships only a snap-redirect stub for chromium
#          (package 2:1snap1-*), which cannot run inside Docker.  We use pip
#          to drive the Playwright browser download, then remove the Python
#          package — only the binary is kept.
# Pure CDP Mode drives the browser directly — no chromedriver required.
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) \
            curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
                | gpg --dearmor -o /usr/share/keyrings/google-linux-keyring.gpg; \
            echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-keyring.gpg] \
                  https://dl.google.com/linux/chrome/deb/ stable main" \
                > /etc/apt/sources.list.d/google-chrome.list; \
            apt-get update && \
            apt-get install -y --no-install-recommends google-chrome-stable && \
            rm -rf /var/lib/apt/lists/*; \
            ln -sf /usr/bin/google-chrome-stable /usr/bin/chromium-browser; \
            ;; \
        arm64) \
            pip install --quiet playwright; \
            PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers playwright install chromium; \
            CHROMIUM_BIN=$(find /opt/playwright-browsers -type f -name 'chrome' -perm /111 \
                           2>/dev/null | head -1); \
            [ -n "$CHROMIUM_BIN" ] || { echo "Playwright Chromium binary not found" >&2; exit 1; }; \
            ln -sf "$CHROMIUM_BIN" /usr/bin/chromium-browser; \
            pip uninstall -y playwright; \
            ;; \
        *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac

# Verify the Chrome binary is on PATH
RUN chromium-browser --version

# =============================================================================
# Application Setup & Python Package Installation
# =============================================================================
WORKDIR /app

COPY environment.yml .
RUN mamba env create -f environment.yml && \
    echo "source activate app" >> ~/.bashrc && \
    mamba clean -afy

ENV PATH=/opt/conda/envs/app/bin:$PATH

# =============================================================================
# Application Files
# =============================================================================
COPY server.py start.sh ./
# Chrome profile placeholder — copy your profile into chromiumprofile/ before
# building to ship a pre-configured profile baked into the image.
# An empty folder results in Chrome creating a fresh profile on first launch.
COPY chromiumprofile/ /app/chromiumprofile/

# Ensure the entrypoint script is executable
RUN chmod +x start.sh

# =============================================================================
# Container Configuration
# =============================================================================
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

CMD ["bash", "/app/start.sh"]
