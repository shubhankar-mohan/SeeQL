# syntax=docker/dockerfile:1.7
# =============================================================================
# SeeQL — LLM-powered MySQL DBA agent
#
# Multi-arch (linux/amd64, linux/arm64), multi-stage build.
#
# Variants (pick at build time via --build-arg INSTALL_EXTRAS=...):
#   api       (default)  — generic image, works against any MySQL 8.0+
#   api,gcp              — adds Cloud Monitoring and Cloud Logging support
#
# google-genai (Vertex AI / Gemini backend) is installed in every variant so
# the documented default model (gemini-2.0-flash) works out of the box.
#
# Build args:
#   SEEQL_VERSION    Image version label (default: dev)
#   VCS_REF          Git commit SHA (default: unknown)
#   BUILD_DATE       ISO 8601 build timestamp
#   INSTALL_EXTRAS   Pip extras to install (default: api)
#
# Local build examples:
#   docker build -t seeql:dev .
#   docker build --build-arg INSTALL_EXTRAS=api,gcp -t seeql:dev-gcp .
#
# Multi-arch build:
#   docker buildx build --platform=linux/amd64,linux/arm64 \
#     --build-arg SEEQL_VERSION=$(git describe --tags --always) \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
#     -t ghcr.io/shubhankar-mohan/seeql:latest --push .
# =============================================================================

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------- Stage 1: build wheels ----------
FROM ${PYTHON_IMAGE} AS builder

ARG INSTALL_EXTRAS=api

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Only the files pip needs to resolve and build the dependency graph
COPY pyproject.toml README.md ./
COPY main.py ./
COPY config/ ./config/
COPY collectors/ ./collectors/
COPY storage/ ./storage/
COPY parsers/ ./parsers/
COPY scheduler/ ./scheduler/
COPY api/ ./api/
COPY agent/ ./agent/
COPY alerting/ ./alerting/
COPY seeql/ ./seeql/

# google-genai is built unconditionally so the default (api-only) runtime
# image can import the Gemini/Vertex backend without the [gcp] extra.
RUN pip wheel --wheel-dir /wheels ".[${INSTALL_EXTRAS}]" google-genai

# ---------- Stage 2: runtime ----------
FROM ${PYTHON_IMAGE} AS runtime

ARG SEEQL_VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE
ARG INSTALL_EXTRAS=api

LABEL org.opencontainers.image.title="SeeQL" \
      org.opencontainers.image.description="LLM-powered MySQL DBA agent with anomaly detection and incident replay" \
      org.opencontainers.image.version="${SEEQL_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/shubhankar-mohan/SeeQL" \
      org.opencontainers.image.url="https://github.com/shubhankar-mohan/SeeQL" \
      org.opencontainers.image.documentation="https://github.com/shubhankar-mohan/SeeQL#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.authors="SeeQL contributors"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SEEQL_API_PORT=8080 \
    SEEQL_ENV=production

WORKDIR /app

# Install from the pre-built wheels — no network access needed
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels "seeql[${INSTALL_EXTRAS}]" google-genai \
    && rm -rf /wheels \
    && find /usr/local/lib/python3.12 -type d -name '__pycache__' -prune -exec rm -rf {} +

# Runtime code. Tests, scripts, and internal docs intentionally not shipped.
COPY main.py ./
COPY config/ ./config/
COPY collectors/ ./collectors/
COPY storage/ ./storage/
COPY parsers/ ./parsers/
COPY scheduler/ ./scheduler/
COPY api/ ./api/
COPY agent/ ./agent/
COPY alerting/ ./alerting/
COPY seeql/ ./seeql/
COPY templates/ ./templates/
COPY static/ ./static/

# Reference config (users mount their own at /etc/seeql/seeql.yml)
COPY seeql.example.yml /etc/seeql/seeql.example.yml
ENV SEEQL_CONFIG=/etc/seeql/seeql.yml

# Non-root user, writable data + logs
RUN useradd --create-home --shell /usr/sbin/nologin seeql \
    && mkdir -p /app/data /app/logs \
    && chown -R seeql:seeql /app

USER seeql

VOLUME ["/app/data", "/app/logs"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5); sys.exit(0)" || exit 1

# Default: scheduler + API + dashboard.
# Override with `seeql run` for collector-only, `seeql check` for a probe.
CMD ["seeql", "serve"]
