"""resolved_labels.created_at -- a stable verdict-birth timestamp

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-17

Why:

`resolved_labels` is the training fact: the gold verdict per (flight, superpixel)
that rasterizes into the training mask. To retrain on "the labels as of time T"
you need to ask which verdicts EXISTED at T. The table already had `updated_at`,
but that column carries onupdate=func.now(), so it moves every time a verdict is
re-touched (adjudication, method change). A "verdicts as of T" filter on
`updated_at` would silently drop any verdict resolved before T but adjudicated
after it. This adds a `created_at` that is set once on insert and never moves --
the correct snapshot key.

`labels.created_at` already exists (since 0001) and is left untouched.

Existing rows are backfilled from `updated_at`: for a verdict that has never been
re-touched, updated_at == its birth time exactly; for one that has, updated_at is
the closest available approximation (we have no earlier record). New rows get
their real insert time from the server default.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add with a server default so the column is non-null going forward and the
    # app's INSERTs get a real birth timestamp without naming the column.
    op.add_column(
        "resolved_labels",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    # Backfill historical verdicts: their best available birth time is updated_at.
    # (The add_column default stamped them with migration-run time; override it.)
    op.execute("UPDATE resolved_labels SET created_at = updated_at")


def downgrade() -> None:
    op.drop_column("resolved_labels", "created_at")
