"""
API v1 — all endpoints in one file for clarity.

FIXES APPLIED:
  #1  Routes renamed /articles → /stories to match Flutter's RemoteNewsRepository.
  #2  Response shape: "items" → "stories", search uses "results".
  #3  Language normalisation: Flutter sends BCP-47 ("en-US") → backend ISO-639-1 ("en").
  #4  Category normalisation: Flutter sends "World" → stored/queried as "world".
  #8  Search cache key now includes category (was missing, caused wrong cache hits).
  #12 Optional API key auth via X-API-Key header (enable by setting API_KEY in .env).
"""
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis
from app.core.database import get_db
from app.core.cache import get_redis, CacheClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import (
    StoryOut, StoryDetailOut, PaginatedStories, SearchResults,
    TranslateRequest, TranslateSummaryOut,
    LanguagesOut, LanguageOut, HealthOut, ErrorOut,
    _compute_time_ago, _compute_read_minutes,
)
from app.services.repository import ArticleRepository
from app.services.translator import TranslationService, LANGUAGE_METADATA

settings = get_settings()
log = get_logger(__name__)
router = APIRouter()
_translator = TranslationService()


# ── Auth dependency ───────────────────────────────────────────
# FIX #12: Simple X-API-Key header check.
# Set API_KEY in .env to enable. Leave empty to skip (dev mode).

async def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.auth_enabled:
        return  # Auth disabled — dev mode
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid or missing API key"},
        )

AuthDep = Annotated[None, Depends(verify_api_key)]


# ── Parameter dependencies ────────────────────────────────────

# FIX #3: Accept Flutter's BCP-47 "language" param name and strip country suffix.
# "en-US" → "en", "fr-FR" → "fr", "ar-SA" → "ar", "th-TH" → "th"
def normalize_language(
    language: str = Query(default="en", max_length=10)
) -> str:
    code = language.split("-")[0].lower()
    if code not in settings.supported_languages:
        return settings.default_language
    return code


# FIX: Accept Flutter's "limit" param name (backend previously used "per_page").
def validate_pagination(
    page: int = Query(default=1, ge=1, le=1000),
    limit: int = Query(default=20, ge=1, le=100),
) -> tuple[int, int]:
    return page, limit


# FIX #4: Normalise category to lowercase to match DB storage.
# Flutter sends "World", DB stores "world".
def normalize_category(
    category: str | None = Query(default=None)
) -> str | None:
    if category is None:
        return None
    cat = category.strip().lower()
    return None if cat == "all" else cat


LangDep     = Annotated[str, Depends(normalize_language)]
PageDep     = Annotated[tuple[int, int], Depends(validate_pagination)]
CategoryDep = Annotated[str | None, Depends(normalize_category)]
DBDep       = Annotated[AsyncSession, Depends(get_db)]
CacheDep    = Annotated[aioredis.Redis, Depends(get_redis)]


# ─────────────────────────────────────────────────────────────
#  GET /stories
#  FIX #1: was /articles
# ─────────────────────────────────────────────────────────────

@router.get(
    "/stories",
    response_model=PaginatedStories,
    summary="Get top news stories",
    description=(
        "Returns paginated stories sorted by publish date. "
        "Pass `language` (BCP-47 or ISO-639-1) for translated summaries. "
        "Pass `category` (case-insensitive: World, Tech, Health…). "
        "Results are cached for 4 hours."
    ),
)
async def get_stories(
    _auth: AuthDep,
    db: DBDep,
    redis: CacheDep,
    lang: LangDep,
    page_per: PageDep,
    category: CategoryDep,
) -> PaginatedStories:
    page, per_page = page_per
    cache = CacheClient(redis)
    cache_key = CacheClient.stories_key(lang, category or "all", page)

    cached = await cache.get_json(cache_key)
    if cached:
        log.debug("cache_hit", key=cache_key)
        return PaginatedStories(**cached)

    repo = ArticleRepository(db)
    articles, total = await repo.get_top_stories(
        lang=lang,
        category=category,
        page=page,
        per_page=per_page,
    )

    items = [_to_story_out(a, lang, repo) for a in articles]
    result = PaginatedStories(
        stories=items,                              # FIX #2: key is "stories"
        total=total,
        page=page,
        per_page=per_page,
        has_more=(page * per_page) < total,
    )

    await cache.set_json(cache_key, result.model_dump())
    return result


# ─────────────────────────────────────────────────────────────
#  GET /stories/search
#  FIX #1: was /articles/search
#  FIX #2: returns {"results": [...]} to match Flutter's searchStories
# ─────────────────────────────────────────────────────────────

@router.get(
    "/stories/search",
    response_model=SearchResults,
    summary="Search stories by keyword",
)
async def search_stories(
    _auth: AuthDep,
    db: DBDep,
    redis: CacheDep,
    lang: LangDep,
    category: CategoryDep,
    q: str = Query(..., min_length=1, max_length=200),
    country: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=20, ge=1, le=50),
) -> SearchResults:
    cache = CacheClient(redis)
    # FIX #8: category is now part of the cache key
    cache_key = CacheClient.search_key(q, country, lang, category)

    cached = await cache.get_json(cache_key)
    if cached:
        return SearchResults(**cached)

    repo = ArticleRepository(db)
    articles = await repo.search(
        query=q,
        country=country,
        category=category,
        lang=lang,
        limit=limit,
    )

    results = [_to_story_out(a, lang, repo) for a in articles]
    response = SearchResults(results=results)          # FIX #2: key is "results"
    await cache.set_json(cache_key, response.model_dump(), ttl=1800)
    return response


# ─────────────────────────────────────────────────────────────
#  GET /stories/{story_id}
#  FIX #1: was /articles/{article_id}
# ─────────────────────────────────────────────────────────────

@router.get(
    "/stories/{story_id}",
    response_model=StoryDetailOut,
    summary="Get a single story with sentence breakdown",
)
async def get_story(
    story_id: str,
    _auth: AuthDep,
    db: DBDep,
    redis: CacheDep,
    lang: LangDep,
) -> StoryDetailOut:
    cache = CacheClient(redis)
    cache_key = CacheClient.story_key(story_id, lang)

    cached = await cache.get_json(cache_key)
    if cached:
        return StoryDetailOut(**cached)

    repo = ArticleRepository(db)
    article = await repo.get_by_id(story_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Story not found"},
        )

    localized = repo.localize(article, lang)
    sentences = _split_sentences(localized["summary"])

    out = StoryDetailOut(
        id=article.id,
        title=localized["title"],
        summary=localized["summary"],
        source=localized["source"],
        category=localized["category"],
        time_ago=localized["time_ago"],
        original_url=localized["original_url"],
        image_url=localized["image_url"],
        read_minutes=localized["read_minutes"],
        is_breaking=localized["is_breaking"],
        region=localized["region"],
        language_code=localized["language_code"],
        sentence_1=sentences[0] if len(sentences) > 0 else "",
        sentence_2=sentences[1] if len(sentences) > 1 else "",
        sentence_3=sentences[2] if len(sentences) > 2 else "",
    )
    await cache.set_json(cache_key, out.model_dump())
    return out


# ─────────────────────────────────────────────────────────────
#  POST /stories/{story_id}/translate
# ─────────────────────────────────────────────────────────────

@router.post(
    "/stories/{story_id}/translate",
    response_model=TranslateSummaryOut,
    summary="Translate a story summary on-demand",
    description=(
        "Request a translation for a language not yet pre-translated. "
        "Result is persisted in the DB so subsequent calls are instant."
    ),
)
async def translate_story(
    story_id: str,
    body: TranslateRequest,
    _auth: AuthDep,
    db: DBDep,
) -> TranslateSummaryOut:
    # FIX #3: normalise incoming language code too
    target_lang = body.target_language.split("-")[0].lower()

    if target_lang not in settings.supported_languages:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_language",
                "message": f"Language '{target_lang}' is not supported.",
            },
        )

    repo = ArticleRepository(db)
    article = await repo.get_by_id(story_id)
    if not article:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Story not found"},
        )

    existing = await repo.get_translation(story_id, target_lang)
    if existing:
        sentences = _split_sentences(existing.summary)
        return TranslateSummaryOut(
            article_id=story_id,
            language_code=target_lang,
            title=existing.title,
            summary=existing.summary,
            sentence_1=sentences[0] if len(sentences) > 0 else "",
            sentence_2=sentences[1] if len(sentences) > 1 else "",
            sentence_3=sentences[2] if len(sentences) > 2 else "",
            provider=existing.translation_provider,
        )

    try:
        t_title, t_summary = await _translator.translate_pair(
            article.title_en,
            article.summary_en,
            target_lang,
        )
    except Exception as e:
        log.error("translate_endpoint_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "translation_failed", "message": "Translation service unavailable"},
        )

    provider = TranslationService.provider_name(target_lang)
    await repo.upsert_translation(story_id, target_lang, t_title, t_summary, provider)
    await db.commit()

    sentences = _split_sentences(t_summary)
    return TranslateSummaryOut(
        article_id=story_id,
        language_code=target_lang,
        title=t_title,
        summary=t_summary,
        sentence_1=sentences[0] if len(sentences) > 0 else "",
        sentence_2=sentences[1] if len(sentences) > 1 else "",
        sentence_3=sentences[2] if len(sentences) > 2 else "",
        provider=provider,
    )


# ─────────────────────────────────────────────────────────────
#  GET /languages
# ─────────────────────────────────────────────────────────────

@router.get(
    "/languages",
    response_model=LanguagesOut,
    summary="List all supported languages",
)
async def get_languages(redis: CacheDep) -> LanguagesOut:
    cache = CacheClient(redis)
    cache_key = CacheClient.languages_key()

    cached = await cache.get_json(cache_key)
    if cached:
        return LanguagesOut(**cached)

    langs = [
        LanguageOut(**lang)
        for lang in LANGUAGE_METADATA
        if lang["code"] in settings.supported_languages
    ]
    result = LanguagesOut(languages=langs)
    await cache.set_json(cache_key, result.model_dump(), ttl=86400)  # 24h
    return result


# ─────────────────────────────────────────────────────────────
#  GET /health
# ─────────────────────────────────────────────────────────────

@router.get(
    "/health",
    response_model=HealthOut,
    summary="Service health check",
    include_in_schema=False,
)
async def health_check(db: DBDep, redis: CacheDep) -> HealthOut:
    db_status = "ok"
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    cache_status = "ok"
    try:
        await redis.ping()
    except Exception:
        cache_status = "error"

    return HealthOut(
        status="ok" if db_status == "ok" and cache_status == "ok" else "degraded",
        version=settings.app_version,
        environment=settings.environment,
        db=db_status,
        cache=cache_status,
    )


# ── Internal helpers ──────────────────────────────────────────

def _to_story_out(article, lang: str, repo: ArticleRepository) -> StoryOut:
    localized = repo.localize(article, lang)
    return StoryOut(
        id=localized["id"],
        title=localized["title"],
        summary=localized["summary"],
        source=localized["source"],
        category=localized["category"],
        time_ago=localized["time_ago"],
        original_url=localized["original_url"],
        image_url=localized["image_url"],
        read_minutes=localized["read_minutes"],
        is_breaking=localized["is_breaking"],
        region=localized["region"],
        language_code=localized["language_code"],
    )


def _split_sentences(text: str) -> list[str]:
    import re
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()][:3]
