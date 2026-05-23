# =============================================================================
# Base Image
# =============================================================================
# Miniforge (Ubuntu + mamba + conda environments)
# https://hub.docker.com/r/condaforge/miniforge3/tags
# Use fixed version (vs. latest) to avoid freq rebuild
FROM condaforge/miniforge3:26.1.1-2

# =============================================================================
# Environment Variables  
# =============================================================================
ENV ENABLE_CRON=false
ENV TZ=America/Chicago
ENV FLASK_ENV=production

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# =============================================================================
# System Dependencies
# =============================================================================
RUN apt-get update && \
    if [ "$ENABLE_CRON" = "true" ]; then \
        apt-get install -y --no-install-recommends tzdata cron; \
    else \
        apt-get install -y --no-install-recommends tzdata; \
    fi && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

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
COPY . .

RUN if [ "$ENABLE_CRON" = "true" ]; then crontab /app/crontab; fi

# =============================================================================
# Container Configuration
# =============================================================================
# Expose port for Flask application
EXPOSE 5000
CMD ["bash", "/app/start.sh"]
