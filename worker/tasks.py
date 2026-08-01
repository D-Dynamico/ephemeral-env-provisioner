"""
Celery worker — handles async provisioning and teardown tasks.

Run with:
    celery -A worker.tasks worker --loglevel=info
"""

import logging
from datetime import datetime, timezone, timedelta

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from db.models import Environment, EnvironmentStatus
from docker_manager.compose import docker_manager

log = logging.getLogger(__name__)

# ── Celery app ─────────────────────────────────────────────────────────────────

celery_app = Celery(
    "provisioner",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,          # Only ack after task completes (safer)
    worker_prefetch_multiplier=1, # One task at a time per worker process
)

# Periodic sweep for expired environments — requires `celery -A worker.tasks beat`.
celery_app.conf.beat_schedule = {
    "reap-expired": {
        "task": "worker.tasks.reap_expired_environments",
        "schedule": 60.0,
    },
}

# ── Sync DB session for Celery (sync SQLAlchemy, not async) ───────────────────
# Celery tasks run in their own threads/processes, so we use sync SQLAlchemy.
sync_engine = create_engine(
    settings.database_url.replace("+asyncpg", ""),  # use psycopg2 sync driver
    pool_pre_ping=True,
)
SyncSession = sessionmaker(bind=sync_engine, expire_on_commit=False)


def get_sync_db() -> Session:
    return SyncSession()


# ── Tasks ──────────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def provision_environment(self, env_id: str) -> dict:
    """
    Provision a full Docker stack for the given environment ID.
    Updates environment status in the DB as it progresses.
    """
    db = get_sync_db()
    try:
        env: Environment = db.query(Environment).filter_by(id=env_id).first()
        if not env:
            raise ValueError(f"Environment {env_id} not found")

        # ── Transition: pending → provisioning ─────────────────────────────
        env.status = EnvironmentStatus.PROVISIONING
        db.commit()
        log.info("[%s] Starting provisioning (template=%s)", env_id, env.template)

        # ── Spin up Docker stack ────────────────────────────────────────────
        result = docker_manager.provision(
            env_id=str(env.id),
            template_name=env.template,
        )

        # ── Transition: provisioning → running ─────────────────────────────
        now = datetime.now(timezone.utc)
        env.status = EnvironmentStatus.RUNNING
        env.network_id = result.network_id
        env.container_ids = result.container_ids
        env.host_port = result.host_port
        env.started_at = now
        env.expires_at = now + timedelta(seconds=env.ttl_seconds)
        db.commit()

        log.info(
            "[%s] Running on host port %d, expires at %s",
            env_id, result.host_port, env.expires_at
        )
        return {
            "env_id": env_id,
            "host_port": result.host_port,
            "status": "running",
        }

    except Exception as exc:
        log.exception("[%s] Provisioning failed: %s", env_id, exc)
        # Mark as failed in DB
        try:
            env.status = EnvironmentStatus.FAILED
            env.error_message = str(exc)[:500]
            db.commit()
        except Exception:
            pass

        # Retry with exponential backoff unless it's a value error
        if not isinstance(exc, ValueError):
            raise self.retry(exc=exc, countdown=2 ** self.request.retries * 10)
        raise

    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=5)
def teardown_environment(self, env_id: str) -> dict:
    """
    Stop and remove all Docker resources for an environment.
    """
    db = get_sync_db()
    try:
        env: Environment = db.query(Environment).filter_by(id=env_id).first()
        if not env:
            raise ValueError(f"Environment {env_id} not found")

        # ── Transition: running → stopping ─────────────────────────────────
        env.status = EnvironmentStatus.STOPPING
        db.commit()
        log.info("[%s] Starting teardown", env_id)

        docker_manager.teardown(
            env_id=str(env.id),
            container_ids=env.container_ids or [],
            network_id=env.network_id,
        )

        # ── Transition: stopping → stopped ─────────────────────────────────
        env.status = EnvironmentStatus.STOPPED
        env.stopped_at = datetime.now(timezone.utc)
        db.commit()

        log.info("[%s] Teardown complete", env_id)
        return {"env_id": env_id, "status": "stopped"}

    except Exception as exc:
        log.exception("[%s] Teardown failed: %s", env_id, exc)
        raise self.retry(exc=exc)

    finally:
        db.close()


@celery_app.task
def reap_expired_environments() -> dict:
    """
    Periodic sweep — tear down environments whose TTL has elapsed.

    Each row is claimed by flipping it to STOPPING before the teardown task is
    enqueued, so a second beat tick cannot enqueue teardown for the same
    environment twice.
    """
    db = get_sync_db()
    try:
        now = datetime.now(timezone.utc)
        expired = db.query(Environment).filter(
            Environment.status == EnvironmentStatus.RUNNING,
            Environment.expires_at <= now,
        ).all()

        if not expired:
            return {"reaped": 0}

        for env in expired:
            env.status = EnvironmentStatus.STOPPING
        db.commit()

        for env in expired:
            teardown_environment.delay(str(env.id))
            log.info("[%s] TTL expired — teardown enqueued", env.id)

        return {"reaped": len(expired)}

    finally:
        db.close()