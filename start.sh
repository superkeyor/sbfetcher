#!/usr/bin/env bash
# =============================================================================
# start.sh — entrypoint for seleniumbase-fetcher container
# =============================================================================
set -euo pipefail

echo "==> SeleniumBase Pure CDP Fetcher"
echo "    Host:    ${HOST:-0.0.0.0}"
echo "    Port:    ${PORT:-3000}"
echo "    Warm-up timeout: ${WARMUP_TIMEOUT:-30}s"
echo "    Log:     ${LOG_LEVEL:-INFO}"
echo "    VNC:     ${ENABLE_VNC:-false} (noVNC on container port 6080)"

# Verify Chromium is available before starting the server
if ! command -v chromium &>/dev/null; then
    echo "ERROR: chromium not found on PATH" >&2
    exit 1
fi
echo "    Chrome:  $(chromium --version 2>/dev/null)"

# Starts the ONE Xvfb (DISPLAY=:99, set in the base image), sized like the Chrome window;
# plus x11vnc + noVNC if ENABLE_VNC=true. Exits 1 if Xvfb fails.
export SCREEN_GEOMETRY="${WINDOW_WIDTH:-1920}x${WINDOW_HEIGHT:-1080}x24"
vnc-start || exit 1

exec python /app/server.py
