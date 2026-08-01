"""
API-key authentication.

The service needs the Docker socket, which is root-equivalent on the host, so an
endpoint that can start containers is remote code execution in practice
(CLAUDE.md §9). A key is required for every route that touches an environment.

Keys map to a *principal*: the identity that owns the environments created with
that key. Callers do not name their own owner, because the quota and the
unique-name guard are enforced per owner and a caller-supplied one is not a
guard at all.

This is a static allowlist, not user management. There is no rotation, no
expiry, no hashing at rest, no per-key scope and no rate limiting. Keys live in
the process environment and are readable by anything that can read it.
"""

import hmac
from functools import lru_cache

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from config import settings

API_KEY_HEADER = "X-API-Key"

# A key short enough to guess is the same as no key. Long enough that brute
# force over the network is not the weak link.
MIN_KEY_LENGTH = 16

_api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,  # we raise, so the 401 body matches the rest of the API
    description="Static API key. Identifies the principal that owns the environment.",
)


@lru_cache(maxsize=8)
def _parse(raw: str) -> dict[str, str]:
    """
    Parse `key:principal,key:principal` into {key: principal}.

    Cached on the raw string rather than on the settings object, so overriding
    `settings.api_keys` in a test takes effect with no cache to invalidate.

    Callers must not mutate the returned dict; it is shared.
    """
    keys: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, principal = pair.partition(":")
        principal = principal.strip()
        if not sep or not key or not principal:
            raise ValueError(
                "API_KEYS entries must be 'key:principal'. "
                f"Got an entry with {'no principal' if sep else 'no colon'}."
            )
        if len(key) < MIN_KEY_LENGTH:
            raise ValueError(
                f"API key for principal '{principal}' is {len(key)} characters; "
                f"minimum is {MIN_KEY_LENGTH}."
            )
        if key in keys:
            raise ValueError(
                f"Duplicate API key maps to both '{keys[key]}' and '{principal}'."
            )
        keys[key] = principal
    return keys


def api_key_map() -> dict[str, str]:
    """The configured key → principal mapping. Empty when auth is unconfigured."""
    return _parse(settings.api_keys)


def _principal_for(candidate: str, keys: dict[str, str]) -> str | None:
    """
    Constant-time lookup.

    Every entry is compared and there is no early return, so response timing
    does not reveal how much of a key was correct or which principal matched.
    """
    match: str | None = None
    for key, principal in keys.items():
        if hmac.compare_digest(key.encode("utf-8"), candidate.encode("utf-8")):
            match = principal
    return match


async def require_principal(key: str | None = Security(_api_key_header)) -> str:
    """
    Resolve the caller to a principal, or reject.

    Fails closed: with no keys configured this refuses every request rather than
    waving them through. Startup also refuses (see `api.main.lifespan`), so this
    branch only fires if the configuration is emptied while running.
    """
    keys = api_key_map()
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key authentication is not configured.",
        )

    principal = _principal_for(key, keys) if key else None
    if principal is None:
        # Same response for missing and wrong, so probing cannot tell them apart.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {API_KEY_HEADER} header.",
        )
    return principal


# Convenience alias so routes read as `principal: str = Depends(CurrentPrincipal)`.
CurrentPrincipal = Depends(require_principal)
