"""
Auth tests. The service fronts a root-equivalent Docker socket, so these cover
the gate itself, not just the happy path (CLAUDE.md §9).
"""

import pytest

from api.auth import API_KEY_HEADER, _parse, api_key_map, MIN_KEY_LENGTH
from config import settings
from tests.conftest import TEST_API_KEY, TEST_PRINCIPAL


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
    import uuid
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
