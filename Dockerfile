# syntax=docker/dockerfile:1.6
# Reproducible runtime for good360-monitor.
# The Playwright base image ships with a matched Chromium build — this is the
# #1 lesson from the April 13 postmortem (venv/browser drift caused a 3-day outage).

FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York \
    WORKDIR=/app/workdir \
    DEVTOOLS_CHROME_EXECUTABLE=/usr/bin/google-chrome-stable

WORKDIR /app

# Chrome DevTools MCP requires Node.js v20.19+ plus Chrome stable/current.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs google-chrome-stable \
    && node --version \
    && npm --version \
    && google-chrome-stable --version \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + compose plugin (no daemon) — used by the admin dashboard to
# restart sibling services after a settings write. Static binaries so we
# don't depend on what's in apt for the base distro.
ENV DOCKER_VERSION=27.5.1 \
    DOCKER_COMPOSE_VERSION=2.32.4
RUN set -eux; \
    curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
        | tar -xzC /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
        "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-x86_64"; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker --version; \
    docker compose version

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Runtime state lives in a volume (never in the image).
RUN mkdir -p /app/workdir /app/workdir/browser_screenshots /app/workdir/intake_form_submissions

# Health check: the real scanner health endpoint (see healthcheck.py).
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python /app/healthcheck.py || exit 1

# Default command: the monitor loop. Override in docker-compose per service.
CMD ["python", "good360_monitor.py"]
