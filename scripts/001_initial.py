"""initial schema

Revision ID: 001_initial
Create Date: 2025-04-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── articles ─────────────────────────────────────────────
    op.create_table(
        "articles",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("source_url", sa.String(512), nullable=False),
        sa.Column("original_url", sa.String(1024), nullable=False, unique=True),
        sa.Column("image_url", sa.String(1024), nullable=True),
        sa.Column("title_en", sa.String(512), nullable=False),
        sa.Column("summary_en", sa.Text, nullable=False),
        sa.Column("full_content_en", sa.Text, nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("language_detected", sa.String(8), server_default="en"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_summarized", sa.Boolean, server_default="false"),
        sa.Column("is_active", sa.Boolean, server_default="true"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("tfidf_vector", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_articles_category", "articles", ["category"])
    op.create_index("ix_articles_country", "articles", ["country"])
    op.create_index("ix_articles_published_at", "articles", ["published_at"])
    op.create_index("ix_articles_is_active", "articles", ["is_active"])
    op.create_index("ix_articles_is_summarized", "articles", ["is_summarized"])
    op.create_index("ix_articles_content_hash", "articles", ["content_hash"])
    op.create_index("ix_articles_category_published", "articles", ["category", "published_at"])
    op.create_index("ix_articles_active_published", "articles", ["is_active", "published_at"])

    # Full-text search index (PostgreSQL only)
    op.execute(
        "CREATE INDEX ix_articles_title_fts ON articles "
        "USING gin(to_tsvector('english', title_en))"
    )

    # ── article_translations ──────────────────────────────────
    op.create_table(
        "article_translations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("article_id", UUID(as_uuid=False),
                  sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language_code", sa.String(8), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("translation_provider", sa.String(32), server_default="deepl"),
        sa.Column("quality_score", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("article_id", "language_code", name="uq_article_lang"),
    )
    op.create_index("ix_translations_article_lang", "article_translations",
                    ["article_id", "language_code"])

    # ── fetch_logs ────────────────────────────────────────────
    op.create_table(
        "fetch_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("articles_fetched", sa.Integer, server_default="0"),
        sa.Column("articles_new", sa.Integer, server_default="0"),
        sa.Column("articles_duped", sa.Integer, server_default="0"),
        sa.Column("duration_seconds", sa.Float, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("success", sa.Boolean, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("fetch_logs")
    op.drop_table("article_translations")
    op.execute("DROP INDEX IF EXISTS ix_articles_title_fts")
    op.drop_table("articles")
