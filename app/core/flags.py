"""
Feature flag reader — used by tasks.py at the start of each run.

FIX: Two changes to prevent silent fallback to permissive defaults:

1. SAFE_DEFAULTS — if Redis is unreachable, everything is DISABLED
   (fail-safe). Better to skip a summarization run than to waste
   Bedrock tokens when flags can't be read.

2. Fresh Redis connection per call — avoids event loop mismatch
   when the worker creates its own loop via run_async(). The shared
   singleton from get_redis() was created in the API's event loop
   and fails silently inside the worker's separate loop.
"""
import json
import ssl
from typing import Any
from app.core.logging import get_logger

log = get_logger(__name__)

REDIS_KEY = "feature_flags"

# Permissive defaults — used by the API endpoint only
DEFAULTS: dict[str, Any] = {
    "summarize_enabled":       True,
    "fetch_enabled":           True,
    "semantic_dedup_enabled":  True,
    "max_daily_summaries":     200,
    "min_words":               20,
    "top_n":                   30,
}

# Safe defaults — used as fallback when Redis is unreachable in worker
# Everything disabled so we never spend tokens when flags can't be verified
SAFE_DEFAULTS: dict[str, Any] = {
    "summarize_enabled":       False,
    "fetch_enabled":           False,
    "semantic_dedup_enabled":  True,
    "max_daily_summaries":     0,
    "min_words":               20,
    "top_n":                   30,
}


async def get_flags() -> dict[str, Any]:
    """
    Load flags from Redis using a fresh connection.
    Falls back to SAFE_DEFAULTS (everything disabled) on any error.
    """
    import redis.asyncio as aioredis
    from app.core.config import get_settings
    import re

    settings  = get_settings()
    redis_url = str(settings.redis_url)
    clean_url = re.sub(r'\?.*$', '', redis_url)

    try:
        # Fresh connection per call — avoids event loop mismatch in worker
        if clean_url.startswith("rediss://"):
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE
            r = aioredis.from_url(clean_url, decode_responses=True, ssl_context=ssl_ctx)
        else:
            r = aioredis.from_url(clean_url, decode_responses=True)

        stored = await r.get(REDIS_KEY)
        await r.aclose()

        if stored:
            overrides = json.loads(stored)
            merged    = {**DEFAULTS, **overrides}
            log.debug("flags_loaded", **merged)
            return merged

        # Key not set yet — return permissive defaults
        log.info("flags_key_missing_using_defaults")
        return DEFAULTS.copy()

    except Exception as e:
        # Redis unreachable — fail safe, disable everything
        log.warning("flags_load_failed_safe_defaults", error=str(e))
        return SAFE_DEFAULTS.copy()
