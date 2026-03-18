Plane.so all-in-one container for OpenHost. Bundles everything into a single Docker image:

- Plane community edition v0.27.1 (web, api, worker, beat, space, live, admin)
- PostgreSQL 16
- Redis
- RabbitMQ
- MinIO (local S3-compatible storage)
- Caddy (internal reverse proxy)
- Supervisor (process manager)
- OpenHost federated identity auth proxy

## How it works

### Auto-setup

On first boot, the container:
1. Initializes PostgreSQL and runs Plane's database migrations
2. Waits for the API to be ready
3. Creates an admin user (`owner@openhost.app`) via Plane's sign-up API
4. Marks the instance as set up

The owner is automatically logged in when they visit the app — no manual setup required.

### Authentication

The OpenHost router sets an `X-OpenHost-Is-Owner: true` header for authenticated zone owners. The auth proxy (`openhost_auth.py`) uses this to auto-create Django sessions for the owner.

Guest users authenticate via OpenHost's federated identity flow:
1. Owner adds guests by domain at `/openhost-auth/manage`
2. Guests visit the app and are redirected to `/openhost-auth/login`
3. They authenticate via their home zone's identity approval flow
4. On callback, a Plane session is created and they're added to the workspace

### Session management

Sessions are stored in Plane's custom `sessions` table using Django's `signing.dumps` with the salt `django.contrib.sessions.SessionStore`. The `SECRET_KEY` is persisted in the data directory so sessions survive container restarts.

The `check-session` forward-auth endpoint (called by Caddy on every page load):
- Validates existing sessions by decoding the signature (catches stale sessions from old SECRET_KEYs)
- Auto-creates sessions for the zone owner
- Redirects unauthenticated browser visitors to the OpenHost identity login

## Deploying

Deploy via the OpenHost router dashboard — point it at this repo. The app will be available at `{app_name}.{zone_domain}` via subdomain routing.

The app name can be anything (e.g. `plane`, `plane2`). The domain is derived automatically from `OPENHOST_APP_NAME` and `OPENHOST_ZONE_DOMAIN`.

## Data

All persistent data lives in `$OPENHOST_APP_DATA_DIR` (falls back to `/app/data/`):
- `postgres/` — PostgreSQL data
- `redis/` — Redis AOF
- `rabbitmq/` — RabbitMQ mnesia
- `minio/` — uploaded files
- `.secret_keys` — persisted SECRET_KEY and LIVE_SERVER_SECRET_KEY
- `openhost_guests.json` — registered guest identities

## Files

- `Dockerfile` — multi-stage build pulling from Plane's official images
- `supervisor.conf` — process manager config for all services
- `start.sh` — initializes PostgreSQL, generates `plane.env`, starts supervisor
- `Caddyfile` — internal reverse proxy routing
- `openhost_auth.py` — OpenHost federated auth proxy (auto-setup, owner auto-login, guest identity login)
- `openhost.toml` — OpenHost app manifest
- `plane.env` — placeholder, overwritten at runtime by `start.sh`

## Resources

Needs ~2GB RAM and 2 CPU cores. The container image is ~1.5GB+ due to bundling all services.

## Based on

Plane community AIO: https://github.com/makeplane/plane/tree/preview/deployments/aio/community
