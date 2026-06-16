"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SRID = 26919


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "flights",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("crs", sa.String, nullable=False, server_default=f"EPSG:{SRID}"),
        sa.Column("gsd_cm", sa.Float),
        sa.Column("ortho_path", sa.String),
        sa.Column("superpixel_path", sa.String),
        sa.Column("abstain_path", sa.String),
        sa.Column("notes", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "containers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "flight_id",
            sa.Integer,
            sa.ForeignKey("flights.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("superpixel_id", sa.Integer, nullable=False),
        sa.Column("geom", Geometry("POLYGON", srid=SRID)),
        sa.Column("n_pixels", sa.Integer, server_default="0"),
        sa.Column("abstain_frac", sa.Float, server_default="0"),
        sa.Column("pair_purity", sa.Float),
        sa.Column("diffuse_frac", sa.Float),
        sa.Column("pair_code", sa.Integer),
        sa.Column("class_a", sa.Integer),
        sa.Column("class_b", sa.Integer),
        sa.Column("is_diffuse", sa.Boolean, server_default=sa.false()),
        sa.Column("model_probs", JSONB),
        sa.Column("chip_keys", JSONB),
        sa.Column("priority", sa.Float, server_default="0", index=True),
        sa.Column("replication_target", sa.Integer, server_default="1"),
        sa.Column("status", sa.String, server_default="pending", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("flight_id", "superpixel_id", name="uq_container_flight_sp"),
        sa.CheckConstraint(
            "status in ('pending','in_progress','done','skipped')",
            name="ck_container_status",
        ),
    )
    # GiST spatial index on the geometry column.
    op.create_index(
        "ix_containers_geom", "containers", ["geom"], postgresql_using="gist"
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String),
        sa.Column("role", sa.String, nullable=False, server_default="labeler"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "role in ('labeler','admin','ecologist')", name="ck_user_role"
        ),
    )

    op.create_table(
        "labels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "container_id",
            sa.Integer,
            sa.ForeignKey("containers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String, nullable=False, server_default="label"),
        sa.Column("class_id", sa.Integer),
        sa.Column("confidence", sa.SmallInteger),
        sa.Column("notes", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("container_id", "user_id", name="uq_label_container_user"),
        sa.CheckConstraint(
            "action in ('label','skip','split','other')", name="ck_label_action"
        ),
    )

    op.create_table(
        "resolved_labels",
        sa.Column(
            "container_id",
            sa.Integer,
            sa.ForeignKey("containers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("class_id", sa.Integer),
        sa.Column("method", sa.String, nullable=False, server_default="single"),
        sa.Column("resolved_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("is_gold", sa.Boolean, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "method in ('single','majority','adjudicated')", name="ck_resolved_method"
        ),
    )


def downgrade() -> None:
    op.drop_table("resolved_labels")
    op.drop_table("labels")
    op.drop_table("users")
    op.drop_index("ix_containers_geom", table_name="containers")
    op.drop_table("containers")
    op.drop_table("flights")
