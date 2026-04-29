"""
Celery Workers — background task pipeline

POOL: solo (--pool=solo --concurrency=1)
  prefork forks child processes. asyncio event loops, Semaphores, and
  asyncio.gather() deadlock silently inside forked processes — tasks are
  received but never execute, and the same task re-queues indefinitely.
  solo runs everything in one process: no forking, asyncio works correctly.
  Internal asyncio.gather() concurrency still works fine within each task.

BATCH: 10 articles per summarize run (runs every 15 min → 40/hour)
CONCURRENCY: 3 articles processed simultaneously inside each task
PER-ARTICLE TIMEOUT: 45s for summarization, 20s per translation
"""
import asyncio
from datetime import datetime, timedelta, timezone
from celery import Celery
from celery.schedules import crontab
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

settings = get_settings()
log = get_logger(__name__)

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
    # FIX: removed task_acks_late=True — with solo pool and no forking,
    # late acks only cause duplicate task delivery when tasks take > broker timeout
    task_acks_late=False,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=settings.celery_broker_url,
    beat_schedule={
        "fetch-news-every-4h": {
            "task": "app.workers.tasks.fetch_and_process_news",
            "schedule": settings.news_fetch_interval_minutes * 60,
        },
        "summarize-pending-every-15m": {
            "task": "app.workers.tasks.summarize_pending",
            "schedule": 900,
        },
        "cleanup-daily": {
            "task": "app.workers.tasks.cleanup_old_articles",
            "schedule": crontab(hour=3, minute=0),
        },
    },
)


def run_async(coro):
    """
    Run an async coroutine from a sync Celery task.
    Safe with --pool=solo because there is no forking.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _get_summarizer():
    if settings.use_bedrock:
        from app.services.bedrock_summarizer import BedrockSummarizationService
        log.info("ai_provider", provider="bedrock", model=settings.bedrock_model_id)
        return BedrockSummarizationService()
    from app.services.summarizer import SummarizationService
    log.info("ai_provider", provider="claude", model=settings.claude_model)
    return SummarizationService()


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
    return (
        any(kw in title.lower() for kw in _BREAKING_KEYWORDS)
        or source_name in _BREAKING_SOURCES
    )


# ── Tasks ─────────────────────────────────────────────────────

@celery_app.task(
    name="app.workers.tasks.fetch_and_process_news",
    bind=True, max_retries=3, default_retry_delay=120,
    soft_time_limit=300, time_limit=360,
)
def fetch_and_process_news(self):
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
    soft_time_limit=480,
    time_limit=540,
)
def summarize_pending(self):
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
    setup_logging()
    result = run_async(_cleanup())
    log.info("cleanup_complete", **result)
    return result


# ── Async implementations ─────────────────────────────────────

async def _fetch_and_process() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.services.ingestion import RSSFetcher, NewsAPIFetcher, Deduplicator
    from app.services.repository import ArticleRepository
    from app.models.orm import FetchLog

    start = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        repo = ArticleRepository(db)

        rss_articles = await RSSFetcher().fetch_all()
        api_articles = await NewsAPIFetcher().fetch_top_headlines()
        all_raw      = rss_articles + api_articles
        log.info("raw_fetched", rss=len(rss_articles), api=len(api_articles))

        existing_urls   = await repo.get_existing_urls()
        existing_hashes = await repo.get_existing_hashes()
        unique, duped   = Deduplicator(existing_hashes, existing_urls).filter(all_raw)
        saved           = await repo.bulk_create_raw(unique)

        db.add(FetchLog(
            source="all",
            articles_fetched=len(all_raw),
            articles_new=len(saved),
            articles_duped=duped,
            duration_seconds=(datetime.now(timezone.utc) - start).total_seconds(),
        ))
        await db.commit()
        return {"fetched": len(all_raw), "new": len(saved), "duped": duped}


async def _summarize_pending() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.core.cache import get_redis, CacheClient
    from app.services.repository import ArticleRepository
    from app.services.translator import TranslationService

    BATCH_SIZE     = 10
    CONCURRENCY    = 3
    PRIORITY_LANGS = ["ar", "fr", "es", "pt", "sw", "hi", "zh", "id", "th", "vi"]

    summarizer = _get_summarizer()
    translator = TranslationService()
    summarized = 0
    translated = 0
    sem        = asyncio.Semaphore(CONCURRENCY)

    async def process_one(article) -> tuple[bool, int]:
        async with sem:
            try:
                content = article.full_content_en or article.summary_en or article.title_en

                summary = await asyncio.wait_for(
                    summarizer.summarize(article.title_en, content), timeout=45
                )
                category = await asyncio.wait_for(
                    summarizer.categorize(article.title_en, article.summary_en), timeout=15
                )
                is_breaking = _detect_breaking(
                    article.title_en, article.source_name, article.published_at
                )

                # Use a fresh DB session per article to avoid shared-state issues
                async with AsyncSessionLocal() as db:
                    repo = ArticleRepository(db)
                    await repo.update_summary(
                        article, summary.sentence_1, summary.sentence_2,
                        summary.sentence_3, category, is_breaking,
                    )
                    t_count = 0
                    for lang in PRIORITY_LANGS:
                        if await repo.get_translation(article.id, lang):
                            continue
                        try:
                            t_title, t_summary = await asyncio.wait_for(
                                translator.translate_pair(article.title_en, summary.full, lang),
                                timeout=20,
                            )
                            await repo.upsert_translation(
                                article.id, lang, t_title, t_summary,
                                TranslationService.provider_name(lang),
                            )
                            t_count += 1
                        except asyncio.TimeoutError:
                            log.warning("translation_timeout", lang=lang, id=article.id)
                        except Exception as e:
                            log.warning("translation_failed", lang=lang, error=str(e))
                    await db.commit()

                log.info("article_summarized",
                         id=article.id, category=category, breaking=is_breaking)
                return True, t_count

            except asyncio.TimeoutError:
                log.warning("article_timeout", id=article.id)
                return False, 0
            except Exception as e:
                log.error("article_failed", id=article.id, error=str(e))
                return False, 0

    # Fetch pending articles
    async with AsyncSessionLocal() as db:
        repo     = ArticleRepository(db)
        articles = await repo.get_unsummarized(limit=BATCH_SIZE)

    if not articles:
        log.info("no_pending_articles")
        return {"summarized": 0, "translated": 0}

    log.info("summarize_batch_start", count=len(articles))

    results = await asyncio.gather(*[process_one(a) for a in articles])

    for ok, t_count in results:
        if ok:
            summarized += 1
            translated += t_count

    # Bust story cache
    try:
        redis   = await get_redis()
        deleted = await CacheClient(redis).delete_pattern("stories:*")
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
    return {"deactivated": result.rowcount}
