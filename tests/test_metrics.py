"""
Metric definitions.

The label sets get their own tests because the constraint they encode is
invisible at the call site. `provision_total.labels(...)` looks equally correct
whichever labels it names, and a metric labelled by `env_id` or `owner` costs
one time series per environment forever and leaks a principal's identity into an
endpoint meant to be scraped by something else (CLAUDE.md §9). Nothing else in
the test suite would notice.
"""

import os
from unittest.mock import patch

import pytest
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.tasks as worker_tasks
from config import settings
from db.models import Base, Environment, EnvironmentStatus
from docker_manager.compose import StackResult
from metrics import (
    ALL_METRICS,
    FORBIDDEN_LABELS,
    MULTIPROC_ENV_VAR,
    OUTCOMES,
    UNKNOWN_TEMPLATE,
    Outcome,
    mark_worker_process_dead,
    prepare_multiproc_dir,
    provision_duration_seconds,
    provision_total,
    render_latest,
    teardown_total,
)


# ── Label sets ────────────────────────────────────────────────────────────────

def test_label_names_are_exactly_as_specified():
    assert provision_total._labelnames == ("template", "outcome")
    assert teardown_total._labelnames == ("outcome",)
    assert provision_duration_seconds._labelnames == ("template",)


def test_no_metric_carries_an_unbounded_label():
    """
    The rule this suite exists for. A new metric labelled by env_id or owner
    fails here rather than in production six months later.
    """
    for metric in ALL_METRICS:
        offending = FORBIDDEN_LABELS.intersection(metric._labelnames)
        assert not offending, f"{metric._name} is labelled by {sorted(offending)}"


def test_outcome_is_a_closed_set_of_three():
    assert OUTCOMES == {"success", "failure", "retry"}
    # retry must stay distinct from failure: both tasks retry, and folding them
    # together reports a recovered transient error as a lost environment.
    assert Outcome.RETRY != Outcome.FAILURE


# ── Exposed names ─────────────────────────────────────────────────────────────

def test_counter_names_expose_without_a_doubled_suffix():
    """
    prometheus_client strips a `_total` suffix from a Counter's name and adds it
    back when rendering. Constructing `Counter("provision")` would therefore
    also expose `provision_total`, and constructing `Counter("provision_total")`
    does not expose `provision_total_total`. Pinned because the naming in
    CLAUDE.md §11.2 is exact.
    """
    body = render_latest()[0].decode()
    assert "# TYPE provision_total counter" in body
    assert "# TYPE teardown_total counter" in body
    assert "provision_total_total" not in body


def test_render_latest_uses_the_prometheus_content_type():
    _, content_type = render_latest()
    assert content_type == CONTENT_TYPE_LATEST
    assert content_type.startswith("text/plain")


# ── Buckets ───────────────────────────────────────────────────────────────────

def test_duration_buckets_span_the_container_ready_timeout():
    """
    A provision cannot take longer than the readiness timeout without failing,
    so buckets must reach it — and past it, or the slowest real successes are
    indistinguishable from each other in the overflow bucket.
    """
    bounds = provision_duration_seconds._upper_bounds
    finite = [b for b in bounds if b != float("inf")]

    assert max(finite) > settings.container_ready_timeout_seconds
    # The default buckets stop at 10s, which is roughly one observed provision.
    assert max(finite) > 10
    assert bounds[-1] == float("inf")


# ── Increments ────────────────────────────────────────────────────────────────
# The metrics are process-global and accumulate across the whole session, so
# every assertion below is a delta. Absolute values would couple these tests to
# their execution order.

TEMPLATE = "webapp-postgres"


def _count(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _duration_count(template: str) -> float:
    return _count("provision_duration_seconds_count", template=template)


@pytest.fixture
def sync_sessions():
    """Sync SQLite, as the worker uses sync SQLAlchemy (mirrors test_environments)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _make_env(sessions, name: str) -> str:
    session = sessions()
    env = Environment(
        name=name,
        owner="dev@example.com",
        template=TEMPLATE,
        status=EnvironmentStatus.PENDING,
        ttl_seconds=300,
    )
    session.add(env)
    session.commit()
    env_id = str(env.id)
    session.close()
    return env_id


def _stack() -> StackResult:
    return StackResult(network_id="net123", container_ids=["c1", "c2"], host_port=32768)


def _run_task(task, env_id: str, sessions, method: str, impl, retries: int = 0):
    """
    Run a task body at a chosen retry count, with one DockerManager method stubbed.

    `push_request` is what makes the attempt look like the Nth: calling the task
    directly would push its own request and reset retries to 0, which is the
    difference between the `retry` and `failure` outcomes.
    """
    task.push_request(retries=retries)
    try:
        with patch.object(worker_tasks, "get_sync_db", lambda: sessions()), \
             patch.object(worker_tasks.docker_manager, method, side_effect=impl):
            return task.run(env_id)
    finally:
        task.pop_request()


def test_provision_success_counts_and_times(sync_sessions):
    env_id = _make_env(sync_sessions, "ok-env")
    before = _count("provision_total", template=TEMPLATE, outcome=Outcome.SUCCESS)
    before_timed = _duration_count(TEMPLATE)

    _run_task(
        worker_tasks.provision_environment, env_id, sync_sessions,
        "provision", lambda **kw: _stack(),
    )

    assert _count(
        "provision_total", template=TEMPLATE, outcome=Outcome.SUCCESS
    ) == before + 1
    assert _duration_count(TEMPLATE) == before_timed + 1


def test_provision_failure_with_attempts_left_counts_as_retry(sync_sessions):
    env_id = _make_env(sync_sessions, "flaky-env")
    before = _count("provision_total", template=TEMPLATE, outcome=Outcome.RETRY)
    before_failed = _count("provision_total", template=TEMPLATE, outcome=Outcome.FAILURE)

    def boom(**_kw):
        raise RuntimeError("docker daemon unreachable")

    # A task invoked directly rather than dispatched by a worker has
    # `called_directly` set, and Celery's retry() re-raises the original
    # exception instead of Retry. The classification under test happens before
    # that call, so what surfaces here does not affect what was counted.
    with pytest.raises(RuntimeError):
        _run_task(
            worker_tasks.provision_environment, env_id, sync_sessions,
            "provision", boom, retries=0,
        )

    assert _count(
        "provision_total", template=TEMPLATE, outcome=Outcome.RETRY
    ) == before + 1
    # A transient error that will be tried again must not read as a lost
    # environment — this is the whole reason `retry` is a separate outcome.
    assert _count(
        "provision_total", template=TEMPLATE, outcome=Outcome.FAILURE
    ) == before_failed


def test_provision_failure_when_exhausted_counts_as_failure(sync_sessions):
    env_id = _make_env(sync_sessions, "doomed-env")
    before = _count("provision_total", template=TEMPLATE, outcome=Outcome.FAILURE)
    before_timed = _duration_count(TEMPLATE)

    def boom(**_kw):
        raise RuntimeError("docker daemon unreachable")

    task = worker_tasks.provision_environment
    with pytest.raises(Exception):
        _run_task(task, env_id, sync_sessions, "provision", boom,
                  retries=task.max_retries)

    assert _count(
        "provision_total", template=TEMPLATE, outcome=Outcome.FAILURE
    ) == before + 1
    # A failed provision must never land in the duration histogram: its
    # distribution is unrelated, and mixing them makes the percentiles describe
    # neither population.
    assert _duration_count(TEMPLATE) == before_timed


def test_provision_failure_before_the_row_is_read_uses_the_sentinel(sync_sessions):
    """
    A missing row means the template is genuinely unknown. The series still has
    to be labelled — an empty label value is a third meaning nobody reads.
    """
    missing = "3f1a0c22-0000-4000-8000-000000000000"
    before = _count(
        "provision_total", template=UNKNOWN_TEMPLATE, outcome=Outcome.FAILURE
    )

    with pytest.raises(ValueError):
        _run_task(
            worker_tasks.provision_environment, missing, sync_sessions,
            "provision", lambda **kw: _stack(),
        )

    assert _count(
        "provision_total", template=UNKNOWN_TEMPLATE, outcome=Outcome.FAILURE
    ) == before + 1


def test_teardown_success_counts(sync_sessions):
    env_id = _make_env(sync_sessions, "bye-env")
    before = _count("teardown_total", outcome=Outcome.SUCCESS)

    _run_task(
        worker_tasks.teardown_environment, env_id, sync_sessions,
        "teardown", lambda **kw: None,
    )

    assert _count("teardown_total", outcome=Outcome.SUCCESS) == before + 1


def test_teardown_exhausted_counts_as_failure(sync_sessions):
    env_id = _make_env(sync_sessions, "stuck-env")
    before = _count("teardown_total", outcome=Outcome.FAILURE)

    def boom(**_kw):
        raise RuntimeError("docker daemon unreachable")

    task = worker_tasks.teardown_environment
    with pytest.raises(RuntimeError):
        _run_task(task, env_id, sync_sessions, "teardown", boom,
                  retries=task.max_retries)

    assert _count("teardown_total", outcome=Outcome.FAILURE) == before + 1


def test_running_a_task_twice_counts_twice(sync_sessions):
    """
    Deliberately *not* the idempotency assertion its neighbours in
    test_environments.py make.

    Provisioning twice must be a no-op against Docker and the database
    (invariant 2), but the counter has to move both times. `task_acks_late` makes
    redelivery normal, and a counter that skipped the second attempt would stop
    measuring load — which is the only thing it is for. The two properties are
    not in conflict; they are about different subjects.
    """
    env_id = _make_env(sync_sessions, "twice-env")
    before = _count("provision_total", template=TEMPLATE, outcome=Outcome.SUCCESS)

    for _ in range(2):
        _run_task(
            worker_tasks.provision_environment, env_id, sync_sessions,
            "provision", lambda **kw: _stack(),
        )

    assert _count(
        "provision_total", template=TEMPLATE, outcome=Outcome.SUCCESS
    ) == before + 2


# ── Multiprocess mode ─────────────────────────────────────────────────────────
# Only the worker runs in multiprocess mode. These cover the directory
# handling; that the parent's server actually sums two children's counts is not
# reachable from a single-process test suite and is verified against the real
# stack instead.

def test_multiproc_dir_is_skipped_when_not_configured(monkeypatch):
    """
    The normal case under pytest and for a bare local `celery` run. Returning
    None rather than inventing a directory is what keeps single-process mode
    working with no configuration.
    """
    monkeypatch.delenv(MULTIPROC_ENV_VAR, raising=False)
    assert prepare_multiproc_dir() is None
    # Must not raise either, or every child exit in single-process mode would.
    mark_worker_process_dead(1234)


def test_multiproc_dir_is_created_empty(monkeypatch, tmp_path):
    """
    Wiping is the point. The files outlive the worker, so a restart that
    collected them would serve the previous run's totals as current — and a
    counter that jumps reads as real traffic to anything computing a rate.
    """
    target = tmp_path / "multiproc"
    target.mkdir()
    stale = target / "counter_99.db"
    stale.write_bytes(b"leftover from the previous run")

    monkeypatch.setenv(MULTIPROC_ENV_VAR, str(target))
    returned = prepare_multiproc_dir()

    assert returned == str(target)
    assert os.path.isdir(target)
    assert not stale.exists()
    assert os.listdir(target) == []


def test_multiproc_dir_is_created_when_absent(monkeypatch, tmp_path):
    target = tmp_path / "does-not-exist-yet"
    monkeypatch.setenv(MULTIPROC_ENV_VAR, str(target))

    assert prepare_multiproc_dir() == str(target)
    assert os.path.isdir(target)


# ── /metrics on the API ───────────────────────────────────────────────────────

async def test_metrics_requires_a_key(anon_client):
    """
    Consistent with every other route that is not /health. The endpoint is not
    especially sensitive, but §9's posture is that this service does not grow
    an unauthenticated surface by default.
    """
    response = await anon_client.get("/metrics")
    assert response.status_code == 401


async def test_metrics_returns_the_exposition_format(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE provision_total counter" in response.text


async def test_metrics_never_exposes_an_env_id_or_owner(client):
    """
    Belt and braces over the label-name test: this reads the rendered output,
    so a metric added later that interpolates an id into a *value* is caught
    too. The principal's own address is the one identifier certain to be in
    scope during this test.
    """
    response = await client.get("/metrics")

    assert "env_id" not in response.text
    assert "dev@example.com" not in response.text
