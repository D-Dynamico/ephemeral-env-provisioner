
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_principal
from api.schemas.environment_schema import (
    EnvironmentCreate,
    EnvironmentResponse,
    EnvironmentList,
    TaskAccepted,
)
from db.models import Environment, EnvironmentStatus
from db.session import get_db
from worker.tasks import provision_environment, teardown_environment

# Auth is applied to the whole router, not per route, so a route added later is
# protected by default. `/health` and the docs live outside it and stay open.
router = APIRouter(
    prefix="/environments",
    tags=["environments"],
    dependencies=[Depends(require_principal)],
    responses={401: {"description": "Missing or invalid X-API-Key header"}},
)


# ── POST /environments ─────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=TaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create and provision a new environment",
)
async def create_environment(
    payload: EnvironmentCreate,
    db: AsyncSession = Depends(get_db),
):
    # Guard: max environments per user
    count_result = await db.execute(
        select(func.count()).where(
            Environment.owner == payload.owner,
            Environment.status.notin_([
                EnvironmentStatus.STOPPED,
                EnvironmentStatus.FAILED,
            ])
        )
    )
    active_count = count_result.scalar_one()
    if active_count >= 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Max 5 active environments per user. Stop some before creating new ones.",
        )

    # Guard: unique name per owner
    existing = await db.execute(
        select(Environment).where(
            Environment.owner == payload.owner,
            Environment.name == payload.name,
            Environment.status.notin_([
                EnvironmentStatus.STOPPED,
                EnvironmentStatus.FAILED,
            ])
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active environment named '{payload.name}' already exists for this owner.",
        )

    env = Environment(
        name=payload.name,
        owner=payload.owner,
        template=payload.template,
        ttl_seconds=payload.ttl_seconds,
        status=EnvironmentStatus.PENDING,
    )
    db.add(env)
    await db.flush()  # get the generated UUID before committing

    # Enqueue Celery task
    task = provision_environment.delay(str(env.id))
    env.celery_task_id = task.id
    await db.commit()

    return TaskAccepted(
        environment_id=env.id,
        task_id=task.id,
    )


# ── GET /environments ──────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=EnvironmentList,
    summary="List environments (optionally filter by owner or status)",
)
async def list_environments(
    owner: Optional[str] = Query(None),
    status_filter: Optional[EnvironmentStatus] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(Environment)
    if owner:
        query = query.where(Environment.owner == owner)
    if status_filter:
        query = query.where(Environment.status == status_filter)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    query = query.order_by(Environment.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(query)).scalars().all()

    return EnvironmentList(total=total, items=rows)


# ── GET /environments/{id} ─────────────────────────────────────────────────────

@router.get(
    "/{env_id}",
    response_model=EnvironmentResponse,
    summary="Get a single environment by ID",
)
async def get_environment(
    env_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    env = await db.get(Environment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    return env


# ── DELETE /environments/{id} ──────────────────────────────────────────────────

@router.delete(
    "/{env_id}",
    response_model=TaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger teardown of an environment",
)
async def delete_environment(
    env_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    env = await db.get(Environment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")

    if env.status in (EnvironmentStatus.STOPPED, EnvironmentStatus.STOPPING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Environment is already {env.status.value}.",
        )

    task = teardown_environment.delay(str(env.id))
    env.celery_task_id = task.id
    await db.commit()

    return TaskAccepted(
        environment_id=env.id,
        task_id=task.id,
        message="Teardown started. Poll /environments/{id} for status.",
    )