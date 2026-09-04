# Atlas PMOS runtime image.
#
# Multi-stage: dependencies are built with a compiler present, then copied into
# a slim runtime that has none. The final image runs as an unprivileged user on
# a read-only-friendly layout, because a container that can rewrite its own code
# turns a file-write bug into remote code execution.
#
# SPDX-License-Identifier: MIT

# --------------------------------------------------------------------- build
FROM python:3.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-only toolchain. None of this reaches the runtime image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first, so a source-only change does not invalidate the
# dependency layer.
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py
RUN pip install --upgrade pip setuptools wheel \
    && pip install ".[postgres]"

COPY . .
RUN pip install --no-deps .

# ------------------------------------------------------------------- runtime
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    ATLAS_ENV=production

RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 atlas \
    && useradd --system --uid 1001 --gid atlas --home /app --shell /usr/sbin/nologin atlas

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=atlas:atlas . .

# The only writable paths the application needs. Everything else can be mounted
# read-only.
RUN mkdir -p /app/instance /app/instance/storage \
    && chown -R atlas:atlas /app/instance

USER atlas
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/healthz || exit 1

# tini reaps zombies and forwards signals, so Gunicorn actually receives
# SIGTERM and drains instead of being killed after the grace period.
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "200", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--forwarded-allow-ips", "*"]
