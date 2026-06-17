"""projects, rounds, and superpixel-keyed labels/verdicts

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17

What this does, and why:

- Adds `projects` (a study site grouping flights) and `flights.project_id`.
- Adds rounds: `containers.round` and `flights.active_round`. A container is now a
  per-round question; the queue serves the flight's active round.
- Re-keys the durable answers off the ephemeral container and onto the superpixel:
  `labels` and `resolved_labels` lose `container_id` and gain
  (`flight_id`, `superpixel_id`). Labels also carry the round + contested pair they
  were answered under (audit that survives container churn). Existing rows are
  backfilled from their container BEFORE the link is dropped, so no answers are
  lost. After this, replacing a round's containers never touches answers.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- projects -----------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("notes", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column(
        "flights",
        sa.Column(
            "project_id",
            sa.Integer,
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_flights_project_id", "flights", ["project_id"])
    op.add_column(
        "flights",
        sa.Column("active_round", sa.Integer, nullable=False, server_default="1"),
    )

    # --- rounds on containers ----------------------------------------------
    op.add_column(
        "containers",
        sa.Column("round", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_index("ix_containers_round", "containers", ["round"])
    op.create_index("ix_containers_superpixel_id", "containers", ["superpixel_id"])
    # a superpixel may now recur across rounds; uniqueness includes the round.
    op.drop_constraint("uq_container_flight_sp", "containers", type_="unique")
    op.create_unique_constraint(
        "uq_container_flight_round_sp",
        "containers",
        ["flight_id", "round", "superpixel_id"],
    )

    # --- labels: re-key to the superpixel ----------------------------------
    op.add_column("labels", sa.Column("flight_id", sa.Integer, nullable=True))
    op.add_column("labels", sa.Column("superpixel_id", sa.Integer, nullable=True))
    op.add_column(
        "labels", sa.Column("round", sa.Integer, nullable=False, server_default="1")
    )
    op.add_column("labels", sa.Column("pair_code", sa.Integer, nullable=True))
    op.add_column("labels", sa.Column("class_a", sa.Integer, nullable=True))
    op.add_column("labels", sa.Column("class_b", sa.Integer, nullable=True))
    # backfill from the container the label was made against (still linked here).
    op.execute(
        """
        UPDATE labels l SET
            flight_id     = c.flight_id,
            superpixel_id = c.superpixel_id,
            round         = c.round,
            pair_code     = c.pair_code,
            class_a       = c.class_a,
            class_b       = c.class_b
        FROM containers c
        WHERE l.container_id = c.id
        """
    )
    op.alter_column("labels", "flight_id", nullable=False)
    op.alter_column("labels", "superpixel_id", nullable=False)
    # drop the old per-container uniqueness, then the link column (its FK to
    # containers drops with it).
    op.drop_constraint("uq_label_container_user", "labels", type_="unique")
    op.drop_column("labels", "container_id")
    op.create_foreign_key(
        "fk_labels_flight", "labels", "flights", ["flight_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_labels_flight_id", "labels", ["flight_id"])
    op.create_index("ix_labels_superpixel_id", "labels", ["superpixel_id"])
    op.create_unique_constraint(
        "uq_label_flight_sp_user", "labels", ["flight_id", "superpixel_id", "user_id"]
    )

    # --- resolved_labels: re-key to (flight, superpixel) -------------------
    op.add_column("resolved_labels", sa.Column("flight_id", sa.Integer, nullable=True))
    op.add_column(
        "resolved_labels", sa.Column("superpixel_id", sa.Integer, nullable=True)
    )
    op.execute(
        """
        UPDATE resolved_labels r SET
            flight_id     = c.flight_id,
            superpixel_id = c.superpixel_id
        FROM containers c
        WHERE r.container_id = c.id
        """
    )
    op.alter_column("resolved_labels", "flight_id", nullable=False)
    op.alter_column("resolved_labels", "superpixel_id", nullable=False)
    op.drop_constraint("resolved_labels_pkey", "resolved_labels", type_="primary")
    op.drop_column("resolved_labels", "container_id")  # drops its FK too
    op.create_primary_key(
        "resolved_labels_pkey", "resolved_labels", ["flight_id", "superpixel_id"]
    )
    op.create_foreign_key(
        "fk_resolved_flight",
        "resolved_labels",
        "flights",
        ["flight_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Best-effort reverse for a dev database. Re-links answers to a container by
    # (flight, superpixel), choosing the lowest-id container when several rounds
    # exist. Loses the denormalized round/pair audit columns on labels.
    op.drop_constraint("fk_resolved_flight", "resolved_labels", type_="foreignkey")
    op.drop_constraint("resolved_labels_pkey", "resolved_labels", type_="primary")
    op.add_column("resolved_labels", sa.Column("container_id", sa.Integer, nullable=True))
    op.execute(
        """
        UPDATE resolved_labels r SET container_id = c.id
        FROM (
            SELECT flight_id, superpixel_id, MIN(id) AS id
            FROM containers GROUP BY flight_id, superpixel_id
        ) c
        WHERE r.flight_id = c.flight_id AND r.superpixel_id = c.superpixel_id
        """
    )
    op.alter_column("resolved_labels", "container_id", nullable=False)
    op.create_primary_key("resolved_labels_pkey", "resolved_labels", ["container_id"])
    op.create_foreign_key(
        None, "resolved_labels", "containers", ["container_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_column("resolved_labels", "superpixel_id")
    op.drop_column("resolved_labels", "flight_id")

    op.drop_constraint("uq_label_flight_sp_user", "labels", type_="unique")
    op.drop_index("ix_labels_superpixel_id", table_name="labels")
    op.drop_index("ix_labels_flight_id", table_name="labels")
    op.drop_constraint("fk_labels_flight", "labels", type_="foreignkey")
    op.add_column("labels", sa.Column("container_id", sa.Integer, nullable=True))
    op.execute(
        """
        UPDATE labels l SET container_id = c.id
        FROM (
            SELECT flight_id, superpixel_id, MIN(id) AS id
            FROM containers GROUP BY flight_id, superpixel_id
        ) c
        WHERE l.flight_id = c.flight_id AND l.superpixel_id = c.superpixel_id
        """
    )
    op.alter_column("labels", "container_id", nullable=False)
    op.create_foreign_key(
        None, "labels", "containers", ["container_id"], ["id"], ondelete="CASCADE"
    )
    op.create_unique_constraint(
        "uq_label_container_user", "labels", ["container_id", "user_id"]
    )
    for col in ("class_b", "class_a", "pair_code", "round", "superpixel_id", "flight_id"):
        op.drop_column("labels", col)

    op.drop_constraint("uq_container_flight_round_sp", "containers", type_="unique")
    op.create_unique_constraint(
        "uq_container_flight_sp", "containers", ["flight_id", "superpixel_id"]
    )
    op.drop_index("ix_containers_superpixel_id", table_name="containers")
    op.drop_index("ix_containers_round", table_name="containers")
    op.drop_column("containers", "round")

    op.drop_column("flights", "active_round")
    op.drop_index("ix_flights_project_id", table_name="flights")
    op.drop_column("flights", "project_id")
    op.drop_table("projects")
