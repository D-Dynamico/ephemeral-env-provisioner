"""
Template resolution and naming.

These are pure functions, so they get real coverage despite §8 — no daemon
involved. The rest of DockerManager still does not.
"""

from unittest.mock import MagicMock

import pytest

from config import settings
from docker_manager.compose import (
    TEMPLATES,
    DockerManager,
    container_name,
    network_name,
    resolve_environment,
    resource_limits,
    to_docker_healthcheck,
)

ENV_ID = "7eb66d91-b31d-4e8c-9d66-88ec645e25b6"


# ── Naming (invariant 6) ───────────────────────────────────────────────────────

def test_names_are_derived_from_the_env_id():
    assert network_name(ENV_ID) == f"env-{ENV_ID}"
    assert container_name(ENV_ID, "db") == f"env-{ENV_ID}-db"


# ── Resolution ─────────────────────────────────────────────────────────────────

def test_role_host_resolves_to_the_container_dns_name():
    """One role addresses another by container name inside the bridge network."""
    spec = {"environment": {"DATABASE_URL": "postgresql://app:app@{db_host}:5432/app"}}

    resolved = resolve_environment(ENV_ID, spec, ["db", "app"])

    assert resolved["DATABASE_URL"] == (
        f"postgresql://app:app@env-{ENV_ID}-db:5432/app"
    )
    assert resolved["DATABASE_URL"] == (
        f"postgresql://app:app@{container_name(ENV_ID, 'db')}:5432/app"
    )


def test_env_id_is_available():
    spec = {"environment": {"ENV_NAME": "preview-{env_id}"}}
    assert resolve_environment(ENV_ID, spec, ["db"])["ENV_NAME"] == f"preview-{ENV_ID}"


def test_values_without_placeholders_pass_through():
    spec = {"environment": {"POSTGRES_USER": "app", "POSTGRES_DB": "app"}}
    assert resolve_environment(ENV_ID, spec, ["db"]) == {
        "POSTGRES_USER": "app",
        "POSTGRES_DB": "app",
    }


def test_missing_environment_block_is_empty_not_an_error():
    assert resolve_environment(ENV_ID, {"role": "db"}, ["db"]) == {}
    assert resolve_environment(ENV_ID, {"environment": None}, ["db"]) == {}


def test_non_string_values_are_left_alone():
    """Docker accepts ints; formatting them would be a type error."""
    spec = {"environment": {"PORT": 5432, "DEBUG": True}}
    assert resolve_environment(ENV_ID, spec, ["db"]) == {"PORT": 5432, "DEBUG": True}


def test_unknown_placeholder_fails_at_provision_time():
    """
    Better a provision that raises than a container holding a half-substituted
    connection string that fails somewhere less obvious.
    """
    spec = {"environment": {"DATABASE_URL": "postgresql://{cache_host}/app"}}

    with pytest.raises(ValueError, match="matches no role"):
        resolve_environment(ENV_ID, spec, ["db", "app"])


def test_resolution_is_deterministic():
    """Invariant 2: a task may run twice and must build the same stack."""
    spec = {"environment": {"DATABASE_URL": "postgresql://{db_host}/app"}}
    assert resolve_environment(ENV_ID, spec, ["db"]) == resolve_environment(
        ENV_ID, spec, ["db"]
    )


# ── The shipped template ───────────────────────────────────────────────────────

def test_webapp_postgres_resolves_cleanly():
    """Every placeholder in the shipped template matches a role in it."""
    template = TEMPLATES["webapp-postgres"]
    roles = [s["role"] for s in template]

    for spec in template:
        resolve_environment(ENV_ID, spec, roles)  # must not raise


def test_every_template_resolves_cleanly():
    """Guards the next template added, not just this one."""
    for name, template in TEMPLATES.items():
        roles = [s["role"] for s in template]
        for spec in template:
            try:
                resolve_environment(ENV_ID, spec, roles)
            except ValueError as exc:
                pytest.fail(f"template {name!r}, role {spec['role']!r}: {exc}")


def test_db_role_has_a_healthcheck():
    """
    Anything that connects to Postgres on boot depends on this. Without it the
    app races the database and the failure is intermittent.
    """
    db = next(s for s in TEMPLATES["webapp-postgres"] if s["role"] == "db")
    assert db["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "pg_isready" in db["healthcheck"]["test"][1]


# ── Healthcheck translation ────────────────────────────────────────────────────

def test_seconds_become_nanoseconds():
    """Templates are written in seconds; Docker's API wants nanoseconds."""
    hc = to_docker_healthcheck({
        "test": ["CMD-SHELL", "true"],
        "interval_seconds": 2,
        "timeout_seconds": 3,
        "start_period_seconds": 1,
        "retries": 15,
    })

    assert hc == {
        "test": ["CMD-SHELL", "true"],
        "interval": 2_000_000_000,
        "timeout": 3_000_000_000,
        "start_period": 1_000_000_000,
        "retries": 15,
    }


def test_optional_healthcheck_fields_are_omitted():
    """Docker applies its own defaults; sending zeros would override them."""
    assert to_docker_healthcheck({"test": ["CMD", "true"]}) == {"test": ["CMD", "true"]}


def test_shipped_healthchecks_translate():
    for template in TEMPLATES.values():
        for spec in template:
            if spec.get("healthcheck"):
                assert "test" in to_docker_healthcheck(spec["healthcheck"])


# ── Resource limits (§9) ───────────────────────────────────────────────────────

def test_limits_fall_back_to_the_settings_defaults():
    """A spec that says nothing still gets a ceiling, never an unlimited one."""
    limits = resource_limits({"role": "app"})

    assert limits["mem_limit"] == settings.default_container_memory_limit
    assert limits["nano_cpus"] == int(settings.default_container_cpu_limit * 1_000_000_000)
    assert limits["pids_limit"] == settings.default_container_pids_limit


def test_spec_overrides_win_and_cores_become_nano_cpus():
    limits = resource_limits({"memory_limit": "256m", "cpu_limit": 0.5, "pids_limit": 64})

    assert limits["mem_limit"] == "256m"
    assert limits["nano_cpus"] == 500_000_000
    assert limits["pids_limit"] == 64


def test_swap_cannot_be_used_to_evade_the_memory_limit():
    limits = resource_limits({"memory_limit": "256m"})
    assert limits["memswap_limit"] == limits["mem_limit"]


def test_every_role_in_every_template_is_bounded():
    """
    Guards the next template added. An unbounded container is a denial of
    service against this service's own host (§9).
    """
    for name, template in TEMPLATES.items():
        for spec in template:
            limits = resource_limits(spec)
            where = f"template {name!r}, role {spec['role']!r}"
            assert limits["mem_limit"], where
            assert limits["nano_cpus"] > 0, where
            assert limits["pids_limit"] > 0, where


def test_start_container_passes_limits_to_docker():
    """
    The helper is only a guard if its output actually reaches `containers.run`.
    """
    mgr = DockerManager()
    mgr._client = MagicMock()
    network = MagicMock()
    network.name = network_name(ENV_ID)
    spec = {"role": "db", "image": "postgres:16-alpine", "memory_limit": "256m",
            "cpu_limit": 0.5}

    mgr._start_container(env_id=ENV_ID, spec=spec, network=network, environment={})

    kwargs = mgr._client.containers.run.call_args.kwargs
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["memswap_limit"] == "256m"
    assert kwargs["nano_cpus"] == 500_000_000
    assert kwargs["pids_limit"] == settings.default_container_pids_limit


# ── Readiness ──────────────────────────────────────────────────────────────────

def _container(status="running", health=None, oom=False):
    c = MagicMock()
    c.status = status
    c.short_id = "abc123"
    state: dict = {"Health": {"Status": health}} if health else {}
    if oom:
        state["OOMKilled"] = True
    c.attrs = {"State": state}
    c.reload = MagicMock()
    return c


def test_ready_when_healthcheck_passes():
    mgr = DockerManager()
    spec = {"role": "db", "healthcheck": {"test": ["CMD", "true"]}}

    mgr._wait_for_ready(_container(health="healthy"), spec, timeout=5)  # must not raise


def test_running_is_not_ready_when_a_healthcheck_exists():
    """
    The bug this replaces: `running` was treated as ready, so a dependant
    booted while Postgres was still starting.
    """
    mgr = DockerManager()
    spec = {"role": "db", "healthcheck": {"test": ["CMD", "true"]}}

    with pytest.raises(TimeoutError):
        mgr._wait_for_ready(_container(health="starting"), spec, timeout=1)


def test_running_is_ready_without_a_healthcheck():
    """No healthcheck means `running` is the best signal available."""
    mgr = DockerManager()

    mgr._wait_for_ready(_container(), {"role": "app"}, timeout=5)  # must not raise


def test_unhealthy_fails_fast():
    mgr = DockerManager()
    spec = {"role": "db", "healthcheck": {"test": ["CMD", "true"]}}

    with pytest.raises(RuntimeError, match="failed its healthcheck"):
        mgr._wait_for_ready(_container(health="unhealthy"), spec, timeout=30)


def test_exited_container_fails_fast():
    mgr = DockerManager()

    with pytest.raises(RuntimeError, match="died during startup"):
        mgr._wait_for_ready(_container(status="exited"), {"role": "app"}, timeout=30)


def test_oom_kill_names_the_memory_limit():
    """
    An OOM kill and a crash look identical from `status`. Reporting them the
    same way sends the next reader hunting the wrong bug.
    """
    mgr = DockerManager()
    spec = {"role": "app", "memory_limit": "384m"}

    with pytest.raises(RuntimeError, match="exceeded its memory limit of 384m"):
        mgr._wait_for_ready(_container(status="exited", oom=True), spec, timeout=30)


def test_ready_timeout_defaults_to_the_setting():
    assert settings.container_ready_timeout_seconds < settings.provisioning_timeout_seconds, (
        "a container wait longer than the staleness timeout would let the sweep "
        "fail the row while the worker is still working on it"
    )
