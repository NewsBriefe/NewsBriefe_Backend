"""
Celery Workers — background task pipeline

Schedule:
  • fetch_and_process_news   → every 4 hours (configurable)
  • summarize_pending        → every 15 minutes
  • cleanup_old_articles     → daily at 03:00 UTC

Flow:
  fetch_and_process_news
    └─ RSSFetcher.fetch_all() + NewsAPIFetcher.fetch_top_headlines()
    └─ Deduplicator.filter()
    └─ ArticleRepository.bulk_create_raw()
    └─ [triggers] summarize_pending

  summarize_pending
    └─ ArticleRepository.get_unsummarized(limit=50)
    └─ SummarizationService.summarize() for each
    └─ SummarizationService.categorize() for each
    └─ _detect_breaking() — flag is_breaking
    └─ ArticleRepository.update_summary()
    └─ [for top languages] TranslationService.translate_pair()
    └─ ArticleRepository.upsert_translation()
    └─ commit after each article  (FIX #20)
    └─ CacheClient.delete_pattern("stories:*")   ← invalidate cache
"""
import asyncio
from datetime import datetime, timedelta, timezone
from celery import Celery
from celery.schedules import crontab
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

settings = get_settings()
log = get_logger(__name__)

# ── Celery app ───────────────────────────────────────────────
celery_app = Celery(
    "newsbrief",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # FIX: suppress CPendingDeprecationWarning in Celery 5.x
    broker_connection_retry_on_startup=True,
    # FIX: correct module name is 'redbeat' not 'celery_redbeat'
    # The PyPI package is 'celery-redbeat' but the Python module is 'redbeat'
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=settings.celery_broker_url,
    beat_schedule={
        "fetch-news-every-4h": {
            "task": "app.workers.tasks.fetch_and_process_news",
            "schedule": settings.news_fetch_interval_minutes * 60,
        },
        "summarize-pending-every-15m": {
            "task": "app.workers.tasks.summarize_pending",
            "schedule": 900,  # 15 minutes
        },
        "cleanup-daily": {
            "task": "app.workers.tasks.cleanup_old_articles",
            "schedule": crontab(hour=3, minute=0),
        },
    },
)


def run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Breaking news detection ───────────────────────────────────
_BREAKING_KEYWORDS = frozenset({
    "breaking", "urgent", "alert", "flash:", "just in",
    "developing", "emergency", "exclusive", "crisis",
})
_BREAKING_SOURCES = frozenset({
    "Reuters", "AP News", "Bloomberg", "BBC", "Al Jazeera",
    "AFP", "Associated Press",
})


def _detect_breaking(title: str, source_name: str, published_at: datetime) -> bool:
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_hours = (now - published_at).total_seconds() / 3600
    if age_hours > 3:
        return False
    title_lower = title.lower()
    has_keyword = any(kw in title_lower for kw in _BREAKING_KEYWORDS)
    is_wire     = source_name in _BREAKING_SOURCES
    return has_keyword or is_wire


# ── Tasks ────────────────────────────────────────────────────

@celery_app.task(
    name="app.workers.tasks.fetch_and_process_news",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    soft_time_limit=300,
    time_limit=360,
)
def fetch_and_process_news(self):
    """Fetch from all sources, deduplicate, store."""
    setup_logging()
    try:
        result = run_async(_fetch_and_process())
        log.info("fetch_complete", **result)
        summarize_pending.delay()
        return result
    except Exception as exc:
        log.error("fetch_failed", error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(
    name="app.workers.tasks.summarize_pending",
    bind=True,
    max_retries=2,
    soft_time_limit=600,
    time_limit=660,
)
def summarize_pending(self):
    """Summarize unsummarized articles + pre-translate top languages."""
    setup_logging()
    try:
        result = run_async(_summarize_pending())
        log.info("summarization_complete", **result)
        return result
    except Exception as exc:
        log.error("summarization_task_failed", error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(name="app.workers.tasks.cleanup_old_articles")
def cleanup_old_articles():
    """Soft-delete articles older than 7 days."""
    setup_logging()
    result = run_async(_cleanup())
    log.info("cleanup_complete", **result)
    return result


# ── Async implementations ────────────────────────────────────

async def _fetch_and_process() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.services.ingestion import RSSFetcher, NewsAPIFetcher, Deduplicator
    from app.services.repository import ArticleRepository
    from app.models.orm import FetchLog

    start = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        repo = ArticleRepository(db)

        rss = RSSFetcher()
        newsapi = NewsAPIFetcher()

        rss_articles = await rss.fetch_all()
        api_articles = await newsapi.fetch_top_headlines()
        all_raw = rss_articles + api_articles

        log.info("raw_fetched", rss=len(rss_articles), api=len(api_articles))

        existing_urls = await repo.get_existing_urls()
        existing_hashes = await repo.get_existing_hashes()
        dedup = Deduplicator(existing_hashes, existing_urls)
        unique, duped_count = dedup.filter(all_raw)

        saved = await repo.bulk_create_raw(unique)

        fetch_log = FetchLog(
            source="all",
            articles_fetched=len(all_raw),
            articles_new=len(saved),
            articles_duped=duped_count,
            duration_seconds=(datetime.now(timezone.utc) - start).total_seconds(),
        )
        db.add(fetch_log)
        await db.commit()

        return {
            "fetched": len(all_raw),
            "new": len(saved),
            "duped": duped_count,
        }


async def _summarize_pending() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.core.cache import get_redis, CacheClient
    from app.services.repository import ArticleRepository
    from app.services.summarizer import SummarizationService
    from app.services.translator import TranslationService

    PRIORITY_LANGS = ["ar", "fr", "es", "pt", "sw", "hi", "zh", "id", "th", "vi"]

    summarizer = SummarizationService()
    translator = TranslationService()
    summarized = 0
    translated = 0

    async with AsyncSessionLocal() as db:
        repo = ArticleRepository(db)
        articles = await repo.get_unsummarized(limit=50)

        for article in articles:
            try:
                content = article.full_content_en or article.summary_en or article.title_en
                summary = await summarizer.summarize(article.title_en, content)
                category = await summarizer.categorize(article.title_en, article.summary_en)
                is_breaking = _detect_breaking(
                    article.title_en,
                    article.source_name,
                    article.published_at,
                )

                await repo.update_summary(
                    article,
                    sentence_1=summary.sentence_1,
                    sentence_2=summary.sentence_2,
                    sentence_3=summary.sentence_3,
                    category=category,
                    is_breaking=is_breaking,
                )
                summarized += 1

                for lang in PRIORITY_LANGS:
                    existing = await repo.get_translation(article.id, lang)
                    if existing:
                        continue
                    try:
                        t_title, t_summary = await translator.translate_pair(
                            article.title_en,
                            summary.full,
                            lang,
                        )
                        await repo.upsert_translation(
                            article.id, lang,
                            t_title, t_summary,
                            TranslationService.provider_name(lang),
                        )
                        translated += 1
                    except Exception as e:
                        log.warning("pre_translate_failed", lang=lang, error=str(e))

                # Commit after each article so a timeout can't wipe the batch
                await db.commit()

            except Exception as e:
                log.error("summarize_article_failed", id=article.id, error=str(e))
                await db.rollback()
                continue

    try:
        redis = await get_redis()
        cache = CacheClient(redis)
        deleted = await cache.delete_pattern("stories:*")
        log.info("cache_invalidated", keys_deleted=deleted)
    except Exception as e:
        log.warning("cache_invalidate_failed", error=str(e))

    return {"summarized": summarized, "translated": translated}


async def _cleanup() -> dict:
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update
    from app.models.orm import Article

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Article)
            .where(Article.published_at < cutoff, Article.is_active == True)
            .values(is_active=False)
        )
        await db.commit()
        count = result.rowcount

    return {"deactivated": count}
