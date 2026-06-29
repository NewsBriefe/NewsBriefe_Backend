"""
Feature flag reader — used by tasks.py at the start of each run.
Reads from Redis so changes via POST /v1/admin/flags take effect
on the next task run with no worker restart needed.
"""
import json
from typing import Any
from app.core.logging import get_logger

log = get_logger(__name__)

REDIS_KEY = "feature_flags"

DEFAULTS: dict[str, Any] = {
    "summarize_enabled":       True,
    "fetch_enabled":           True,
    "semantic_dedup_enabled":  True,
    "max_daily_summaries":     200,
    "min_words":               20,
    "top_n":                   30,
}


async def get_flags() -> dict[str, Any]:
    """
    Load flags from Redis. Falls back to defaults if Redis is
    unavailable or flag not set — worker always has safe values.
    """
    try:
        from app.core.cache import get_redis
        redis  = await get_redis()
        stored = await redis.get(REDIS_KEY)
        if stored:
            overrides = json.loads(stored)
            return {**DEFAULTS, **overrides}
    except Exception as e:
        log.warning("flags_load_failed", error=str(e))
    return DEFAULTS.copy()
