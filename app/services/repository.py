"""
Article Repository — all database queries live here.
The API layer never writes raw SQL.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from app.models.orm import Article, ArticleTranslation
from app.models.schemas import StoryOut, StoryDetailOut, _compute_time_ago, _compute_read_minutes
from app.services.ingestion import RawArticle
from app.core.logging import get_logger

log = get_logger(__name__)


class ArticleRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Read ─────────────────────────────────────────────────

    async def get_top_stories(
        self,
        lang: str,
        category: str | None,
        page: int,
        per_page: int,
        max_age_hours: int = 48,
    ) -> tuple[list[Article], int]:
        """Returns (articles, total_count)."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        conditions = [
            Article.is_active == True,
            Article.is_summarized == True,
            Article.published_at >= cutoff,
        ]
        # FIX #4: category arrives lowercase from routes.py — matches DB storage
        if category and category != "all":
            conditions.append(Article.category == category)

        count_q = select(func.count()).select_from(Article).where(and_(*conditions))
        total = (await self._db.execute(count_q)).scalar_one()

        q = (
            select(Article)
            .options(selectinload(Article.translations))
            .where(and_(*conditions))
            .order_by(desc(Article.published_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        rows = (await self._db.execute(q)).scalars().all()
        return list(rows), total

    async def get_by_id(self, article_id: str) -> Article | None:
        q = (
            select(Article)
            .options(selectinload(Article.translations))
            .where(Article.id == article_id)
        )
        return (await self._db.execute(q)).scalar_one_or_none()

    async def search(
        self,
        query: str,
        country: str | None,
        category: str | None,
        lang: str,
        limit: int = 20,
    ) -> list[Article]:
        """
        Full-text search on title + summary.
        FIX #11: now searches both title_en AND summary_en (was title-only).
        """
        conditions = [
            Article.is_active == True,
            Article.is_summarized == True,
        ]

        if query:
            ts_query = func.plainto_tsquery("english", query)
            # FIX #11: search title AND summary, not just title
            title_vec   = func.to_tsvector("english", Article.title_en)
            summary_vec = func.to_tsvector("english", Article.summary_en)
            conditions.append(
                or_(
                    title_vec.op("@@")(ts_query),
                    summary_vec.op("@@")(ts_query),
                )
            )

        if country:
            conditions.append(func.lower(Article.country) == country.lower())
        # FIX #4: category already lowercase from routes.py
        if category and category != "all":
            conditions.append(Article.category == category)

        q = (
            select(Article)
            .options(selectinload(Article.translations))
            .where(and_(*conditions))
            .order_by(desc(Article.published_at))
            .limit(limit)
        )
        rows = (await self._db.execute(q)).scalars().all()
        return list(rows)

    async def get_existing_urls(self) -> set[str]:
        q = select(Article.original_url)
        rows = (await self._db.execute(q)).scalars().all()
        return set(rows)

    async def get_existing_hashes(self) -> set[str]:
        q = select(Article.content_hash).where(Article.content_hash.is_not(None))
        rows = (await self._db.execute(q)).scalars().all()
        return set(rows)

    async def get_unsummarized(self, limit: int = 50) -> list[Article]:
        q = (
            select(Article)
            .where(Article.is_summarized == False)
            .order_by(Article.fetched_at)
            .limit(limit)
        )
        return list((await self._db.execute(q)).scalars().all())

    async def get_translation(
        self, article_id: str, lang: str
    ) -> ArticleTranslation | None:
        q = select(ArticleTranslation).where(
            and_(
                ArticleTranslation.article_id == article_id,
                ArticleTranslation.language_code == lang,
            )
        )
        return (await self._db.execute(q)).scalar_one_or_none()

    # ── Write ────────────────────────────────────────────────

    async def create_from_raw(self, raw: RawArticle) -> Article:
        article = Article(
            title_en=raw.title,
            summary_en=raw.description,     # placeholder until summarized
            source_name=raw.source_name,
            source_url=raw.source_url,
            original_url=raw.url,
            image_url=raw.image_url,
            category=raw.category,          # already lowercase from ingestion
            country=raw.country,
            published_at=raw.published_at,
            fetched_at=datetime.now(timezone.utc),
            content_hash=raw.content_hash,
            is_summarized=False,
            is_breaking=False,
        )
        self._db.add(article)
        await self._db.flush()
        return article

    async def bulk_create_raw(self, raws: list[RawArticle]) -> list[Article]:
        """
        FIX #7: The original code called rollback() inside the loop, which
        wiped every previously flushed article when a single one failed.
        Fix: use a savepoint per article so only the failing row is rolled back.
        """
        articles = []
        for raw in raws:
            try:
                async with self._db.begin_nested():   # savepoint
                    a = await self.create_from_raw(raw)
                    articles.append(a)
            except IntegrityError:
                # Duplicate URL/hash — silently skip, savepoint rolled back
                log.debug("article_duplicate_skipped", url=raw.url)
            except Exception as e:
                log.warning("article_insert_failed", url=raw.url, error=str(e))
        await self._db.commit()
        return articles

    async def update_summary(
        self,
        article: Article,
        sentence_1: str,
        sentence_2: str,
        sentence_3: str,
        category: str | None = None,
        is_breaking: bool = False,
    ) -> Article:
        full = f"{sentence_1} {sentence_2} {sentence_3}".strip()
        article.summary_en = full
        if category:
            article.category = category   # already lowercase from summarizer
        article.is_summarized = True
        article.is_breaking = is_breaking
        self._db.add(article)
        await self._db.flush()
        return article

    async def upsert_translation(
        self,
        article_id: str,
        lang: str,
        title: str,
        summary: str,
        provider: str,
    ) -> ArticleTranslation:
        existing = await self.get_translation(article_id, lang)
        if existing:
            existing.title = title
            existing.summary = summary
            existing.translation_provider = provider
            self._db.add(existing)
            return existing

        translation = ArticleTranslation(
            article_id=article_id,
            language_code=lang,
            title=title,
            summary=summary,
            translation_provider=provider,
        )
        self._db.add(translation)
        await self._db.flush()
        return translation

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def localize(article: Article, lang: str) -> dict:
        """
        Return article fields in the requested language.
        Falls back to English if translation doesn't exist yet.
        Fields named to match Flutter's Story.fromJson() exactly.
        """
        read_minutes = _compute_read_minutes(article.full_content_en, article.summary_en)
        time_ago = _compute_time_ago(article.published_at)

        base = {
            "id": article.id,
            "source": article.source_name,           # Flutter reads "source"
            "original_url": article.original_url,
            "image_url": article.image_url,
            "category": article.category.capitalize(), # Flutter displays "World" not "world"
            "region": article.country,               # Flutter reads "region"
            "published_at": article.published_at,
            "time_ago": time_ago,                    # Flutter reads "time_ago"
            "read_minutes": read_minutes,            # Flutter reads "read_minutes"
            "is_breaking": article.is_breaking,      # Flutter reads "is_breaking"
        }

        if lang == "en" or not article.translations:
            return {**base, "title": article.title_en, "summary": article.summary_en, "language_code": "en"}

        translation = next(
            (t for t in article.translations if t.language_code == lang),
            None,
        )
        if translation:
            return {**base, "title": translation.title, "summary": translation.summary, "language_code": lang}

        # Fallback to English
        return {**base, "title": article.title_en, "summary": article.summary_en, "language_code": "en"}
