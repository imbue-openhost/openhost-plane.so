Plane.so all-in-one container for OpenHost. bundles everything into a single Docker image:

- Plane community edition (web, api, worker, beat, space, live, admin)
- PostgreSQL 16
- Redis
- RabbitMQ
- MinIO (local S3-compatible storage)
- Caddy (internal reverse proxy)
- Supervisor (process manager)

## deploying

deploy via the router dashboard — point it at this directory. the app will be available at `/plane`.

## data

all persistent data lives in `/app/data/` (mapped to OpenHost's app_data directory):
- `postgres/` — PostgreSQL data
- `redis/` — Redis AOF
- `rabbitmq/` — RabbitMQ mnesia
- `minio/` — uploaded files

## resources

needs ~2GB RAM and 2 CPU cores. the container is large (~1.5GB+) due to bundling all services.

## configuration

`start.sh` auto-generates `plane.env` at runtime with local service URLs. no external services needed.

to set a custom domain, pass `DOMAIN_NAME=your.domain` as an environment variable.

## based on

plane community AIO image: https://github.com/makeplane/plane/tree/preview/deployments/aio/community
