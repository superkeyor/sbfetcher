# =============================================================================
# Base Image
# =============================================================================
# chromiumkit: Debian + Chromium + Xvfb + x11vnc + noVNC (`vnc-start`, ENABLE_VNC, VNC_PASSWORD, port 6080)
# https://hub.docker.com/r/superkeyor/chromiumkit
# Miniforge (mamba + conda environments) is added below, as in dockerapp_template.
# Install steps borrowed from the official (Ubuntu-only) miniforge3 image:
# https://github.com/conda-forge/miniforge-images/blob/main/ubuntu/Dockerfile
FROM superkeyor/chromiumkit:latest
ARG MINIFORGE_VERSION=26.9.4-0

# =============================================================================
# Labels
# =============================================================================
LABEL maintainer="superkeyor"
LABEL description="SeleniumBase Pure CDP + FastAPI — headed Chrome behind a CDP WebSocket relay for changedetection.io Playwright"
LABEL org.opencontainers.image.title="sbfetcher"

# =============================================================================
# Miniforge
# =============================================================================
ENV CONDA_DIR=/opt/conda
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
ENV PATH=${CONDA_DIR}/bin:${PATH}

# tini: PID 1 init (signal forwarding, zombie reaping), as in the official image
RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates tini && \
    rm -rf /var/lib/apt/lists/* && \
    wget --no-hsts -q "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-${MINIFORGE_VERSION}-Linux-$(uname -m).sh" -O /tmp/miniforge.sh && \
    bash /tmp/miniforge.sh -b -p ${CONDA_DIR} && \
    rm /tmp/miniforge.sh && \
    find ${CONDA_DIR} -follow -type f \( -name '*.a' -o -name '*.pyc' \) -delete && \
    conda clean -afy

# =============================================================================
# Environment Variables
# =============================================================================
ENV TZ=America/Chicago
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
# Server defaults — all overridable at runtime via docker run -e or compose
ENV HOST=0.0.0.0
ENV PORT=3000
# Master switch for the whole warm-up feature. true: a new host, or one whose last visit failed, has its
# site root visited first (bot check solved, cookies renewed); hosts that went through cleanly are fetched
# directly. false: no warm-up, no learning from failures, no state file; /cdp is a plain relay to Chrome.
ENV WARMUP=true
# Warm-up navigation + bot-check wait (seconds)
ENV WARMUP_TIMEOUT=30
# Skip a needed warm-up if the host was warmed less than this many seconds ago (0 = no limit)
ENV WARMUP_COOLDOWN=300
# A host with this many failed direct visits within WARMUP_ALWAYS_DAYS is warmed before every check
ENV WARMUP_ALWAYS_AFTER=2
ENV WARMUP_ALWAYS_DAYS=90
# Per-host memory; default is sbfetcher-hosts.json inside CHROME_USER_DATA_DIR
# ENV WARMUP_STATE_FILE=/app/chromiumprofile/sbfetcher-hosts.json
# Map Playwright's new_context() onto the persistent profile
ENV CDP_SHARE_DEFAULT_CONTEXT=true
# Empty = Chrome's real user-agent
ENV USER_AGENT=""
# Chrome window size; start.sh also sizes the Xvfb display (SCREEN_GEOMETRY) from these
ENV WINDOW_WIDTH=1920
ENV WINDOW_HEIGHT=1080
ENV LOG_LEVEL=INFO
ENV HEADED=true
# Optional size limit for the Chrome profile, in MB. Unset or 0 = never trim (default). If the profile is
# bigger at container start, its cache folders are deleted (cached pages, images; Chrome re-creates them);
# cookies, site storage, extensions and the warm-up memory are kept. Example: PROFILE_MAX_MB=2000
# ENV PROFILE_MAX_MB=0
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
# VNC viewer (from the base image): ENABLE_VNC=true serves noVNC on port 6080; VNC_PASSWORD (empty = none).
# Anyone who can reach that port can control Chrome.

# =============================================================================
# System Dependencies
# =============================================================================
# tzdata for timezone config; curl for HEALTHCHECK. Chromium and its libraries come from the base image.
RUN export DEBIAN_FRONTEND=noninteractive && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo "$TZ" > /etc/timezone && \
    apt-get update && \
    apt-get install -y --no-install-recommends tzdata curl && \
    rm -rf /var/lib/apt/lists/*

# Verify the Chromium binary (Debian's, from the base image) is on PATH; SeleniumBase finds it there
RUN chromium --version

# =============================================================================
# Application Setup & Python Package Installation
# =============================================================================
WORKDIR /app
COPY environment.yml .

RUN mamba env create -y -f environment.yml && \
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

# =============================================================================
# Container Configuration
# =============================================================================
# 3000: CDP relay + /health. The base image already exposes 6080 (noVNC, only with ENABLE_VNC=true).
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["bash", "/app/start.sh"]
