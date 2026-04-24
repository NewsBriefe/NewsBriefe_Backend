import os
from functools import lru_cache
from typing import Any
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# When running inside Docker, DOCKER=true is set in the environment block.
# We skip reading the .env file entirely so that the local .env file
# (which has localhost URLs) can never override the Docker hostnames.
# Local dev (no DOCKER var set) still reads .env normally.
_running_in_docker = os.getenv("DOCKER", "").lower() == "true"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None if _running_in_docker else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ───────────────────────────────────────────────
    app_name: str = "NewsBrief API"
    app_version: str = "1.0.0"
    debug: bool = False
    environment: str = Field(default="development")

    # ── API ───────────────────────────────────────────────
    api_prefix: str = "/v1"
    allowed_origins: list[str] = []
    api_key: str = Field(default="")

    # ── Database ─────────────────────────────────────────
    # Default uses "postgres" (Docker service name).
    # Local dev must set DATABASE_URL in .env with "localhost".
    database_url: str = "postgresql+asyncpg://newsbrief:newsbrief@postgres:5432/newsbrief"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis ─────────────────────────────────────────────
    # Default uses "redis" (Docker service name).
    redis_url: str = "redis://redis:6379/0"
    cache_ttl_seconds: int = 14_400  # 4 hours

    # ── Celery ────────────────────────────────────────────
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # ── AI / Claude ──────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 512
    claude_timeout_seconds: int = 30

    # ── Translation ──────────────────────────────────────
    deepl_api_key: str = Field(default="")
    translation_provider: str = "deepl"

    # ── News Sources ─────────────────────────────────────
    newsapi_key: str = Field(default="")
    news_fetch_interval_minutes: int = 240
    max_articles_per_fetch: int = 100
    dedup_similarity_threshold: float = 0.75

    # ── Rate limiting ────────────────────────────────────
    rate_limit_per_minute: int = 60

    # ── Supported languages ──────────────────────────────
    supported_languages: list[str] = [
        "en", "am", "ar", "fr", "es", "pt", "sw",
        "hi", "zh", "id", "tr", "de", "ru", "ja",
        "ko", "it", "nl", "pl", "th", "vi",
    ]
    default_language: str = "en"

    # ── Sentry ───────────────────────────────────────────
    sentry_dsn: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_list_fields(cls, values: Any) -> Any:
        for field in ("supported_languages", "allowed_origins"):
            v = values.get(field)
            if isinstance(v, str):
                import json
                try:
                    values[field] = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    values[field] = [x.strip() for x in v.split(",") if x.strip()]
        return values

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
