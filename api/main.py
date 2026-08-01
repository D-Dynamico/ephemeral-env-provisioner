import structlog
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.environment_router import router as env_router

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is owned by Alembic (CLAUDE.md §7). The app never creates tables;
    # `alembic upgrade head` runs before the API starts (see docker-compose.yml).
    log.info("Starting up")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(env_router)

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}