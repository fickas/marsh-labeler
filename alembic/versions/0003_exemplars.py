"""add exemplars table

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SRID = 26919


def upgrade() -> None:
    op.create_table(
        "exemplars",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "flight_id",
            sa.Integer,
            sa.ForeignKey("flights.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("class_id", sa.Integer, nullable=False, index=True),
        sa.Column("source_fid", sa.Integer),
        sa.Column("chip_keys", JSONB),
        sa.Column("geom", Geometry("POLYGON", srid=SRID)),
        sa.Column("notes", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_exemplars_geom", "exemplars", ["geom"], postgresql_using="gist"
    )


def downgrade() -> None:
    op.drop_index("ix_exemplars_geom", table_name="exemplars")
    op.drop_table("exemplars")
