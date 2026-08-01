"""
The application provisioned into every `webapp-postgres` environment.

Deliberately small, and deliberately *stateful*: it writes to the Postgres
container in its own environment. That is the point — it proves the two
containers share an isolated network and that each environment has its own
database, which a static placeholder image cannot demonstrate.

This is not part of the provisioner. It is baked into an image the provisioner
starts; the provisioner never builds it (see CLAUDE.md §1).
"""

import os
import time
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DATABASE_URL = os.environ["DATABASE_URL"]
ENV_ID = os.environ.get("ENV_ID", "unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id    SERIAL PRIMARY KEY,
    at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def connect(retries: int = 30, delay: float = 1.0) -> psycopg.Connection:
    """
    Connect, retrying while the database refuses connections.

    The provisioner already waits for the db container's healthcheck before
    starting this one, so this loop should never spin. It stays because the
    healthcheck is the provisioner's promise, not this app's guarantee, and an
    app that crash-loops on a cold database is a bad demo of a good system.
    """
    last: Exception | None = None
    for _ in range(retries):
        try:
            return psycopg.connect(DATABASE_URL, autocommit=True)
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"Database unreachable after {retries} attempts") from last


@asynccontextmanager
async def lifespan(app: FastAPI):
    with connect() as conn:
        conn.execute(SCHEMA)
    yield


app = FastAPI(title="Provisioned demo app", lifespan=lifespan)


@app.get("/health")
def health():
    """Docker polls this. It checks the database too, so `healthy` means usable."""
    with connect(retries=1) as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "env_id": ENV_ID}


@app.get("/", response_class=HTMLResponse)
def index():
    """Record a visit, then show every visit this environment has seen."""
    with connect(retries=1) as conn:
        conn.execute("INSERT INTO visits DEFAULT VALUES")
        total = conn.execute("SELECT count(*) FROM visits").fetchone()[0]
        recent = conn.execute(
            "SELECT id, at FROM visits ORDER BY id DESC LIMIT 10"
        ).fetchall()

    rows = "\n".join(
        f"<tr><td>{vid}</td><td>{at:%Y-%m-%d %H:%M:%S} UTC</td></tr>"
        for vid, at in recent
    )
    return f"""
<!doctype html>
<title>Environment {ENV_ID}</title>
<style>
  body {{ font-family: ui-monospace, monospace; margin: 3rem auto; max-width: 40rem; }}
  td {{ padding: .2rem 1rem .2rem 0; }}
  .id {{ color: #666; font-size: .85rem; }}
</style>
<h1>Ephemeral environment</h1>
<p class="id">{ENV_ID}</p>
<p><strong>{total}</strong> visit(s) recorded in this environment's own database.</p>
<table>{rows}</table>
<p class="id">Reload to write another row. Every environment has its own
Postgres, so this count starts at zero in the next one.</p>
"""
