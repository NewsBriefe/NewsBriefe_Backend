"""
Celery Workers — optimized summarization pipeline

AI_PROVIDER controls which LLM is used:
  groq    → GroqSummarizationService    (free, fast, recommended)
  bedrock → BedrockSummarizationService (AWS, kept for future use)
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

celery_app = Celery(
    "newsbrief",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
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
    """
    Return the configured summarizer or fail fast on invalid configuration.
    Switch provider by changing AI_PROVIDER — no code changes needed.
    """
    provider = settings.ai_provider

    if provider == "groq":
        from app.services.groq_summarizer import GroqSummarizationService
        log.info("ai_provider", provider="groq", model=settings.groq_model)
        return GroqSummarizationService()

    if provider == "bedrock":
        from app.services.bedrock_summarizer import BedrockSummarizationService
        log.info("ai_provider", provider="bedrock", model=settings.bedrock_model_id)
        return BedrockSummarizationService()

    raise ValueError(f"Unsupported AI_PROVIDER: {provider}")


# ── Breaking news detection ───────────────────────────────────

_BREAKING_KW  = frozenset(["breaking","urgent","alert","flash:","just in",
                            "developing","emergency","crisis"])
_BREAKING_SRC = frozenset(["Reuters","AP News","Bloomberg","BBC","Al Jazeera",
                            "AFP","Associated Press"])

def _detect_breaking(title: str, source: str, published_at: datetime) -> bool:
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    if (now - published_at).total_seconds() / 3600 > 3:
        return False
    return any(kw in title.lower() for kw in _BREAKING_KW) or source in _BREAKING_SRC


# ── Article ranking ───────────────────────────────────────────

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


# ── Title similarity (Jaccard) ────────────────────────────────

_STOPWORDS = {"the","a","an","in","on","at","to","for","of","and","or","is",
              "are","was","were","by","with","as","its","it","this","that",
              "from","has","have"}

def _title_similarity(t1: str, t2: str) -> float:
    def words(t):
        return {w.lower() for w in re.findall(r'\b\w+\b', t)
                if w.lower() not in _STOPWORDS and len(w) > 2}
    w1, w2 = words(t1), words(t2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


# ── Semantic embedding helpers ────────────────────────────────

async def _batch_get_embeddings(redis, texts: list[str]) -> dict[str, list[float]]:
    """
    Fetch cached embeddings for a list of titles via one Redis mget,
    compute only the missing ones in a single fastembed batch call,
    then pipeline them back to Redis. Avoids N+1 round trips and
    avoids re-embedding the same title across runs.
    """
    if not texts:
        return {}
    unique = list(dict.fromkeys(texts))
    keys = [f"embedding:{hashlib.sha256(t.encode()).hexdigest()[:16]}" for t in unique]

    cached = await redis.mget(keys)
    result: dict[str, list[float]] = {}
    missing_texts, missing_keys = [], []
    for t, k, c in zip(unique, keys, cached):
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
            pipe.setex(k, 86400, json.dumps(emb))
        await pipe.execute()
    return result


def _best_semantic_match(
    title: str, recent_titles: list[str], embeddings_map: dict
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
    from app.core.flags import get_flags
    from app.services.ingestion import RSSFetcher, NewsAPIFetcher, Deduplicator
    from app.services.repository import ArticleRepository
    from app.models.orm import FetchLog

    flags = await get_flags()
    if not flags["fetch_enabled"]:
        log.info("fetch_disabled_by_flag")
        return {"fetched": 0, "new": 0, "duped": 0, "disabled": True}

    start = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        repo    = ArticleRepository(db)
        rss     = await RSSFetcher().fetch_all()
        api     = await NewsAPIFetcher().fetch_top_headlines()
        all_raw = rss + api
        log.info("raw_fetched", rss=len(rss), api=len(api))

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
    from app.core.flags import get_flags
    from app.services.repository import ArticleRepository

    redis = await get_redis()
    cache = CacheClient(redis)

    # ── Read all flags from Redis ─────────────────────────
    flags = await get_flags()
    log.info("summarize_flags", **{k: v for k, v in flags.items()})

    if not flags["summarize_enabled"]:
        log.info("summarization_disabled_by_flag")
        return {"summarized": 0, "disabled": True}

    MAX_DAILY     = int(flags["max_daily_summaries"])
    TOP_N         = int(flags["top_n"])
    MIN_WORDS     = int(flags["min_words"])
    SEM_ENABLED   = bool(flags["semantic_dedup_enabled"])
    SEM_THRESHOLD = float(settings.semantic_dedup_threshold)
    JACCARD_TH    = 0.55
    CONCURRENCY   = 1
    ARTICLE_TO    = 45

    today       = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    budget_key  = f"daily_summaries:{today}"
    daily_count = int(await redis.get(budget_key) or 0)
    if daily_count >= MAX_DAILY:
        log.info("daily_budget_reached", count=daily_count, limit=MAX_DAILY)
        return {"summarized": 0, "skipped_budget": daily_count}

    remaining  = MAX_DAILY - daily_count
    summarizer = _get_summarizer()
    stats      = {"summarized": 0, "skipped_stub": 0,
                  "skipped_dedup_jaccard": 0, "skipped_dedup_semantic": 0,
                  "skipped_cache": 0, "failed": 0}
    sem = asyncio.Semaphore(CONCURRENCY)

    async with AsyncSessionLocal() as db:
        repo = ArticleRepository(db)

        candidates = await repo.get_unsummarized(limit=100)
        if not candidates:
            log.info("no_pending_articles")
            return {**stats}

        ranked  = sorted(candidates, key=_rank_article, reverse=True)
        to_proc = ranked[:min(TOP_N, remaining)]

        recent_titles: list[str] = await repo.get_recent_summarized_titles(hours=6)

        # Stage 2 prep: batch-embed every candidate title + every recent
        # title in ONE call, so process_one() below only does cheap
        # in-memory cosine similarity — no per-article model calls.
        embeddings_map: dict = {}
        if SEM_ENABLED:
            all_titles = [a.title_en for a in to_proc] + recent_titles
            try:
                embeddings_map = await _batch_get_embeddings(redis, all_titles)
            except Exception as e:
                log.warning("embedding_batch_failed", error=str(e))

        async def process_one(candidate) -> None:
            article_id = candidate.id
            async with sem:
                article_db = AsyncSessionLocal()
                article_repo = ArticleRepository(article_db)
                try:
                    article = await article_repo.get_by_id(article_id)
                    if article is None or article.is_summarized:
                        return
                    content    = article.full_content_en or article.summary_en or article.title_en
                    word_count = len(content.split())

                    # Skip empty stubs
                    if word_count < MIN_WORDS:
                        await article_repo.update_summary(
                            article,
                            sentence_1=article.summary_en or article.title_en,
                            sentence_2="", sentence_3="",
                            category=_heuristic_cat(article.title_en),
                            is_breaking=False,
                        )
                        await article_db.commit()
                        stats["skipped_stub"] += 1
                        return

                    # Jaccard dedup
                    for rt in recent_titles:
                        if _title_similarity(article.title_en, rt) >= JACCARD_TH:
                            similar = await article_repo.find_similar_summarized(rt)
                            if similar:
                                await article_repo.update_summary(
                                    article,
                                    sentence_1=similar.summary_en,
                                    sentence_2="", sentence_3="",
                                    category=similar.category,
                                    is_breaking=similar.is_breaking,
                                )
                                await article_db.commit()
                                stats["skipped_dedup_jaccard"] += 1
                                return

                    # Semantic dedup
                    if SEM_ENABLED and embeddings_map:
                        best_title, best_sim = _best_semantic_match(
                            article.title_en, recent_titles, embeddings_map
                        )
                        if best_sim >= SEM_THRESHOLD:
                            similar = await article_repo.find_similar_summarized(best_title)
                            if similar:
                                await article_repo.update_summary(
                                    article,
                                    sentence_1=similar.summary_en,
                                    sentence_2="", sentence_3="",
                                    category=similar.category,
                                    is_breaking=similar.is_breaking,
                                )
                                await article_db.commit()
                                stats["skipped_dedup_semantic"] += 1
                                return

                    # Summary hash cache
                    content_hash = hashlib.sha256(
                        f"{article.title_en}:{content[:400]}".encode()
                    ).hexdigest()
                    cache_key = f"summary:{content_hash}"
                    cached    = await redis.get(cache_key)
                    if cached:
                        data = json.loads(cached)
                        await article_repo.update_summary(
                            article,
                            sentence_1=data["s1"], sentence_2=data["s2"], sentence_3=data["s3"],
                            category=data["cat"], is_breaking=False,
                        )
                        await article_db.commit()
                        stats["skipped_cache"] += 1
                        return

                    # ── Call LLM ──────────────────────────────────
                    summary  = await asyncio.wait_for(
                        summarizer.summarize(article.title_en, content),
                        timeout=ARTICLE_TO,
                    )
                    # Categorization stays local to save a second LLM call.
                    category = _heuristic_cat(article.title_en)

                    is_breaking = _detect_breaking(
                        article.title_en, article.source_name, article.published_at
                    )
                    await article_repo.update_summary(
                        article,
                        sentence_1=summary.sentence_1,
                        sentence_2=summary.sentence_2,
                        sentence_3=summary.sentence_3,
                        category=category,
                        is_breaking=is_breaking,
                    )
                    await redis.setex(cache_key, 86400 * 7, json.dumps({
                        "s1": summary.sentence_1, "s2": summary.sentence_2,
                        "s3": summary.sentence_3, "cat": category,
                    }))
                    await redis.incr(budget_key)
                    await redis.expire(budget_key, 86400)
                    recent_titles.append(article.title_en)
                    await article_db.commit()
                    stats["summarized"] += 1
                    log.info("article_summarized", id=article.id,
                             category=category, provider=settings.ai_provider)

                except asyncio.TimeoutError:
                    log.warning("article_timeout", id=article_id)
                    await article_db.rollback()
                    stats["failed"] += 1
                except Exception as e:
                    log.error("summarize_failed", id=article_id, error=str(e))
                    await article_db.rollback()
                    stats["failed"] += 1
                finally:
                    await article_db.close()

        await asyncio.gather(*[process_one(a) for a in to_proc])

    if to_proc and stats["failed"] == len(to_proc):
        raise RuntimeError("All articles failed summarization")

    try:
        await cache.delete_pattern("stories:*")
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
