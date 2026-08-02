# Ephemeral Environment Provisioner

Provision isolated Docker stacks on demand via a REST API. Each environment gets
its own bridge network, its own containers, and a TTL after which it is torn down
automatically.

The interesting part is not the create path — it is what happens when things go
wrong. Workers die mid-provision, teardowns fail, Docker and the database drift
apart. This is a **container lifecycle manager with reconciliation**: Docker is
the source of truth for what exists, PostgreSQL for what should exist, and
background sweeps continuously close the gap.

## What it is not

The motivating use case is per-PR preview environments, but the following are
deliberately out of scope and not built:

- Building images from a PR — it starts pre-existing images only
- Ingress or routing — no `{id}.example.com`, you get a Docker-assigned host port
- Secret injection or per-environment config beyond the template
- Data seeding
- Multi-host scheduling — single Docker daemon, single node

## Stack

| Layer | Tech |
|---|---|
| API | FastAPI + uvicorn, async SQLAlchemy 2.0 (asyncpg) |
| Task queue | Celery + Redis, with beat for the periodic sweeps |
| DB | PostgreSQL, schema managed by Alembic |
| Containers | Docker SDK for Python |
| Testing | pytest + pytest-asyncio |

The worker uses **sync** SQLAlchemy while the API uses **async**. Celery tasks
cannot share the API's engine or its event loop, so these stay separate.

## Lifecycle

```
PENDING ──▶ PROVISIONING ──▶ RUNNING ──▶ STOPPING ──▶ STOPPED
                  │                          │
                  └──────────▶ FAILED ◀──────┘
```

A client never blocks on provisioning. `POST` returns `202` with a task id
immediately; poll `GET /environments/{id}` for progress.

### Reconciliation

Three sweeps run on Celery beat. They are idempotent and compose with each other.

| Sweep | Interval | What it does |
|---|---|---|
| `reap-expired` | 60s | `RUNNING` rows past `expires_at` → claim as `STOPPING`, enqueue teardown |
| `recover-stale` | 120s | `PENDING` / `PROVISIONING` / `STOPPING` past their timeout → `FAILED` |
| `reconcile-orphans` | 300s | Labelled Docker resources whose row is missing or terminal → removed |

Every resource carries a `provisioner.env_id` label. That is what makes the
third sweep possible: a provision that dies after creating the network but
before persisting its id leaves resources the database has no record of, and the
label is the only way to find them again.

The sweeps chain. A worker killed mid-provision strands its row in
`PROVISIONING`; `recover-stale` moves it to `FAILED`; `reconcile-orphans` then
sees a terminal row and reclaims its containers and network.

Resources younger than a grace period (default 900s) are never swept, so the
reconciler cannot delete a stack that is still being built.

## Quick start

```bash
# 1. Copy env config, then set API_KEYS to a real key — the API will not
#    start without it. See Authentication below.
cp env.example .env

# 2. Build the image environments will run. The provisioner starts
#    pre-existing images and never builds one, so this is a prerequisite.
docker build -t provisioner/demo-app:1 templates/demo-app

# 3. Start the stack — postgres, redis, api, worker, beat
docker compose up --build

# 4. API at        http://localhost:8000
#    Swagger UI at http://localhost:8000/docs
```

The API container runs `alembic upgrade head` before uvicorn starts. The app
never creates its own tables.

> **Note:** if a PostgreSQL service is already running on the host, it will
> contend with Docker's published port 5432. Run `psql`/`alembic` inside the
> compose network rather than against `localhost`.

## Templates

Templates are a fixed in-process allowlist (`docker_manager/compose.py`). A
caller picks a template by name and can never supply an image, a command, or a
mount.

`webapp-postgres` is two containers on their own bridge network:

| Role | Image | Memory | CPU | Notes |
|---|---|---|---|---|
| `db` | `postgres:16-alpine` | 256m | 0.5 | Internal only, no host port. Healthcheck: `pg_isready` |
| `app` | `provisioner/demo-app:1` | 384m | 0.5 | Built from `templates/demo-app`. Mapped to a random host port |

The app writes to the database in **its own** environment: open it and each
reload records a visit, so the count starts at zero in every new environment.
That is the point of the template — it demonstrates network isolation and
per-environment state rather than asserting them.

Values in a template resolve per environment. `{db_host}` becomes the db
container's name, which is its DNS name on that environment's network, and
`{env_id}` becomes the id. A placeholder that matches no role in the template
raises at provision time.

Each role can declare a healthcheck. Provisioning waits for one role to report
healthy before starting the role that depends on it — Postgres accepts
connections several seconds after its container reports `running`, and an app
that connects on boot would otherwise race it.

Every container carries a CPU, memory and PID ceiling. A template role may tune
them; it cannot opt out, so an unbounded container is not reachable through the
template allowlist. The defaults live in `config.py`
(`DEFAULT_CONTAINER_MEMORY_LIMIT`, `DEFAULT_CONTAINER_CPU_LIMIT`,
`DEFAULT_CONTAINER_PIDS_LIMIT`) and apply to any role that stays silent.

The limits bound the host, not just the environment: `webapp-postgres` costs
1.0 core and 640m per environment, so the five-environment quota caps one
principal at 5 cores and ~3.2GB. Swap is pinned to the memory limit, so the
ceiling cannot be evaded by swapping. A container killed at its limit reports
that it exceeded the limit rather than that it crashed.

Changing the app means rebuilding and re-tagging its image. The provisioner
starts pre-existing images and does not build (see *What it is not*).

## Authentication

Every `/environments` route requires an `X-API-Key` header. `/health` and the
docs are open.

Keys are configured as `key:principal` pairs in `API_KEYS`:

```bash
# Generate a key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# .env
API_KEYS=<generated>:dev@example.com,<another>:ci
```

**The API refuses to start when `API_KEYS` is empty.** A process that can start
containers on a root-equivalent socket has no safe default-open mode.

The principal is the identity that owns whatever that key creates. Minimum key
length is 16 characters; duplicate keys are rejected at startup.

What this is not: there is no key rotation, expiry, hashing at rest, per-key
scope or rate limiting. Keys sit in the process environment in plaintext. This
closes the unauthenticated-RCE hole; it is not user management.

CORS sends no headers unless `CORS_ALLOW_ORIGINS` lists origins explicitly.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/environments/` | `202` + task id. Max active per principal (`429`), unique active name per principal (`409`) |
| `GET` | `/environments/` | The caller's environments. Filter by `status`; `limit` / `offset` |
| `GET` | `/environments/{id}` | Poll for status. `404` for another principal's environment |
| `DELETE` | `/environments/{id}` | `202`, triggers teardown |
| `GET` | `/health` | Open, no key |

```bash
# Create
curl -X POST http://localhost:8000/environments/ \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "pr-42", "template": "webapp-postgres", "ttl_seconds": 3600}'

# Poll
curl -H "X-API-Key: $API_KEY" http://localhost:8000/environments/<id>

# Tear down early
curl -X DELETE -H "X-API-Key: $API_KEY" http://localhost:8000/environments/<id>
```

Environment names must match `^[a-z0-9\-]+$`. TTL is clamped to 5 minutes – 24 hours.
The owner is never sent by the caller: it is the principal behind the key, so
the per-owner quota (`MAX_ENVIRONMENTS_PER_USER`, default 5) and the unique-name
guard cannot be sidestepped by naming someone else.

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

The suite runs against in-memory SQLite with Celery mocked, so no Docker daemon
is required. That is a convenience, not a claim of coverage — it exercises the
API contract, the guards, the state machine and the sweeps, but **not**
`DockerManager`, which is the only component that can fail in a way that costs
real resources.

## Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "<slug>"
alembic downgrade -1
```

Alembic owns the schema. Run these inside the compose network
(`docker compose run --rm api alembic ...`) so the `postgres` hostname resolves.

## Project structure

```
.
├── api/
│   ├── main.py                       # FastAPI app + lifespan
│   ├── auth.py                       # API-key → principal, fail-closed
│   ├── routes/
│   │   └── environment_router.py     # Environment endpoints
│   └── schemas/
│       └── environment_schema.py     # Pydantic request/response models
├── worker/
│   └── tasks.py                      # Celery tasks: provision, teardown, 3 sweeps
├── docker_manager/
│   └── compose.py                    # Docker SDK wrapper, templates, label discovery
├── templates/
│   └── demo-app/                     # The app environments run. Built out of band,
│       ├── Dockerfile                # never built by the provisioner
│       └── app.py
├── db/
│   ├── models.py                     # SQLAlchemy ORM models + set_status
│   └── session.py                    # Async session factory
├── alembic/
│   └── versions/                     # Migrations
├── tests/
│   ├── conftest.py                   # SQLite fixtures + authenticated clients
│   ├── test_auth.py
│   ├── test_templates.py
│   └── test_environments.py
├── config.py                         # Pydantic settings
├── docker-compose.yml                # postgres, redis, api, worker, beat
└── Dockerfile
```

## Status

There are no container CPU or memory limits yet. The quota bounds how many
environments a principal can run, not what each one consumes.

The service requires access to the Docker socket, which is root-equivalent on
the host. Authentication is a static API-key allowlist (see below) — enough that
the provisioning endpoints are not open to the internet, and not a substitute
for running this behind a network boundary you control.
