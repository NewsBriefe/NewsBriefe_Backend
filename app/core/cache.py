"""
Redis cache client.

redis.asyncio.from_url() does NOT accept ssl_cert_reqs as a URL query
parameter — it must be passed as a keyword argument. The URL is cleaned
before connecting and SSL options are injected separately.
"""
import re
import json
from typing import Any
import redis.asyncio as aioredis
from app.core.config import get_settings

settings = get_settings()

_redis: aioredis.Redis | None = None


def _parse_redis_url(url: str) -> tuple[str, dict]:
    """
    Strip query parameters that redis.asyncio doesn't accept in URLs
    and return them as kwargs for from_url().

    rediss://...upstash.io:6379/0?ssl_cert_reqs=none
      → clean_url = rediss://...upstash.io:6379/0
      → kwargs    = {"ssl_cert_reqs": None}
    """
    clean = re.sub(r'\?.*$', '', url)
    kwargs: dict = {}

    if url.startswith("rediss://"):
        # Pass ssl_cert_reqs=None (CERT_NONE) as a kwarg — not in the URL.
        kwargs["ssl_cert_reqs"] = None

    return clean, kwargs


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        clean_url, ssl_kwargs = _parse_redis_url(str(settings.redis_url))
        _redis = await aioredis.from_url(
            clean_url,
            encoding="utf-8",
            decode_responses=True,
            **ssl_kwargs,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


class CacheClient:
    def __init__(self, redis: aioredis.Redis, ttl: int = settings.cache_ttl_seconds):
        self._r = redis
        self._ttl = ttl

    async def get_json(self, key: str) -> Any | None:
        raw = await self._r.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._r.setex(
            key,
            ttl or self._ttl,
            json.dumps(value, default=str),
        )

    async def delete(self, key: str) -> None:
        await self._r.delete(key)

    async def delete_pattern(self, pattern: str) -> int:
        keys = await self._r.keys(pattern)
        if keys:
            return await self._r.delete(*keys)
        return 0

    async def exists(self, key: str) -> bool:
        return bool(await self._r.exists(key))

    @staticmethod
    def stories_key(lang: str, category: str, page: int) -> str:
        return f"stories:{lang}:{category}:{page}"

    @staticmethod
    def story_key(story_id: str, lang: str) -> str:
        return f"story:{story_id}:{lang}"

    @staticmethod
    def search_key(query: str, country: str | None, lang: str, category: str | None) -> str:
        slug = query.lower().replace(" ", "_")[:50]
        cat = (category or "all").lower()
        return f"search:{slug}:{country or 'all'}:{lang}:{cat}"

    @staticmethod
    def languages_key() -> str:
        return "languages:all"
