# =============================================================================
# SeeQL — MySQL DBA Agent Docker Image
# =============================================================================
# Build:  docker build -t seeql .
# Run:    See README.md for full docker run command with all env vars.
# =============================================================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[api]" && \
    pip install --no-cache-dir google-genai

# Copy package directories explicitly (avoid .env leaks)
COPY config/ config/
COPY collectors/ collectors/
COPY storage/ storage/
COPY parsers/ parsers/
COPY scheduler/ scheduler/
COPY api/ api/
COPY agent/ agent/
COPY alerting/ alerting/
COPY templates/ templates/
COPY static/ static/
COPY main.py .

# Data and logs are expected to be mounted as volumes
RUN mkdir -p /app/data /app/logs

# Non-root user
RUN useradd --create-home agent && chown -R agent:agent /app

USER agent

# API + Prometheus metrics port
EXPOSE 8080

# Healthcheck via API
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# Default: scheduler + API server
CMD ["python", "main.py", "--api"]
