from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import API_KEY_HEADER, api_key_map
from api.routes.environment_router import router as env_router
from api.routes.metrics_router import router as metrics_router
from config import settings
from observability import get_logger

# Configuration lives in observability.py so the API, the worker and beat all
# render the same shape. It used to be configured here, which meant only this
# process had it.
log = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is owned by Alembic (CLAUDE.md §7). The app never creates tables;
    # `alembic upgrade head` runs before the API starts (see docker-compose.yml).

    # Fail closed. This process can start containers on a root-equivalent Docker
    # socket, so booting without auth is worse than not booting: it looks like a
    # working service. Raising here stops the container rather than serving.
    # Never log the keys themselves, only how many there are (§9).
    keys = api_key_map()  # raises ValueError on a malformed API_KEYS value
    if not keys:
        raise RuntimeError(
            "API_KEYS is empty. Set it to one or more 'key:principal' pairs "
            "before starting the API; see env.example."
        )
    log.info("Starting up", principals=len(set(keys.values())))
    yield
    log.info("Shutting down")

app = FastAPI(
    title="Ephemeral Environment Provisioner",
    description=(
        "Spin up isolated Docker stacks on demand. "
        "Each environment gets its own network, containers, and lifecycle."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Only mounted when origins are configured. There is no first-party web UI, so
# the default is to send no CORS headers at all rather than a permissive set.
# Credentials stay off: the credential is a header, not a cookie, so browsers
# never attach it automatically.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", API_KEY_HEADER],
    )

app.include_router(env_router)
app.include_router(metrics_router)

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}