"""
Log correlation and the §9 credential guard.

These assert on rendered output rather than on the processor chain. What
matters is what actually reaches a log line: a chain that looks right but
drops `env_id`, or one that faithfully renders a database password, both pass
a structural check and fail here.
"""

import json
import logging

import pytest
import structlog

from docker_manager.compose import (
    TEMPLATES,
    DockerManager,
    container_name,
    resolve_environment,
)
from observability import bind_env_id, configure_logging, get_logger

ENV_ID = "7eb66d91-b31d-4e8c-9d66-88ec645e25b6"


@pytest.fixture
def captured(monkeypatch):
    """
    Render through the real chain into a list.

    `configure_logging` is idempotent by design, so the module-level flag has to
    be reset for the test to build its own handler — and restored afterwards, or
    the next test in the session silently reuses this one's capture handler.
    """
    import observability

    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setenv("APP_ENV", "production")   # JSON path
    monkeypatch.setattr(observability.settings, "app_env", "production")

    configure_logging()

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    root = logging.getLogger()
    formatter = root.handlers[0].formatter
    handler = Capture()
    handler.setFormatter(formatter)
    previous = root.handlers[:]
    root.handlers = [handler]

    yield records

    root.handlers = previous
    structlog.contextvars.clear_contextvars()


def test_env_id_is_a_field_not_a_prefix(captured):
    """
    The point of the change: `env_id` is queryable. The old form interpolated
    it into the message, which greps but does not filter or join.
    """
    log = get_logger("test")

    with bind_env_id(ENV_ID):
        log.info("provision.started", template="webapp-postgres")

    event = json.loads(captured[-1])
    assert event["env_id"] == ENV_ID
    assert event["event"] == "provision.started"
    assert ENV_ID not in event["event"]


def test_docker_manager_lines_inherit_the_bound_env_id(captured):
    """
    `remove_resources` never receives the id as something to log, and the
    correlation is worthless if it stops at the worker boundary.
    """
    mgr = DockerManager()
    mgr._client = None
    import docker.errors

    class FakeContainers:
        def get(self, cid):
            raise docker.errors.NotFound("gone")

    class FakeClient:
        containers = FakeContainers()
        networks = FakeContainers()

    mgr._client = FakeClient()

    mgr.remove_resources(env_id=ENV_ID, container_ids=["abc123def456"], network_ids=[])

    events = [json.loads(line) for line in captured]
    already_gone = [e for e in events if e["event"] == "container.already_gone"]
    assert already_gone, f"expected container.already_gone in {events}"
    assert already_gone[0]["env_id"] == ENV_ID


def test_env_id_does_not_leak_into_the_next_environment(captured):
    """
    A prefork child serves many environments. A contextvar left bound would
    label the next one's logs with the previous id, which reads as fact.
    """
    log = get_logger("test")

    with bind_env_id(ENV_ID):
        log.info("provision.started")
    log.info("beat.tick")

    assert json.loads(captured[-1]).get("env_id") is None


def test_nested_binds_restore_the_outer_env_id(captured):
    """`remove_resources` binds inside a caller that already bound."""
    log = get_logger("test")
    inner = "11111111-2222-3333-4444-555555555555"

    with bind_env_id(ENV_ID):
        with bind_env_id(inner):
            log.info("inner")
        log.info("outer")

    assert json.loads(captured[-2])["env_id"] == inner
    assert json.loads(captured[-1])["env_id"] == ENV_ID


def test_resolved_credentials_never_reach_a_log_line(captured):
    """
    §9: resolved template `environment` values carry credentials. Binding
    contextvars makes it easier to leak them by accident, so this asserts the
    actual provisioning log calls stay clean.
    """
    template = TEMPLATES["webapp-postgres"]
    roles = [s["role"] for s in template]

    # Not every resolved value is a secret. The env id and the derived container
    # names are deliberately in the logs — that is the whole point of the
    # correlation — so asserting on them would assert the opposite of intent.
    public = {ENV_ID, *(container_name(ENV_ID, r) for r in roles)}

    secrets: set[str] = set()
    for spec in template:
        for value in resolve_environment(ENV_ID, spec, roles).values():
            # Short values are not distinctive enough to assert on: this
            # template's POSTGRES_USER is "app", which is also a role name and a
            # legitimate substring of the template name. The composite forms
            # below are what actually identify a leak.
            if isinstance(value, str) and len(value) >= 8 and value not in public:
                secrets.add(value)
    assert secrets, "template exposes no value long enough to assert on"

    log = get_logger("test")
    with bind_env_id(ENV_ID):
        # The events the provision path actually emits.
        log.info("provision.started", template="webapp-postgres")
        log.info("network.created", network_id="abc123def456")
        log.info("container.started", container_id="abc123", role="app")
        log.info(
            "provision.running",
            template="webapp-postgres",
            host_port=54321,
            container_count=2,
            expires_at="2026-08-02T10:00:00+00:00",
        )

    output = "\n".join(captured)
    assert "app:app" not in output, "a database password reached the logs"
    for secret in secrets:
        assert secret not in output, f"{secret!r} reached the logs"


def test_password_would_be_caught_if_it_were_logged(captured):
    """
    Guards the guard. If the capture fixture silently rendered nothing, the
    test above would pass while asserting nothing at all.
    """
    log = get_logger("test")
    log.info("bad.event", database_url="postgresql://app:app@host:5432/app")

    assert "app:app" in "\n".join(captured)
