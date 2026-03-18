ARG PLANE_VERSION=v0.27.1

# Source images
FROM node:22-alpine AS node
FROM artifacts.plane.so/makeplane/plane-frontend:${PLANE_VERSION} AS web-img
FROM artifacts.plane.so/makeplane/plane-backend:${PLANE_VERSION} AS backend-img
FROM artifacts.plane.so/makeplane/plane-space:${PLANE_VERSION} AS space-img
FROM artifacts.plane.so/makeplane/plane-admin:${PLANE_VERSION} AS admin-img
FROM artifacts.plane.so/makeplane/plane-live:${PLANE_VERSION} AS live-img

# Final image
FROM python:3.12.10-alpine AS runner

WORKDIR /app

# System dependencies
RUN apk add --no-cache \
    libpq libxslt xmlsec bash curl uuidgen ncdu nss-tools \
    # PostgreSQL
    postgresql16 postgresql16-contrib \
    # Redis
    redis \
    # RabbitMQ + Erlang
    rabbitmq-server erlang \
    # Caddy (from Alpine repos)
    caddy \
    # MinIO download
    ca-certificates wget

# Node.js runtime
COPY --from=node /usr/lib /usr/lib
COPY --from=node /usr/local/lib /usr/local/lib
COPY --from=node /usr/local/include /usr/local/include
COPY --from=node /usr/local/bin /usr/local/bin

# Plane web frontend (Next.js standalone server)
COPY --from=web-img /app/web /app/web
COPY --from=web-img /app/node_modules /app/web/node_modules
COPY --from=web-img /app/next.config.js /app/web/

# Plane space app (Next.js standalone server)
COPY --from=space-img /app/space /app/space/space
COPY --from=space-img /app/node_modules /app/space/node_modules
COPY --from=space-img /app/packages /app/space/packages

# Plane admin panel (Next.js standalone server)
COPY --from=admin-img /app/admin /app/admin
COPY --from=admin-img /app/node_modules /app/admin/node_modules
COPY --from=admin-img /app/next.config.js /app/admin/

# Plane live service (needs full monorepo structure for @plane/* packages)
COPY --from=live-img /app /app/live

# Plane backend (Django) + Python deps
COPY --from=backend-img /code /app/backend
COPY --from=backend-img /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=backend-img /usr/local/bin /usr/local/bin

# Install supervisor + OpenHost auth proxy deps
RUN pip install --no-cache-dir supervisor flask PyJWT requests psycopg2-binary cryptography

# Install MinIO server binary
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then MINIO_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then MINIO_ARCH="arm64"; \
    else MINIO_ARCH="amd64"; fi && \
    wget -q "https://dl.min.io/aistor/minio/release/linux-${MINIO_ARCH}/minio" -O /usr/local/bin/minio && \
    chmod +x /usr/local/bin/minio

# Config files
COPY Caddyfile /app/proxy/Caddyfile
COPY supervisor.conf /app/supervisor.conf
COPY start.sh /app/start.sh
COPY plane.env /app/plane.env
COPY openhost_auth.py /app/openhost_auth.py

# Directories and permissions
RUN mkdir -p /app/logs/access /app/logs/error && \
    chmod +x /app/start.sh && \
    mkdir -p /run/postgresql && \
    chown -R postgres:postgres /run/postgresql && \
    chown -R rabbitmq:rabbitmq /app/logs/access /app/logs/error

EXPOSE 8080

CMD ["/app/start.sh"]
