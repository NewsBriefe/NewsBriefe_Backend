"""
Add is_breaking column to articles table.

Revision ID: 002_add_is_breaking
Previous:    001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "002_add_is_breaking"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add is_breaking column — defaults to False for all existing rows
    op.add_column(
        "articles",
        sa.Column("is_breaking", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index("ix_articles_breaking", "articles", ["is_breaking", "published_at"])

    # Backfill: mark recent articles from wire services as potentially breaking
    # (conservative — only articles with "breaking" in title and < 3h old)
    op.execute("""
        UPDATE articles
        SET is_breaking = true
        WHERE
            is_active = true
            AND published_at >= NOW() - INTERVAL '3 hours'
            AND LOWER(title_en) LIKE '%breaking%'
    """)


def downgrade() -> None:
    op.drop_index("ix_articles_breaking", table_name="articles")
    op.drop_column("articles", "is_breaking")
