"""
Celery Workers — optimized summarization pipeline

OPTIMIZATIONS ACTIVE:
  A1. Input truncated to 400 words in bedrock_summarizer.py
  A2. Heuristic categorization first — Bedrock only when uncertain
  A3. Skip stubs (< 80 words) — mark summarized using raw description
  A4. Translation on demand only — NO pre-translation
  B1. Title similarity dedup, two stages:
      Stage 1 — Jaccard word-overlap (free, no model, runs always)
      Stage 2 — fastembed semantic similarity (catches different
                wording for the same event — only runs when Stage 1
                finds no match, and only if semantic_dedup_enabled)
  B2. Summary hash cache — reuse summary if same content seen before
  C1. Article ranking — score by recency + source quality + breaking flag
  C2. Top-N selection — summarize only top 30 per run
  C3. Daily budget cap — hard limit on Bedrock calls per day
"""
import asyncio
import hashlib
import json
import re
import ssl
from datetime import datetime, timedelta, timezone
from celery import Celery
from celery.schedules import crontab
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

settings = get_settings()
log = get_logger(__name__)

# ── Celery setup ─────────────────────────────────────────────

def _celery_ssl() -> dict:
    if settings.celery_broker_url.startswith("rediss://"):
        return {"ssl_cert_reqs": ssl.CERT_NONE, "ssl_ca_certs": None,
                 "ssl_certfile": None, "ssl_keyfile": None}
    return {}

_ssl = _celery_ssl()

celery_app = Celery("newsbrief", broker=settings.celery_broker_url, backend=settings.celery_result_backend)
celery_app.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    timezone="UTC", enable_utc=True, task_track_started=True,
    task_acks_late=True, worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=settings.celery_broker_url,
    broker_transport_options={"ssl": _ssl} if _ssl else {},
    redis_backend_use_ssl=_ssl if _ssl else None,
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_summarizer():
    if settings.use_bedrock:
        from app.services.bedrock_summarizer import BedrockSummarizationService
        log.info("ai_provider", provider="bedrock", model=settings.bedrock_model_id)
        return BedrockSummarizationService()
    from app.services.summarizer import SummarizationService
    log.info("ai_provider", provider="claude", model=settings.claude_model)
    return SummarizationService()


# ── Breaking news detection ───────────────────────────────────

_BREAKING_KW  = frozenset(["breaking","urgent","alert","flash:","just in","developing","emergency","crisis"])
_BREAKING_SRC = frozenset(["Reuters","AP News","Bloomberg","BBC","Al Jazeera","AFP","Associated Press"])

def _detect_breaking(title: str, source: str, published_at: datetime) -> bool:
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    if (now - published_at).total_seconds() / 3600 > 3:
        return False
    return any(kw in title.lower() for kw in _BREAKING_KW) or source in _BREAKING_SRC


# ── C1: Article ranking ───────────────────────────────────────

_SOURCE_SCORES = {
    "Reuters": 10, "AP News": 10, "Associated Press": 10, "AFP": 10,
    "BBC": 9, "Al Jazeera": 9, "Bloomberg": 9,
    "NYT Science": 8, "NYT Business": 8, "NYT Health": 8, "NYT Arts": 8,
    "WHO": 8, "ScienceDaily": 7, "The Verge": 7, "Ars Technica": 7,
    "ESPN": 6, "BBC Sport": 6, "Inside Climate News": 6, "Grist": 6,
}

def _rank_article(article) -> float:
    now = datetime.now(timezone.utc)
    pub = article.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age_hours = max(0, (now - pub).total_seconds() / 3600)
    recency   = max(0.0, 40.0 - (age_hours * 40.0 / 48.0))
    source    = _SOURCE_SCORES.get(article.source_name, 5) * 2
    breaking  = 20.0 if article.is_breaking else 0.0
    return recency + source + breaking


# ── B1 Stage 1: Jaccard title similarity (free, no model) ────

_STOPWORDS = {"the","a","an","in","on","at","to","for","of","and","or","is","are",
              "was","were","by","with","as","its","it","this","that","from","has","have"}

def _title_similarity(t1: str, t2: str) -> float:
    def words(t):
        return {w.lower() for w in re.findall(r'\b\w+\b', t)
                if w.lower() not in _STOPWORDS and len(w) > 2}
    w1, w2 = words(t1), words(t2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


# ── B1 Stage 2: semantic embeddings (catches different wording) ──

async def _batch_get_embeddings(redis, texts: list[str]) -> dict[str, list[float]]:
    """
    Fetch cached embeddings for a list of titles via one Redis mget,
    compute only the missing ones in a single fastembed batch call,
    then pipeline them back to Redis. Avoids N+1 round trips and
    avoids re-embedding the same title across runs.
    """
    if not texts:
        return {}
    unique_texts = list(dict.fromkeys(texts))  # dedupe, preserve order
    keys = [f"embedding:{hashlib.sha256(t.encode()).hexdigest()[:16]}" for t in unique_texts]

    cached = await redis.mget(keys)
    result: dict[str, list[float]] = {}
    missing_texts, missing_keys = [], []
    for t, k, c in zip(unique_texts, keys, cached):
        if c:
            result[t] = json.loads(c)
        else:
            missing_texts.append(t)
            missing_keys.append(k)

    if missing_texts:
        from app.services.embeddings import EmbeddingService
        embeddings = EmbeddingService.embed_batch(missing_texts)
        pipe = redis.pipeline()
        for t, k, emb in zip(missing_texts, missing_keys, embeddings):
            result[t] = emb
            pipe.setex(k, 86400, json.dumps(emb))  # 24h TTL
        await pipe.execute()

    return result


def _best_semantic_match(
    title: str, recent_titles: list[str], embeddings_map: dict[str, list[float]]
) -> tuple[str | None, float]:
    from app.services.embeddings import EmbeddingService
    new_emb = embeddings_map.get(title)
    if not new_emb:
        return None, 0.0
    best_title, best_sim = None, 0.0
    for rt in recent_titles:
        rt_emb = embeddings_map.get(rt)
        if not rt_emb:
            continue
        sim = EmbeddingService.cosine_similarity(new_emb, rt_emb)
        if sim > best_sim:
            best_sim, best_title = sim, rt
    return best_title, best_sim


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
    bind=True, max_retries=2,
    soft_time_limit=480, time_limit=540,
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
        rss  = await RSSFetcher().fetch_all()
        api  = await NewsAPIFetcher().fetch_top_headlines()
        all_raw = rss + api
        log.info("raw_fetched", rss=len(rss), api=len(api))

        existing_urls   = await repo.get_existing_urls()
        existing_hashes = await repo.get_existing_hashes()
        unique, duped = Deduplicator(existing_hashes, existing_urls).filter(all_raw)
        saved = await repo.bulk_create_raw(unique)

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

    MAX_DAILY_SUMMARIES  = settings.max_daily_summaries
    TOP_N                = 30
    CONCURRENCY          = 1
    ARTICLE_TIMEOUT       = 45
    MIN_WORDS             = 20
    JACCARD_THRESHOLD     = 0.55
    SEMANTIC_THRESHOLD    = settings.semantic_dedup_threshold

    redis = await get_redis()
    cache = CacheClient(redis)

    today       = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    budget_key  = f"daily_summaries:{today}"
    daily_count = int(await redis.get(budget_key) or 0)
    if daily_count >= MAX_DAILY_SUMMARIES:
        log.info("daily_budget_reached", count=daily_count, limit=MAX_DAILY_SUMMARIES)
        return {"summarized": 0, "skipped_budget": daily_count}

    remaining  = MAX_DAILY_SUMMARIES - daily_count
    summarizer = _get_summarizer()

    stats = {"summarized": 0, "skipped_stub": 0, "skipped_dedup_jaccard": 0,
             "skipped_dedup_semantic": 0, "skipped_cache": 0}
    sem = asyncio.Semaphore(CONCURRENCY)

    async with AsyncSessionLocal() as db:
        repo = ArticleRepository(db)

        candidates = await repo.get_unsummarized(limit=100)
        if not candidates:
            return {**stats}

        ranked  = sorted(candidates, key=_rank_article, reverse=True)
        to_proc = ranked[:min(TOP_N, remaining)]

        recent_titles: list[str] = await repo.get_recent_summarized_titles(hours=6)

        # Stage 2 prep: batch-embed every candidate title + every recent
        # title in ONE call, so process_one() below only does cheap
        # in-memory cosine similarity — no per-article model calls.
        embeddings_map: dict[str, list[float]] = {}
        if settings.semantic_dedup_enabled:
            all_titles = [a.title_en for a in to_proc] + recent_titles
            try:
                embeddings_map = await _batch_get_embeddings(redis, all_titles)
            except Exception as e:
                log.warning("embedding_batch_failed", error=str(e))
                embeddings_map = {}

        async def process_one(article) -> None:
            async with sem:
                try:
                    content    = article.full_content_en or article.summary_en or article.title_en
                    word_count = len(content.split())

                    # A3: stub — skip Bedrock entirely
                    if word_count < MIN_WORDS:
                        desc = (article.summary_en or content)[:400]
                        await repo.update_summary(
                            article, sentence_1=desc, sentence_2="", sentence_3="",
                            category=_heuristic_cat(article.title_en), is_breaking=False,
                        )
                        await db.commit()
                        stats["skipped_stub"] += 1
                        return

                    # B1 Stage 1: Jaccard — cheap, runs first
                    for rt in recent_titles:
                        if _title_similarity(article.title_en, rt) >= JACCARD_THRESHOLD:
                            similar = await repo.find_similar_summarized(rt)
                            if similar:
                                await repo.update_summary(
                                    article, sentence_1=similar.summary_en,
                                    sentence_2="", sentence_3="",
                                    category=similar.category, is_breaking=similar.is_breaking,
                                )
                                await db.commit()
                                stats["skipped_dedup_jaccard"] += 1
                                return

                    # B1 Stage 2: semantic — only if Stage 1 found nothing
                    if settings.semantic_dedup_enabled and embeddings_map:
                        best_title, best_sim = _best_semantic_match(
                            article.title_en, recent_titles, embeddings_map
                        )
                        if best_sim >= SEMANTIC_THRESHOLD:
                            similar = await repo.find_similar_summarized(best_title)
                            if similar:
                                await repo.update_summary(
                                    article, sentence_1=similar.summary_en,
                                    sentence_2="", sentence_3="",
                                    category=similar.category, is_breaking=similar.is_breaking,
                                )
                                await db.commit()
                                stats["skipped_dedup_semantic"] += 1
                                log.debug("semantic_dedup_match", title=article.title_en[:60],
                                          matched=best_title[:60], similarity=round(best_sim, 3))
                                return

                    # B2: summary hash cache
                    content_hash = hashlib.sha256(
                        f"{article.title_en}:{content[:400]}".encode()
                    ).hexdigest()
                    cache_key = f"summary:{content_hash}"
                    cached = await redis.get(cache_key)
                    if cached:
                        data = json.loads(cached)
                        await repo.update_summary(
                            article, sentence_1=data["s1"], sentence_2=data["s2"], sentence_3=data["s3"],
                            category=data["cat"], is_breaking=False,
                        )
                        await db.commit()
                        stats["skipped_cache"] += 1
                        return

                    # ── Call Bedrock ──────────────────────────────
                    summary = await asyncio.wait_for(
                        summarizer.summarize(article.title_en, content), timeout=ARTICLE_TIMEOUT,
                    )

                    category = _heuristic_cat(article.title_en)
                    if category == "world":
                        try:
                            category = await asyncio.wait_for(
                                summarizer.categorize(article.title_en, content[:300]), timeout=15,
                            )
                        except asyncio.TimeoutError:
                            pass

                    is_breaking = _detect_breaking(
                        article.title_en, article.source_name, article.published_at
                    )

                    await repo.update_summary(
                        article, sentence_1=summary.sentence_1, sentence_2=summary.sentence_2,
                        sentence_3=summary.sentence_3, category=category, is_breaking=is_breaking,
                    )

                    await redis.setex(cache_key, 86400 * 7, json.dumps({
                        "s1": summary.sentence_1, "s2": summary.sentence_2,
                        "s3": summary.sentence_3, "cat": category,
                    }))

                    await redis.incr(budget_key)
                    await redis.expire(budget_key, 86400)

                    recent_titles.append(article.title_en)
                    await db.commit()
                    stats["summarized"] += 1
                    log.info("article_summarized", id=article.id, category=category)

                except asyncio.TimeoutError:
                    log.warning("article_timeout", id=article.id)
                    await db.rollback()
                except Exception as e:
                    log.error("summarize_failed", id=article.id, error=str(e))
                    await db.rollback()

        await asyncio.gather(*[process_one(a) for a in to_proc])

    try:
        deleted = await cache.delete_pattern("stories:*")
        log.info("cache_invalidated", keys=deleted)
    except Exception as e:
        log.warning("cache_invalidate_failed", error=str(e))

    log.info("summarize_stats", **stats, daily_total=daily_count + stats["summarized"])
    return stats


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


# ── Shared helpers ────────────────────────────────────────────

def _heuristic_cat(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["health","disease","vaccine","hospital","cancer","covid","drug","medical"]):
        return "health"
    if any(w in t for w in ["climate","carbon","emissions","renewable","drought","flood","wildfire"]):
        return "climate"
    if any(w in t for w in ["football","soccer","basketball","tennis","olympic","sport","nba","nfl","fifa"]):
        return "sports"
    if any(w in t for w in ["science","research","nasa","space","biology","physics"]):
        return "science"
    if any(w in t for w in ["economy","stock","gdp","trade","market","bank","inflation"]):
        return "business"
    if any(w in t for w in ["ai","tech","software","apple","google","chip","cyber","robot","startup"]):
        return "tech"
    if any(w in t for w in ["art","music","film","movie","culture","book","oscar"]):
        return "arts"
    return "world"
