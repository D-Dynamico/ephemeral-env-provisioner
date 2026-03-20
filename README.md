# Ephemeral Environment Provisioner

Spin up isolated Docker stacks on demand via a REST API. Each environment gets its own Docker network, containers, and TTL-based lifecycle.

## Stack
| Layer | Tech |
|---|---|
| API | FastAPI + uvicorn |
| Task Queue | Celery + Redis |
| DB | PostgreSQL + SQLAlchemy 2.0 (async) |
| Containers | Docker SDK for Python |
| Testing | pytest + pytest-asyncio |

## Quick Start

```bash
# 1. Copy env config
cp .env.example .env

# 2. Start infrastructure + app
docker compose up --build

# 3. API is live at http://localhost:8000
# 4. Interactive docs at http://localhost:8000/docs
```

## Run Tests

```bash
pip install -r requirements.txt
pip install aiosqlite  # for in-memory SQLite test DB
pytest tests/ -v
```

## Example Usage

```bash
# Create an environment
curl -X POST http://localhost:8000/environments/ \
  -H "Content-Type: application/json" \
  -d '{"name": "pr-42", "owner": "dev@example.com", "template": "webapp-postgres"}'

# Poll status
curl http://localhost:8000/environments/<id>

# Tear it down
curl -X DELETE http://localhost:8000/environments/<id>
```

## Project Structure

```
provisioner/
├── api/
│   ├── main.py              # FastAPI app + lifespan
│   ├── routes/
│   │   └── environments.py  # All environment endpoints
│   └── schemas/
│       └── environment.py   # Pydantic request/response models
├── worker/
│   └── tasks.py             # Celery tasks: provision + teardown
├── docker_manager/
│   └── compose.py           # Docker SDK wrapper + templates
├── db/
│   ├── models.py            # SQLAlchemy ORM models
│   └── session.py           # Async session + init_db
├── tests/
│   └── test_environments.py
├── config.py                # Pydantic settings
├── docker-compose.yml       # Local dev stack
└── Dockerfile
```