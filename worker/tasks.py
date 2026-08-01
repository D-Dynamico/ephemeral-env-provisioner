"""
Celery worker — handles async provisioning and teardown tasks.

Run with:
    celery -A worker.tasks worker --loglevel=info
"""

import logging
import uuid
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

# Periodic sweeps — require `celery -A worker.tasks beat`.
celery_app.conf.beat_schedule = {
    "reap-expired": {
        "task": "worker.tasks.reap_expired_environments",
        "schedule": float(settings.reap_interval_seconds),
    },
    "reconcile-orphans": {
        "task": "worker.tasks.reconcile_orphans",
        "schedule": float(settings.orphan_sweep_interval_seconds),
    },
    "recover-stale": {
        "task": "worker.tasks.recover_stale_transitions",
        "schedule": float(settings.stale_sweep_interval_seconds),
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


def _load_env(db: Session, env_id: str) -> Environment | None:
    """
    Tasks receive env_id as a string (Celery serialises to JSON). The column is
    Uuid(as_uuid=True), whose bind processor calls .hex — so a raw string works
    on PostgreSQL but raises on SQLite. Coerce once, here.
    """
    return db.query(Environment).filter_by(id=uuid.UUID(str(env_id))).first()


def _mark_failed(db: Session, env_id: str, message: str) -> None:
    """
    Best-effort terminal marker. Never raises, so it is safe to call from an
    except block where the session may already be in a broken transaction.
    """
    try:
        db.rollback()
        env = _load_env(db, env_id)
        if env:
            env.set_status(EnvironmentStatus.FAILED)
            env.error_message = message[:500]
            db.commit()
    except Exception:
        log.exception("[%s] Could not mark environment FAILED", env_id)


# ── Tasks ──────────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def provision_environment(self, env_id: str) -> dict:
    """
    Provision a full Docker stack for the given environment ID.
    Updates environment status in the DB as it progresses.
    """
    db = get_sync_db()
    try:
        env: Environment = _load_env(db, env_id)
        if not env:
            raise ValueError(f"Environment {env_id} not found")

        # ── Transition: pending → provisioning ─────────────────────────────
        env.set_status(EnvironmentStatus.PROVISIONING)
        db.commit()
        log.info("[%s] Starting provisioning (template=%s)", env_id, env.template)

        # ── Spin up Docker stack ────────────────────────────────────────────
        result = docker_manager.provision(
            env_id=str(env.id),
            template_name=env.template,
        )

        # ── Transition: provisioning → running ─────────────────────────────
        now = datetime.now(timezone.utc)
        env.set_status(EnvironmentStatus.RUNNING)
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
        _mark_failed(db, env_id, str(exc))

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
        env: Environment = _load_env(db, env_id)
        if not env:
            raise ValueError(f"Environment {env_id} not found")

        # ── Transition: running → stopping ─────────────────────────────────
        env.set_status(EnvironmentStatus.STOPPING)
        db.commit()
        log.info("[%s] Starting teardown", env_id)

        docker_manager.teardown(
            env_id=str(env.id),
            container_ids=env.container_ids or [],
            network_id=env.network_id,
        )

        # ── Transition: stopping → stopped ─────────────────────────────────
        env.set_status(EnvironmentStatus.STOPPED)
        env.stopped_at = datetime.now(timezone.utc)
        db.commit()

        log.info("[%s] Teardown complete", env_id)
        return {"env_id": env_id, "status": "stopped"}

    except Exception as exc:
        log.exception("[%s] Teardown failed: %s", env_id, exc)
        # self.retry() raises out of this block, so the exhaustion check has to
        # happen before the call — it cannot be caught by the same try.
        if self.request.retries >= self.max_retries:
            # Last attempt. Without this the row sits in STOPPING forever with
            # no task left to move it, and the reaper only scans RUNNING.
            _mark_failed(db, env_id, f"Teardown failed: {exc}")
            raise
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
            env.set_status(EnvironmentStatus.STOPPING)
        db.commit()

        for env in expired:
            teardown_environment.delay(str(env.id))
            log.info("[%s] TTL expired — teardown enqueued", env.id)

        return {"reaped": len(expired)}

    finally:
        db.close()


# Statuses where no Docker resources should exist any more. A row in any other
# status is either in flight or legitimately running, so its resources are left
# alone regardless of what the sweep finds.
TERMINAL_STATUSES = frozenset(
    {EnvironmentStatus.STOPPED, EnvironmentStatus.FAILED}
)


@celery_app.task
def reconcile_orphans() -> dict:
    """
    Reconcile actual Docker state against intended DB state.

    Docker is the source of truth for what exists, Postgres for what should
    exist (invariant 3). They drift: `provision_environment` persists
    `container_ids` / `network_id` only on success, so a provision that dies
    part-way leaves resources with no DB record and no other way to find them.

    Removes labelled resources whose environment row is missing or terminal.
    Anything younger than the grace period is skipped, so this can never race a
    worker that is still building a stack.
    """
    db = get_sync_db()
    try:
        found = docker_manager.find_labelled(
            min_age_seconds=settings.orphan_grace_seconds
        )
        removed, skipped = 0, 0

        for env_id, res in found.items():
            try:
                env = _load_env(db, env_id)
            except (ValueError, AttributeError):
                # Label is not a UUID — not ours to reason about. Leave it.
                log.warning("Skipping unparseable env_id label: %r", env_id)
                skipped += 1
                continue

            if env is not None and env.status not in TERMINAL_STATUSES:
                skipped += 1
                continue

            reason = "no DB row" if env is None else f"status={env.status.value}"
            log.info(
                "[%s] Orphaned resources (%s): %d container(s), %d network(s)",
                env_id, reason, len(res.container_ids), len(res.network_ids),
            )
            docker_manager.remove_resources(
                env_id=env_id,
                container_ids=res.container_ids,
                network_ids=res.network_ids,
            )
            removed += 1

        return {"removed": removed, "skipped": skipped}

    finally:
        db.close()


# Every transient state and how long a row may sit in it before it is presumed
# dead. A worker that raises moves its own row; a worker that is *killed* leaves
# nothing behind to do it, and only this sweep can free the row (invariant 5).
TRANSIENT_TIMEOUTS = {
    EnvironmentStatus.PENDING: settings.pending_timeout_seconds,
    EnvironmentStatus.PROVISIONING: settings.provisioning_timeout_seconds,
    EnvironmentStatus.STOPPING: settings.stopping_timeout_seconds,
}


@celery_app.task
def recover_stale_transitions() -> dict:
    """
    Free rows stranded in a transient state by a worker that died.

    All three transient states resolve to FAILED rather than being retried.
    FAILED is terminal, which hands any Docker resources the row still owns to
    `reconcile_orphans` — the two sweeps compose, and neither can loop.

    Rows with a NULL `status_changed_at` are never swept. The column was added
    after these rows existed, and treating unknown as infinitely stale would
    fail live environments.
    """
    db = get_sync_db()
    try:
        now = datetime.now(timezone.utc)
        recovered: dict[str, int] = {}

        for status, timeout in TRANSIENT_TIMEOUTS.items():
            cutoff = now - timedelta(seconds=timeout)
            stale = db.query(Environment).filter(
                Environment.status == status,
                Environment.status_changed_at.isnot(None),
                Environment.status_changed_at <= cutoff,
            ).all()

            for env in stale:
                log.warning(
                    "[%s] Stuck in %s for over %ds — presuming the worker died",
                    env.id, status.value, timeout,
                )
                env.set_status(EnvironmentStatus.FAILED)
                env.error_message = (
                    f"Presumed dead: stuck in {status.value} for over {timeout}s"
                )[:500]

            if stale:
                recovered[status.value] = len(stale)

        if recovered:
            db.commit()

        return {"recovered": recovered, "total": sum(recovered.values())}

    finally:
        db.close()