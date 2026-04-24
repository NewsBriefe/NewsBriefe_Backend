"""
Database ORM models.
Separate from Pydantic schemas — ORM models are the DB layer,
Pydantic schemas are the API contract layer.
"""
from typing import Dict
import uuid
from datetime import datetime
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from pydantic import BaseModel, Field, field_validator


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    # ── Source metadata ──────────────────────────────────
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    original_url: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    image_url: Mapped[str | None] = mapped_column(String(1024))

    # ── Content (base language = English) ────────────────
    title_en: Mapped[str] = mapped_column(String(512), nullable=False)
    summary_en: Mapped[str] = mapped_column(Text, nullable=False)  # 3-sentence AI summary
    full_content_en: Mapped[str | None] = mapped_column(Text)

    # ── Classification ───────────────────────────────────
    # FIX #4: category is stored lowercase ("world", "tech") —
    # routes.py normalises the incoming Flutter value before querying.
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    country: Mapped[str | None] = mapped_column(String(64), index=True)
    language_detected: Mapped[str] = mapped_column(String(8), default="en")

    # FIX #14 / #6: is_breaking column — flagged by the worker when
    # the article is < 3 hours old and matches breaking heuristics.
    is_breaking: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # ── Timestamps ────────────────────────────────────────
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ── Processing status ────────────────────────────────
    is_summarized: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # ── Deduplication ────────────────────────────────────
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    tfidf_vector: Mapped[str | None] = mapped_column(Text)  # JSON-serialized

    # ── Relations ────────────────────────────────────────
    translations: Mapped[list["ArticleTranslation"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_articles_category_published", "category", "published_at"),
        Index("ix_articles_country_published", "country", "published_at"),
        Index("ix_articles_active_published", "is_active", "published_at"),
        Index("ix_articles_breaking", "is_breaking", "published_at"),
    )

    def __repr__(self) -> str:
        return f"<Article id={self.id} title={self.title_en[:40]!r}>"


class ArticleTranslation(Base):
    __tablename__ = "article_translations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    article_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)

    # ── Translated content ───────────────────────────────
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Quality ──────────────────────────────────────────
    translation_provider: Mapped[str] = mapped_column(String(32), default="deepl")
    quality_score: Mapped[float | None] = mapped_column(Float)

    # ── Relation ─────────────────────────────────────────
    article: Mapped["Article"] = relationship(back_populates="translations")

    __table_args__ = (
        UniqueConstraint("article_id", "language_code", name="uq_article_lang"),
        Index("ix_translations_article_lang", "article_id", "language_code"),
    )

# ============================================================================
# Health & Status
# ============================================================================

class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field(default="healthy")
    version: str = Field(default="1.0.0")


class FetchLog(Base):
    __tablename__ = "fetch_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    articles_fetched: Mapped[int] = mapped_column(default=0)
    articles_new: Mapped[int] = mapped_column(default=0)
    articles_duped: Mapped[int] = mapped_column(default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
