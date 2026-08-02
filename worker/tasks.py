"""
Celery worker — handles async provisioning and teardown tasks.

Run with:
    celery -A worker.tasks worker --loglevel=info
"""

import time
import uuid
from datetime import datetime, timezone, timedelta

from celery import Celery
from celery.signals import setup_logging, worker_init, worker_process_shutdown
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from db.models import Environment, EnvironmentStatus
from docker_manager.compose import docker_manager
from metrics import (
    UNKNOWN_TEMPLATE,
    Outcome,
    mark_worker_process_dead,
    prepare_multiproc_dir,
    provision_duration_seconds,
    provision_total,
    start_worker_metrics_server,
    teardown_total,
)
from observability import bind_env_id, configure_logging, get_logger

log = get_logger(__name__)


@setup_logging.connect
def _use_our_logging(**_kwargs) -> None:
    """
    Celery replaces the root logger's handlers on startup unless something
    answers this signal. Answering it — even with our own configuration — is
    what stops it, so the worker's own lines render like everything else.
    """
    configure_logging()


@worker_init.connect
def _serve_metrics(**_kwargs) -> None:
    """
    Fires in the parent, before the prefork pool forks.

    The order matters. The directory is wiped first, so the pool cannot start
    writing into files left by the previous run — a counter that resurrects its
    old total reads as a burst of real traffic to anything computing a rate.
    The server then serves the aggregate of whatever the children go on to
    write, which is the only reason it runs in the parent: the parent itself
    executes no tasks and has nothing of its own to report.

    Beat imports this module too and never receives this signal, so it does not
    try to bind the same port.
    """
    prepare_multiproc_dir()
    start_worker_metrics_server(settings.worker_metrics_port)


@worker_process_shutdown.connect
def _release_metrics_files(pid=None, **_kwargs) -> None:
    """Drop a finished child's gauge files. Its counters stay (see metrics.py)."""
    mark_worker_process_dead(pid)


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
        log.exception("environment.mark_failed_error")


# ── Tasks ──────────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def provision_environment(self, env_id: str) -> dict:
    """
    Provision a full Docker stack for the given environment ID.
    Updates environment status in the DB as it progresses.
    """
    db = get_sync_db()
    # Metrics are counted per *attempt*, so the clock starts here rather than at
    # the row's created_at: this measures the provision, not the time the task
    # spent queued behind other work. Queue wait is a real number worth having
    # and it is a different metric — merged into one histogram, the result moves
    # for two unrelated reasons and can be read for neither.
    started = time.monotonic()
    # Stands in until the row is read, so a failure before that still counts
    # against a closed label set instead of an empty one.
    template = UNKNOWN_TEMPLATE

    # Everything below — including lines from docker_manager — is tagged with
    # this env_id, so one provision is a filter rather than a grep.
    with bind_env_id(env_id):
        try:
            env: Environment = _load_env(db, env_id)
            if not env:
                raise ValueError(f"Environment {env_id} not found")
            template = env.template

            # ── Transition: pending → provisioning ─────────────────────────────
            env.set_status(EnvironmentStatus.PROVISIONING)
            db.commit()
            log.info("provision.started", template=env.template)

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

            provision_total.labels(
                template=template, outcome=Outcome.SUCCESS
            ).inc()
            # Successes only. A provision that dies at 3s and one that succeeds
            # at 11s are different distributions, and mixing them makes the
            # percentiles describe neither. Failures are already counted above.
            provision_duration_seconds.labels(template=template).observe(
                time.monotonic() - started
            )

            log.info(
                "provision.running",
                template=env.template,
                host_port=result.host_port,
                container_count=len(result.container_ids),
                expires_at=env.expires_at.isoformat(),
            )
            return {
                "env_id": env_id,
                "host_port": result.host_port,
                "status": "running",
            }

        except Exception as exc:
            log.exception("provision.failed", error=str(exc))
            _mark_failed(db, env_id, str(exc))

            # A ValueError is not retried, and neither is an attempt that has
            # already used its budget. Both are the end of the road, so they
            # count as `failure`; anything still due another attempt counts as
            # `retry`. The check has to happen before self.retry(), which on the
            # last attempt re-raises rather than returning.
            retryable = not isinstance(exc, ValueError)
            exhausted = self.request.retries >= self.max_retries
            provision_total.labels(
                template=template,
                outcome=Outcome.RETRY if retryable and not exhausted
                else Outcome.FAILURE,
            ).inc()

            # Retry with exponential backoff unless it's a value error
            if retryable:
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
    with bind_env_id(env_id):
        try:
            env: Environment = _load_env(db, env_id)
            if not env:
                raise ValueError(f"Environment {env_id} not found")

            # ── Transition: running → stopping ─────────────────────────────────
            env.set_status(EnvironmentStatus.STOPPING)
            db.commit()
            log.info("teardown.started")

            docker_manager.teardown(
                env_id=str(env.id),
                container_ids=env.container_ids or [],
                network_id=env.network_id,
            )

            # ── Transition: stopping → stopped ─────────────────────────────────
            env.set_status(EnvironmentStatus.STOPPED)
            env.stopped_at = datetime.now(timezone.utc)
            db.commit()

            teardown_total.labels(outcome=Outcome.SUCCESS).inc()
            log.info("teardown.complete")
            return {"env_id": env_id, "status": "stopped"}

        except Exception as exc:
            log.exception(
                "teardown.failed", error=str(exc), attempt=self.request.retries
            )
            # self.retry() raises out of this block, so the exhaustion check has to
            # happen before the call — it cannot be caught by the same try.
            exhausted = self.request.retries >= self.max_retries
            teardown_total.labels(
                outcome=Outcome.FAILURE if exhausted else Outcome.RETRY
            ).inc()

            if exhausted:
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
            with bind_env_id(env.id):
                log.info("reap.enqueued_teardown", expires_at=env.expires_at.isoformat())

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
                log.warning("orphan.unparseable_label", label=repr(env_id))
                skipped += 1
                continue

            if env is not None and env.status not in TERMINAL_STATUSES:
                skipped += 1
                continue

            with bind_env_id(env_id):
                log.info(
                    "orphan.removing",
                    reason="no_db_row" if env is None else env.status.value,
                    container_count=len(res.container_ids),
                    network_count=len(res.network_ids),
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
                with bind_env_id(env.id):
                    log.warning(
                        "stale.presumed_dead",
                        stuck_in=status.value,
                        timeout_seconds=timeout,
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