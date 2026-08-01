"""add status_changed_at to environments

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-01

Adds the timestamp that staleness sweeps key off. Nullable, so existing rows do
not need the add-nullable / backfill / set-not-null dance; a NULL simply means
"never observed changing state" and every sweep must treat it as not-stale
rather than infinitely stale.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "environments",
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Seed existing rows so they are not permanently invisible to staleness
    # sweeps. created_at is the closest honest approximation available.
    op.execute(
        "UPDATE environments SET status_changed_at = created_at "
        "WHERE status_changed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("environments", "status_changed_at")
