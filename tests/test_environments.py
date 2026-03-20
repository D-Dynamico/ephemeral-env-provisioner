"""
Phase 1 tests — cover the API routes with a mocked Celery task
and an in-memory SQLite database (no Docker needed to run tests).
"""

import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, MagicMock

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from db.models import Base, EnvironmentStatus
from db.session import get_db
from api.main import app

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


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


# ── Helpers ────────────────────────────────────────────────────────────────────

def mock_celery_task():
    task = MagicMock()
    task.id = str(uuid.uuid4())
    return task


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_create_environment(client):
    with patch("api.routes.environment_router.provision_environment") as mock_task:
        mock_task.delay.return_value = mock_celery_task()

        r = await client.post("/environments/", json={
            "name": "pr-42",
            "owner": "dev@example.com",
            "template": "webapp-postgres",
            "ttl_seconds": 3600,
        })

    assert r.status_code == 202
    body = r.json()
    assert "environment_id" in body
    assert "task_id" in body
    mock_task.delay.assert_called_once()


@pytest.mark.asyncio
async def test_create_duplicate_environment(client):
    with patch("api.routes.environment_router.provision_environment") as mock_task:
        mock_task.delay.return_value = mock_celery_task()

        await client.post("/environments/", json={
            "name": "pr-42",
            "owner": "dev@example.com",
            "template": "webapp-postgres",
        })

        # Second create with same name + owner should 409
        r = await client.post("/environments/", json={
            "name": "pr-42",
            "owner": "dev@example.com",
            "template": "webapp-postgres",
        })

    assert r.status_code == 409


@pytest.mark.asyncio
async def test_get_environment(client):
    with patch("api.routes.environment_router.provision_environment") as mock_task:
        mock_task.delay.return_value = mock_celery_task()
        create_r = await client.post("/environments/", json={
            "name": "pr-99",
            "owner": "dev@example.com",
            "template": "webapp-postgres",
        })

    env_id = create_r.json()["environment_id"]
    r = await client.get(f"/environments/{env_id}")
    assert r.status_code == 200
    assert r.json()["id"] == env_id
    assert r.json()["status"] == EnvironmentStatus.PENDING.value


@pytest.mark.asyncio
async def test_get_nonexistent_environment(client):
    r = await client.get(f"/environments/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_environments(client):
    with patch("api.routes.environment_router.provision_environment") as mock_task:
        mock_task.delay.return_value = mock_celery_task()
        for i in range(3):
            await client.post("/environments/", json={
                "name": f"env-{i}",
                "owner": "dev@example.com",
                "template": "webapp-postgres",
            })

    r = await client.get("/environments/?owner=dev@example.com")
    assert r.status_code == 200
    assert r.json()["total"] == 3


@pytest.mark.asyncio
async def test_invalid_env_name(client):
    r = await client.post("/environments/", json={
        "name": "UPPERCASE_INVALID",
        "owner": "dev@example.com",
        "template": "webapp-postgres",
    })
    assert r.status_code == 422  # Pydantic validation error