"""resolved_labels.out_of_scope -- retire a superpixel as out of scope

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-18

Adds a boolean `out_of_scope` to resolved_labels. A row with out_of_scope=True
(and class_id NULL) is a permanent "this superpixel isn't part of the study"
decision from the labeler: it retires the superpixel for every user and every
future round (the queue treats class_id-set OR out_of_scope as retired), but it
is not a class, so it stays out of the ground-truth layer and the training mask.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resolved_labels",
        sa.Column("out_of_scope", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("resolved_labels", "out_of_scope")
