"""
Add subscribers table.
Revision ID: 003_add_subscribers
"""
from alembic import op
import sqlalchemy as sa

revision = "003_add_subscribers"
down_revision = "002_add_is_breaking"


def upgrade() -> None:
    op.create_table(
        "subscribers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email", name="uq_subscriber_email"),
    )
    op.create_index("ix_subscribers_email", "subscribers", ["email"])
    op.create_index("ix_subscribers_active", "subscribers", ["is_active"])


def downgrade() -> None:
    op.drop_table("subscribers")
