from functools import lru_cache
from typing import Any
from pydantic import Field, PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ───────────────────────────────────────────────
    app_name: str = "NewsBrief API"
    app_version: str = "1.0.0"
    debug: bool = False
    environment: str = Field(default="development")  # development | staging | production

    # ── API ───────────────────────────────────────────────
    api_prefix: str = "/v1"
    # FIX #19: default to [] — must be set explicitly in .env for production
    # In .env: ALLOWED_ORIGINS=["https://yourdomain.com"]
    allowed_origins: list[str] = []
    # Simple static API key — Flutter must send X-API-Key header
    # Set in .env: API_KEY=your-secret-key-here
    # Leave empty to disable auth (dev only)
    api_key: str = Field(default="")

    # ── Database ─────────────────────────────────────────
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://newsbrief:newsbrief@localhost:5432/newsbrief"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis ────────────────────────────────────────────
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")
    cache_ttl_seconds: int = 14_400  # 4 hours

    # ── Celery ────────────────────────────────────────────
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ── AI / Claude ──────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 512
    claude_timeout_seconds: int = 30

    # ── Translation ──────────────────────────────────────
    deepl_api_key: str = Field(default="")
    translation_provider: str = "deepl"  # deepl | google | none

    # ── News Sources ─────────────────────────────────────
    newsapi_key: str = Field(default="")
    news_fetch_interval_minutes: int = 240  # every 4 hours
    max_articles_per_fetch: int = 100
    dedup_similarity_threshold: float = 0.75

    # ── Rate limiting ────────────────────────────────────
    rate_limit_per_minute: int = 60

    # ── Supported languages (ISO 639-1 two-letter codes) ─
    # FIX #10: pydantic-settings v2 parses list[str] from .env as JSON.
    # In .env use: SUPPORTED_LANGUAGES=["en","fr","ar"]
    # FIX #15: Added th (Thai) and vi (Vietnamese) to match Flutter's 20 languages.
    # Flutter BCP-47 → backend ISO: "en-US"→"en", "th-TH"→"th", "vi-VN"→"vi"
    supported_languages: list[str] = [
        "en", "am", "ar", "fr", "es", "pt", "sw",
        "hi", "zh", "id", "tr", "de", "ru", "ja",
        "ko", "it", "nl", "pl", "th", "vi",
    ]
    default_language: str = "en"

    # ── Sentry ───────────────────────────────────────────
    sentry_dsn: str = ""

    # FIX #10: Handle comma-separated strings from .env in addition to JSON arrays.
    @model_validator(mode="before")
    @classmethod
    def _coerce_list_fields(cls, values: Any) -> Any:
        for field in ("supported_languages", "allowed_origins"):
            v = values.get(field)
            if isinstance(v, str):
                import json
                try:
                    parsed = json.loads(v)
                    values[field] = parsed
                except (json.JSONDecodeError, ValueError):
                    # Comma-separated fallback: "en,fr,ar" → ["en","fr","ar"]
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
        """Auth is only enforced when an API key is configured."""
        return bool(self.api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
