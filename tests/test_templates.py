"""
Template resolution and naming.

These are pure functions, so they get real coverage despite §8 — no daemon
involved. The rest of DockerManager still does not.
"""

import pytest

from docker_manager.compose import (
    TEMPLATES,
    container_name,
    network_name,
    resolve_environment,
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
