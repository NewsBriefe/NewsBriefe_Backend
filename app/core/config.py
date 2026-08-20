import os
import re
from functools import lru_cache
from typing import Any, Literal
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_running_in_docker = os.getenv("DOCKER", "").lower() == "true"


def _clean_redis_url(url: str) -> str:
    return re.sub(r'\?.*$', '', url) if url else url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None if _running_in_docker else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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
    admin_key: str = Field(default="")

    # ── Database ─────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://newsbrief:newsbrief@postgres:5432/newsbrief"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis ─────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"
    cache_ttl_seconds: int = 14_400

    # ── Celery ────────────────────────────────────────────
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # ── AI Provider ───────────────────────────────────────
    # Values: groq | bedrock
    # Switch by changing this one env var — no code changes needed.
    ai_provider: Literal["groq", "bedrock"] = Field(default="groq")

    # ── Groq (FREE tier) ───────────────────
    # Get key at console.groq.com — no credit card needed
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="openai/gpt-oss-20b")
    groq_max_input_words: int = Field(default=300, ge=100, le=1000)
    groq_max_output_tokens: int = Field(default=220, ge=100, le=500)
    groq_timeout_seconds: int = Field(default=30, ge=1, le=120)
    groq_max_retries: int = Field(default=3, ge=0, le=10)

    # ── Claude / Anthropic ────────────────────────────────
    anthropic_api_key: str = Field(default="")
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 512
    claude_timeout_seconds: int = 30

    # ── AWS Bedrock (kept for future use) ─────────────────
    aws_access_key_id: str = Field(default="")
    aws_secret_access_key: str = Field(default="")
    aws_region: str = Field(default="us-east-1")
    bedrock_model_id: str = Field(default="us.deepseek.r1-v1:0")

    # ── Translation ──────────────────────────────────────
    deepl_api_key: str = Field(default="")
    translation_provider: str = "deepl"

    # ── News Sources ─────────────────────────────────────
    newsapi_key: str = Field(default="")
    news_fetch_interval_minutes: int = 240
    max_articles_per_fetch: int = 100
    dedup_similarity_threshold: float = 0.75

    # ── Token-optimization feature flags ──────────────────
    semantic_dedup_enabled: bool = Field(default=True)
    semantic_dedup_threshold: float = Field(default=0.86)
    max_daily_summaries: int = Field(default=200)

    # ── Story visibility window ───────────────────────────
    max_story_age_hours: int = Field(default=720)   # 30 days

    # ── Rate limiting ────────────────────────────────────
    rate_limit_per_minute: int = 60

    # ── Languages ────────────────────────────────────────
    supported_languages: list[str] = [
        "en", "am", "ar", "fr", "es", "pt", "sw",
        "hi", "zh", "id", "tr", "de", "ru", "ja",
        "ko", "it", "nl", "pl", "th", "vi",
    ]
    default_language: str = "en"

    # ── Sentry ───────────────────────────────────────────
    sentry_dsn: str = ""

    @field_validator("ai_provider", mode="before")
    @classmethod
    def _normalize_ai_provider(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, values: Any) -> Any:
        for field in ("supported_languages", "allowed_origins"):
            v = values.get(field)
            if isinstance(v, str):
                import json
                try:
                    values[field] = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    values[field] = [x.strip() for x in v.split(",") if x.strip()]

        for field in ("celery_broker_url", "celery_result_backend"):
            v = values.get(field)
            if isinstance(v, str):
                values[field] = _clean_redis_url(v)

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

    @property
    def use_bedrock(self) -> bool:
        return self.ai_provider.lower() == "bedrock"

    @property
    def use_groq(self) -> bool:
        return self.ai_provider.lower() == "groq"


@lru_cache
def get_settings() -> Settings:
    return Settings()
