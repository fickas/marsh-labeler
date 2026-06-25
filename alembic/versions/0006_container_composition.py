"""containers.composition -- per-superpixel confident/abstain decomposition

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-17

Adds a nullable JSONB `composition` to containers, holding the breakdown the
labeler sees: total pixels, the confident-class counts, and the abstain
"questions" (contested-pair counts) plus diffuse count. It's computed at ingest
from the abstain + softmax rasters, so existing rows stay null until re-ingested
(re-running ingest_flight replaces the round's containers and fills it in).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("containers", sa.Column("composition", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("containers", "composition")
