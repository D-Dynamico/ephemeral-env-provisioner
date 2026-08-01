"""
Auth tests. The service fronts a root-equivalent Docker socket, so these cover
the gate itself, not just the happy path (CLAUDE.md §9).
"""

import uuid
from unittest.mock import patch, MagicMock

import pytest

from api.auth import API_KEY_HEADER, _parse, api_key_map, MIN_KEY_LENGTH
from config import settings
from tests.conftest import TEST_API_KEY, TEST_PRINCIPAL, OTHER_PRINCIPAL


def _mock_task():
    task = MagicMock()
    task.id = str(uuid.uuid4())
    return task


async def _create(http_client, name="pr-42"):
    with patch("api.routes.environment_router.provision_environment") as mock_task:
        mock_task.delay.return_value = _mock_task()
        return await http_client.post(
            "/environments/", json={"name": name, "template": "webapp-postgres"}
        )


# ── The gate ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_key_is_rejected(anon_client):
    r = await anon_client.get("/environments/")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_key_is_rejected(anon_client):
    r = await anon_client.get(
        "/environments/", headers={API_KEY_HEADER: "k_wrong_0123456789abcdef"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_missing_and_wrong_key_are_indistinguishable(anon_client):
    """Probing must not reveal whether a key exists, only that auth failed."""
    missing = await anon_client.get("/environments/")
    wrong = await anon_client.get(
        "/environments/", headers={API_KEY_HEADER: "k_wrong_0123456789abcdef"}
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


@pytest.mark.asyncio
async def test_valid_key_is_accepted(client):
    r = await client.get("/environments/")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_write_routes_are_gated(anon_client):
    """The POST path starts containers. It must never be reachable unauthenticated."""
    r = await anon_client.post("/environments/", json={
        "name": "pr-42",
        "template": "webapp-postgres",
    })
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_is_gated(anon_client):
    r = await anon_client.delete(f"/environments/{uuid.uuid4()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_stays_open(anon_client):
    """Liveness carries no data and no ability to act."""
    r = await anon_client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_unconfigured_auth_fails_closed(anon_client, monkeypatch):
    """
    Startup already refuses an empty API_KEYS. If it is emptied while running,
    the dependency must refuse too rather than admit everyone.
    """
    monkeypatch.setattr(settings, "api_keys", "")
    r = await anon_client.get("/environments/")
    assert r.status_code == 503

    # A previously valid key is not grandfathered in.
    r = await anon_client.get("/environments/", headers={API_KEY_HEADER: TEST_API_KEY})
    assert r.status_code == 503


# ── Ownership comes from the key ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_is_the_principal_not_the_body(client):
    """A caller-supplied owner is ignored; the body field no longer exists."""
    r = await _create(client)
    assert r.status_code == 202

    env_id = r.json()["environment_id"]
    assert (await client.get(f"/environments/{env_id}")).json()["owner"] == TEST_PRINCIPAL


@pytest.mark.asyncio
async def test_owner_in_body_is_rejected_not_honoured(client):
    """
    Extra fields must not silently set the owner. Pydantic ignores unknown keys
    by default, so this asserts the owner is the principal regardless.
    """
    with patch("api.routes.environment_router.provision_environment") as mock_task:
        mock_task.delay.return_value = _mock_task()
        r = await client.post("/environments/", json={
            "name": "pr-77",
            "template": "webapp-postgres",
            "owner": "victim@example.com",
        })

    env_id = r.json()["environment_id"]
    body = (await client.get(f"/environments/{env_id}")).json()
    assert body["owner"] == TEST_PRINCIPAL


@pytest.mark.asyncio
async def test_other_principal_cannot_read(client, other_client):
    env_id = (await _create(client)).json()["environment_id"]

    r = await other_client.get(f"/environments/{env_id}")
    assert r.status_code == 404, "must not confirm the id exists"


@pytest.mark.asyncio
async def test_other_principal_cannot_delete(client, other_client):
    env_id = (await _create(client)).json()["environment_id"]

    r = await other_client.delete(f"/environments/{env_id}")
    assert r.status_code == 404

    # And the environment is untouched.
    assert (await client.get(f"/environments/{env_id}")).status_code == 200


@pytest.mark.asyncio
async def test_list_shows_only_the_callers_environments(client, other_client):
    await _create(client, name="mine")
    await _create(other_client, name="theirs")

    mine = (await client.get("/environments/")).json()
    theirs = (await other_client.get("/environments/")).json()

    assert mine["total"] == 1
    assert mine["items"][0]["name"] == "mine"
    assert mine["items"][0]["owner"] == TEST_PRINCIPAL
    assert theirs["total"] == 1
    assert theirs["items"][0]["name"] == "theirs"
    assert theirs["items"][0]["owner"] == OTHER_PRINCIPAL


@pytest.mark.asyncio
async def test_same_name_allowed_across_principals(client, other_client):
    """The 409 is per owner, and owners are now real."""
    assert (await _create(client, name="pr-42")).status_code == 202
    assert (await _create(other_client, name="pr-42")).status_code == 202


@pytest.mark.asyncio
async def test_quota_is_per_principal(client, other_client, monkeypatch):
    """
    The quota is the only bound on how many containers a caller can start, so
    it must not be evadable by naming a different owner.
    """
    monkeypatch.setattr(settings, "max_environments_per_user", 2)

    assert (await _create(client, name="one")).status_code == 202
    assert (await _create(client, name="two")).status_code == 202
    assert (await _create(client, name="three")).status_code == 429

    # A different principal has its own budget.
    assert (await _create(other_client, name="one")).status_code == 202


@pytest.mark.asyncio
async def test_quota_reads_the_setting(client, monkeypatch):
    """settings.max_environments_per_user was defined and unused; wire it."""
    monkeypatch.setattr(settings, "max_environments_per_user", 1)

    assert (await _create(client, name="one")).status_code == 202
    r = await _create(client, name="two")
    assert r.status_code == 429
    assert "Max 1 active" in r.json()["detail"]


# ── Key parsing ────────────────────────────────────────────────────────────────

def test_parse_maps_keys_to_principals():
    assert api_key_map()[TEST_API_KEY] == TEST_PRINCIPAL


def test_parse_ignores_blanks_and_whitespace():
    parsed = _parse(f" {'a' * 20}:alice , , {'b' * 20}:bob ")
    assert parsed == {"a" * 20: "alice", "b" * 20: "bob"}


def test_parse_empty_is_empty():
    assert _parse("") == {}


@pytest.mark.parametrize("raw", [
    "no-colon-here-at-all-x",          # missing separator
    f"{'a' * 20}:",                     # missing principal
    ":alice",                           # missing key
])
def test_parse_rejects_malformed_entries(raw):
    with pytest.raises(ValueError):
        _parse(raw)


def test_parse_rejects_short_keys():
    """A key short enough to guess is the same as no key."""
    short = "a" * (MIN_KEY_LENGTH - 1)
    with pytest.raises(ValueError, match="minimum"):
        _parse(f"{short}:alice")


def test_parse_rejects_duplicate_keys():
    """One key resolving to two principals would make ownership ambiguous."""
    key = "k" * 20
    with pytest.raises(ValueError, match="Duplicate"):
        _parse(f"{key}:alice,{key}:bob")
