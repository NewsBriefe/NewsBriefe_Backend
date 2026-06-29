"""
Feature Flags API

Stored in Redis — changes take effect immediately with no restart needed.
Worker reads flags at the start of each task run.

Endpoints:
  GET  /v1/admin/flags          — view all current flags
  POST /v1/admin/flags          — update one or more flags
  POST /v1/admin/flags/reset    — reset all flags to defaults
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from typing import Annotated, Any
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log      = get_logger(__name__)
router   = APIRouter(prefix="/admin/flags", tags=["flags"])

# ── Default flag values ───────────────────────────────────────
FLAG_DEFAULTS: dict[str, Any] = {
    "summarize_enabled":       True,   # send articles to Bedrock/Claude
    "fetch_enabled":           True,   # fetch from RSS + NewsAPI
    "semantic_dedup_enabled":  True,   # Stage 2 fastembed dedup
    "max_daily_summaries":     200,    # hard Bedrock call cap per day
    "min_words":               20,     # skip stubs below this word count
    "top_n":                   30,     # max articles summarized per run
}

FLAG_DESCRIPTIONS = {
    "summarize_enabled":      "Send articles to LLM for summarization",
    "fetch_enabled":          "Fetch new articles from RSS + NewsAPI",
    "semantic_dedup_enabled": "Use fastembed semantic dedup (Stage 2)",
    "max_daily_summaries":    "Max Bedrock calls per day (0 = disable LLM)",
    "min_words":              "Skip articles with fewer words than this",
    "top_n":                  "Max articles to summarize per 15-min run",
}

REDIS_KEY = "feature_flags"


# ── Auth ──────────────────────────────────────────────────────
async def verify_admin(x_admin_key: str | None = Header(default=None)) -> None:
    if not settings.admin_key:
        raise HTTPException(status_code=503, detail="Admin endpoints disabled")
    if x_admin_key != settings.admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")

AdminDep = Annotated[None, Depends(verify_admin)]


# ── Helpers ───────────────────────────────────────────────────
async def _get_redis():
    from app.core.cache import get_redis
    return await get_redis()


async def _load_flags() -> dict[str, Any]:
    """Load flags from Redis, fill missing keys with defaults."""
    import json
    redis   = await _get_redis()
    stored  = await redis.get(REDIS_KEY)
    current = json.loads(stored) if stored else {}
    return {**FLAG_DEFAULTS, **current}


async def _save_flags(flags: dict[str, Any]) -> None:
    import json
    redis = await _get_redis()
    await redis.set(REDIS_KEY, json.dumps(flags))


# ── Schemas ───────────────────────────────────────────────────
class FlagUpdate(BaseModel):
    summarize_enabled:      bool  | None = None
    fetch_enabled:          bool  | None = None
    semantic_dedup_enabled: bool  | None = None
    max_daily_summaries:    int   | None = None
    min_words:              int   | None = None
    top_n:                  int   | None = None


class FlagOut(BaseModel):
    flags:        dict[str, Any]
    descriptions: dict[str, str]


# ── Endpoints ─────────────────────────────────────────────────

@router.get("", response_model=FlagOut, summary="View all feature flags")
async def get_flags(_auth: AdminDep) -> FlagOut:
    flags = await _load_flags()
    return FlagOut(flags=flags, descriptions=FLAG_DESCRIPTIONS)


@router.post("", response_model=FlagOut, summary="Update feature flags")
async def update_flags(body: FlagUpdate, _auth: AdminDep) -> FlagOut:
    flags   = await _load_flags()
    updates = body.model_dump(exclude_none=True)

    if not updates:
        raise HTTPException(status_code=400, detail="No flags provided")

    flags.update(updates)
    await _save_flags(flags)
    log.info("feature_flags_updated", updates=updates)
    return FlagOut(flags=flags, descriptions=FLAG_DESCRIPTIONS)


@router.post("/reset", response_model=FlagOut, summary="Reset all flags to defaults")
async def reset_flags(_auth: AdminDep) -> FlagOut:
    await _save_flags(FLAG_DEFAULTS.copy())
    log.info("feature_flags_reset")
    return FlagOut(flags=FLAG_DEFAULTS, descriptions=FLAG_DESCRIPTIONS)
