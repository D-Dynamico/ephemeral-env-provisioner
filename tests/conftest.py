"""
Shared fixtures: in-memory SQLite, dependency overrides, and authenticated
clients. No Docker daemon required (CLAUDE.md §8).
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from api.auth import API_KEY_HEADER
from api.main import app
from config import settings
from db.models import Base
from db.session import get_db

# ── Auth ───────────────────────────────────────────────────────────────────────
# Two principals, so cross-principal isolation is testable. Keys are >= the 16
# character minimum enforced by api.auth.

TEST_API_KEY = "k_test_alice_0123456789"
TEST_PRINCIPAL = "dev@example.com"

OTHER_API_KEY = "k_test_bob_9876543210fedcba"
OTHER_PRINCIPAL = "other@example.com"

TEST_API_KEYS = f"{TEST_API_KEY}:{TEST_PRINCIPAL},{OTHER_API_KEY}:{OTHER_PRINCIPAL}"


@pytest.fixture(autouse=True)
def configured_api_keys(monkeypatch):
    """
    Every test runs against a known key map. `api.auth` caches on the raw
    string, so overriding the setting is enough — there is no cache to clear.
    """
    monkeypatch.setattr(settings, "api_keys", TEST_API_KEYS)


# ── Test DB (SQLite in-memory) ─────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSession = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    # aiosqlite opens a non-daemon thread per connection; without this the
    # interpreter hangs at exit after the last test reports.
    await test_engine.dispose()


# ── Clients ────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    """Authenticated as TEST_PRINCIPAL."""
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={API_KEY_HEADER: TEST_API_KEY},
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def other_client():
    """Authenticated as OTHER_PRINCIPAL. Shares the database with `client`."""
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={API_KEY_HEADER: OTHER_API_KEY},
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def anon_client():
    """No API key header at all."""
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()
