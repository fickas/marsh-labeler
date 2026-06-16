"""add class_scheme to flights

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("flights", sa.Column("class_scheme", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("flights", "class_scheme")
