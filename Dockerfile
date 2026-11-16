# ==============================================================================
# Production Multi-Stage Dockerfile for Bulk Mail Management System (BMS)
#
# Stage 1: Build the frontend single-page application
# Stage 2: Hardened, non-root Python 3.12 runtime serving both API and static UI
# ==============================================================================

# Stage 1: Frontend Build
FROM node:20-alpine AS frontend-builder

WORKDIR /build/frontend

# Install dependencies with lockfile immutability
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline --no-audit

# Build static assets
COPY frontend/ ./
RUN npm run build

# Stage 2: Production Python Runtime
FROM python:3.12-slim-bookworm AS production

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    DEBUG=false \
    PYTHONPATH=/app/backend

# Install system dependencies needed for runtime health checks and postgres driver
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create unprivileged system user and group
RUN groupadd -g 10001 bms && \
    useradd -u 10001 -g bms -d /app -s /usr/sbin/nologin bms

WORKDIR /app

# Install Python production dependencies
COPY backend/pyproject.toml /app/backend/
RUN pip install --no-cache-dir /app/backend

# Copy application code, database migrations, and scripts
COPY backend/app/ /app/backend/app/
COPY backend/migrations/ /app/backend/migrations/
COPY backend/alembic.ini /app/backend/alembic.ini
COPY backend/scripts/ /app/backend/scripts/

# Copy compiled frontend assets from builder stage
COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist

# Secure ownership
RUN chown -R bms:bms /app

# Switch to non-root user
USER bms:bms

EXPOSE 8000

# Health check using internal loopback probe
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

# Production entrypoint
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
