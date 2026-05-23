#!/usr/bin/env bash
# =============================================================================
# start.sh — entrypoint for seleniumbase-fetcher container
# =============================================================================
set -euo pipefail

echo "==> SeleniumBase Pure CDP Fetcher"
echo "    Host:    ${HOST:-0.0.0.0}"
echo "    Port:    ${PORT:-3000}"
echo "    Timeout: ${PAGE_TIMEOUT:-30}s"
echo "    Log:     ${LOG_LEVEL:-INFO}"

# Verify Chrome is available before starting the server
if ! command -v chromium-browser &>/dev/null; then
    echo "ERROR: chromium-browser not found on PATH" >&2
    exit 1
fi
echo "    Chrome:  $(chromium-browser --version 2>/dev/null)"

exec python /app/server.py
