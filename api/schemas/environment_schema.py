import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from db.models import EnvironmentStatus


# ── Request schemas ────────────────────────────────────────────────────────────

class EnvironmentCreate(BaseModel):
    # No `owner` field. The owner is the principal behind the API key: the quota
    # and the unique-name guard are enforced per owner, and an owner the caller
    # picks is not a guard at all.
    name: str = Field(..., min_length=3, max_length=100, pattern=r"^[a-z0-9\-]+$",
                      description="Lowercase alphanumeric slug, e.g. 'my-feature-branch'")
    template: str = Field(default="webapp-postgres",
                          description="Which stack template to spin up")
    ttl_seconds: int = Field(default=7200, ge=300, le=86400,
                             description="Lifetime in seconds (5 min – 24 hrs)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "pr-42-auth-refactor",
                "template": "webapp-postgres",
                "ttl_seconds": 3600,
            }
        }
    }


# ── Response schemas ───────────────────────────────────────────────────────────

class EnvironmentResponse(BaseModel):
    id: uuid.UUID
    name: str
    owner: str
    status: EnvironmentStatus
    template: str
    host_port: Optional[int]
    ttl_seconds: int
    created_at: datetime
    started_at: Optional[datetime]
    expires_at: Optional[datetime]
    stopped_at: Optional[datetime]
    error_message: Optional[str]
    celery_task_id: Optional[str]

    model_config = {"from_attributes": True}


class EnvironmentList(BaseModel):
    total: int
    items: list[EnvironmentResponse]


class TaskAccepted(BaseModel):
    """Returned immediately when a job is enqueued — client should poll status."""
    environment_id: uuid.UUID
    task_id: str
    message: str = "Provisioning started. Poll /environments/{id} for status."