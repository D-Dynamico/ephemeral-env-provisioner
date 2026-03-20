import structlog
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.environment_router import router as env_router
from db.session import init_db

# ── Structured logging setup ───────────────────────────────────────────────────
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up — initialising database")
    await init_db()
    yield
    log.info("Shutting down")


# ── App ────────────────────────────────────────────────────────────────────────
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