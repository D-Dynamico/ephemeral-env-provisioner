"""
Phase 1 tests — cover the API routes with a mocked Celery task
and an in-memory SQLite database (no Docker needed to run tests).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from db.models import Base, Environment, EnvironmentStatus
from db.session import get_db
from api.main import app
import worker.tasks as worker_tasks
from docker_manager.compose import DockerManager, LabelledResources, LABEL_ENV_ID

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


# ── Reaper tests ───────────────────────────────────────────────────────────────
# The reaper runs in the Celery worker, which uses sync SQLAlchemy — so it needs
# its own sync SQLite DB rather than the async one the API tests share.

@pytest.fixture
def sync_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # keep one connection so :memory: survives
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def make_env(name: str, expires_at, status=EnvironmentStatus.RUNNING) -> Environment:
    return Environment(
        name=name,
        owner="dev@example.com",
        template="webapp-postgres",
        status=status,
        ttl_seconds=300,
        expires_at=expires_at,
    )


async def test_reaper_tears_down_only_expired(sync_sessions):
    now = datetime.now(timezone.utc)
    session = sync_sessions()
    expired = make_env("old-env", now - timedelta(minutes=5))
    alive = make_env("new-env", now + timedelta(hours=1))
    session.add_all([expired, alive])
    session.commit()
    expired_id, alive_id = expired.id, alive.id
    session.close()

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks.teardown_environment, "delay") as mock_delay:
        result = worker_tasks.reap_expired_environments()

    assert result == {"reaped": 1}
    mock_delay.assert_called_once_with(str(expired_id))

    check = sync_sessions()
    assert check.get(Environment, expired_id).status == EnvironmentStatus.STOPPING
    assert check.get(Environment, alive_id).status == EnvironmentStatus.RUNNING
    check.close()


async def test_teardown_marks_failed_when_retries_exhausted(sync_sessions):
    """A teardown that exhausts its retries must not leave the row in STOPPING."""
    session = sync_sessions()
    env = make_env("doomed-env", datetime.now(timezone.utc) - timedelta(minutes=5))
    session.add(env)
    session.commit()
    env_id = env.id
    session.close()

    boom = RuntimeError("docker daemon unreachable")
    task = worker_tasks.teardown_environment
    # push_request simulates the final attempt. Call .run() rather than the task
    # itself: Task.__call__ pushes its own request, which would reset retries to 0.
    task.push_request(retries=task.max_retries)
    try:
        with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
             patch.object(worker_tasks.docker_manager, "teardown", side_effect=boom), \
             pytest.raises(RuntimeError):
            task.run(str(env_id))
    finally:
        task.pop_request()

    check = sync_sessions()
    row = check.get(Environment, env_id)
    assert row.status == EnvironmentStatus.FAILED
    assert "docker daemon unreachable" in row.error_message
    check.close()


async def test_reaper_ignores_non_running(sync_sessions):
    """An already-stopping env must not be enqueued twice."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    session = sync_sessions()
    session.add(make_env("stopping-env", past, status=EnvironmentStatus.STOPPING))
    session.commit()
    session.close()

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks.teardown_environment, "delay") as mock_delay:
        result = worker_tasks.reap_expired_environments()

    assert result == {"reaped": 0}
    mock_delay.assert_not_called()

# ── Orphan sweep tests ─────────────────────────────────────────────────────────

def _docker_ts(dt: datetime) -> str:
    """Docker reports RFC3339 with nanosecond precision."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


class _FakeContainer:
    def __init__(self, cid, env_id, created):
        self.id = cid
        self.labels = {LABEL_ENV_ID: env_id}
        self.attrs = {"Created": _docker_ts(created)}


class _FakeNetwork:
    def __init__(self, nid, env_id, created):
        self.id = nid
        self.attrs = {"Created": _docker_ts(created), "Labels": {LABEL_ENV_ID: env_id}}


def _manager_with(containers=(), networks=()):
    mgr = DockerManager()
    client = MagicMock()
    client.containers.list.return_value = list(containers)
    client.networks.list.return_value = list(networks)
    mgr._client = client
    return mgr, client


async def test_find_labelled_honours_grace_period():
    """A stack still being provisioned must never be swept out from under it."""
    now = datetime.now(timezone.utc)
    old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    mgr, _ = _manager_with(
        containers=[
            _FakeContainer("c-old", old_id, now - timedelta(hours=2)),
            _FakeContainer("c-new", new_id, now),
        ],
        networks=[_FakeNetwork("n-old", old_id, now - timedelta(hours=2))],
    )

    found = mgr.find_labelled(min_age_seconds=900)

    assert set(found) == {old_id}, "young resources must be skipped"
    assert found[old_id].container_ids == ["c-old"]
    assert found[old_id].network_ids == ["n-old"]


async def test_remove_resources_tolerates_already_gone():
    """Invariant 2: removing what is already removed is a no-op, not an error."""
    import docker.errors

    mgr, client = _manager_with()
    client.containers.get.side_effect = docker.errors.NotFound("gone")
    client.networks.get.side_effect = docker.errors.NotFound("gone")

    mgr.remove_resources("some-env", ["c1"], ["n1"])  # must not raise


async def test_reconcile_removes_orphans_with_no_db_row(sync_sessions):
    orphan_id = str(uuid.uuid4())
    fake = MagicMock()
    fake.find_labelled.return_value = {
        orphan_id: LabelledResources(container_ids=["c1"], network_ids=["n1"])
    }

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks, "docker_manager", fake):
        result = worker_tasks.reconcile_orphans()

    assert result == {"removed": 1, "skipped": 0}
    fake.remove_resources.assert_called_once_with(
        env_id=orphan_id, container_ids=["c1"], network_ids=["n1"]
    )


async def test_reconcile_spares_running_and_sweeps_terminal(sync_sessions):
    """Status decides: only rows that should own nothing get their resources cut."""
    session = sync_sessions()
    running = make_env("live", datetime.now(timezone.utc) + timedelta(hours=1))
    stopped = make_env("dead", datetime.now(timezone.utc),
                       status=EnvironmentStatus.STOPPED)
    session.add_all([running, stopped])
    session.commit()
    running_id, stopped_id = str(running.id), str(stopped.id)
    session.close()

    fake = MagicMock()
    fake.find_labelled.return_value = {
        running_id: LabelledResources(container_ids=["c-live"], network_ids=[]),
        stopped_id: LabelledResources(container_ids=["c-dead"], network_ids=["n-dead"]),
    }

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks, "docker_manager", fake):
        result = worker_tasks.reconcile_orphans()

    assert result == {"removed": 1, "skipped": 1}
    fake.remove_resources.assert_called_once_with(
        env_id=stopped_id, container_ids=["c-dead"], network_ids=["n-dead"]
    )


async def test_reconcile_skips_unparseable_label(sync_sessions):
    """A label that is not a UUID is not ours to delete."""
    fake = MagicMock()
    fake.find_labelled.return_value = {
        "not-a-uuid": LabelledResources(container_ids=["c1"], network_ids=[])
    }

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks, "docker_manager", fake):
        result = worker_tasks.reconcile_orphans()

    assert result == {"removed": 0, "skipped": 1}
    fake.remove_resources.assert_not_called()


async def test_reconcile_second_run_is_noop(sync_sessions):
    """Invariant 2: once swept, a second pass finds nothing left to do."""
    orphan_id = str(uuid.uuid4())
    fake = MagicMock()
    fake.find_labelled.side_effect = [
        {orphan_id: LabelledResources(container_ids=["c1"], network_ids=["n1"])},
        {},  # resources are gone now
    ]

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks, "docker_manager", fake):
        first = worker_tasks.reconcile_orphans()
        second = worker_tasks.reconcile_orphans()

    assert first == {"removed": 1, "skipped": 0}
    assert second == {"removed": 0, "skipped": 0}
    assert fake.remove_resources.call_count == 1


async def test_reaper_second_run_is_noop(sync_sessions):
    """Invariant 2: the claim means a second tick does not re-enqueue teardown."""
    session = sync_sessions()
    session.add(make_env("old-env", datetime.now(timezone.utc) - timedelta(minutes=5)))
    session.commit()
    session.close()

    with patch.object(worker_tasks, "get_sync_db", lambda: sync_sessions()), \
         patch.object(worker_tasks.teardown_environment, "delay") as mock_delay:
        first = worker_tasks.reap_expired_environments()
        second = worker_tasks.reap_expired_environments()

    assert first == {"reaped": 1}
    assert second == {"reaped": 0}
    assert mock_delay.call_count == 1
