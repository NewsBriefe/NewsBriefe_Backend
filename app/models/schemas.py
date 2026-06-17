"""API Pydantic schemas — the public contract."""
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field, field_validator


def _compute_time_ago(published_at: datetime) -> str:
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    diff = now - published_at
    total_seconds = max(0, int(diff.total_seconds()))
    minutes = total_seconds // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _compute_read_minutes(full_content: str | None, summary: str) -> int:
    text = full_content or summary
    words = len(text.split())
    return max(1, min(10, round(words / 250)))


class StoryOut(BaseModel):
    id: str
    title: str
    summary: str
    source: str
    category: str
    time_ago: str
    original_url: str | None = None
    image_url: str | None = None
    read_minutes: int = 2
    is_breaking: bool = False
    region: str | None = None
    language_code: str = "en"

    model_config = {"from_attributes": True}


class StoryDetailOut(StoryOut):
    sentence_1: str = ""
    sentence_2: str = ""
    sentence_3: str = ""

    @field_validator("sentence_1", "sentence_2", "sentence_3", mode="before")
    @classmethod
    def empty_str(cls, v: Any) -> str:
        return v or ""


class PaginatedStories(BaseModel):
    stories: list[StoryOut]
    total: int
    page: int
    per_page: int
    has_more: bool


class SearchResults(BaseModel):
    results: list[StoryOut]


class TranslateRequest(BaseModel):
    target_language: str = Field(..., min_length=2, max_length=8)


class TranslateSummaryOut(BaseModel):
    article_id: str
    language_code: str
    title: str
    summary: str
    sentence_1: str = ""
    sentence_2: str = ""
    sentence_3: str = ""
    provider: str


class LanguageOut(BaseModel):
    code: str
    name: str
    native: str


class LanguagesOut(BaseModel):
    languages: list[LanguageOut]


class HealthOut(BaseModel):
    status: str
    version: str
    environment: str
    db: str
    cache: str


class ErrorOut(BaseModel):
    code: str
    message: str
    details: dict | None = None
