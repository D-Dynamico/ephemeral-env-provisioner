import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import String, Integer, DateTime, Enum, JSON, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EnvironmentStatus(str, PyEnum):
    PENDING = "pending"         # Job enqueued, not started
    PROVISIONING = "provisioning"  # Containers being created
    RUNNING = "running"         # Healthy and accessible
    STOPPING = "stopping"       # Teardown in progress
    STOPPED = "stopped"         # Cleanly torn down
    FAILED = "failed"           # Something went wrong


class Environment(Base):
    __tablename__ = "environments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)  # user id or email

    # Status machine: pending → provisioning → running → stopping → stopped
    status: Mapped[EnvironmentStatus] = mapped_column(
        Enum(EnvironmentStatus),
        default=EnvironmentStatus.PENDING,
        nullable=False,
    )

    # Docker details
    template: Mapped[str] = mapped_column(String(50), nullable=False)  # which compose template
    container_ids: Mapped[list] = mapped_column(JSON, default=list)     # Docker container IDs
    network_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)  # exposed port (Phase 1)

    # Lifecycle
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=7200)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Error info
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Celery task tracking
    celery_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    def __repr__(self) -> str:
        return f"<Environment id={self.id} name={self.name} status={self.status}>"