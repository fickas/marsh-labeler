"""add flights.selection_params

Stores the abstain-rule values + one real example pixel captured by the
production notebook, so the "Why this tile?" panel renders from the actual run.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("flights", sa.Column("selection_params", JSONB, nullable=True))


def downgrade():
    op.drop_column("flights", "selection_params")
