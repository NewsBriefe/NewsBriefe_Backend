"""
API Pydantic schemas — the public contract.

FIX #1/#2/#5/#6: All field names now match Flutter's Story.fromJson() exactly:
  source_name → source
  country     → region
  published_at → time_ago (computed human-readable string)
  + added: is_breaking, read_minutes
  + PaginatedStories uses key "stories" (was "items")
  + Search response uses key "results"

Never expose ORM models directly to the API layer.
"""
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────

def _compute_time_ago(published_at: datetime) -> str:
    """Convert a UTC datetime to a human-readable 'Xh ago' string."""
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
    """Estimate reading time based on full article content (250 wpm)."""
    text = full_content or summary
    words = len(text.split())
    return max(1, min(10, round(words / 250)))


# ─────────────────────────────────────────────────────────
#  Story schemas — names match Flutter's Story.fromJson()
# ─────────────────────────────────────────────────────────

class StoryOut(BaseModel):
    """
    Story as returned to the Flutter app.
    Field names exactly match Story.fromJson() in api_service.dart.
    """
    id: str
    title: str
    summary: str                        # 3-sentence summary in requested language
    source: str                         # FIX #2: was source_name — Flutter reads "source"
    category: str
    time_ago: str                       # FIX #5: was missing — Flutter reads "time_ago"
    original_url: str | None = None     # FIX: was non-nullable, now optional
    image_url: str | None = None
    read_minutes: int = 2               # FIX #6: was missing — Flutter reads "read_minutes"
    is_breaking: bool = False           # FIX #6: was missing — Flutter reads "is_breaking"
    region: str | None = None           # FIX: Flutter reads "region" (was "country")
    language_code: str = "en"

    model_config = {"from_attributes": True}


class StoryDetailOut(StoryOut):
    """Extended story with pre-split sentence breakdown for the detail screen."""
    sentence_1: str = ""    # What happened
    sentence_2: str = ""    # Why it matters
    sentence_3: str = ""    # What comes next

    @field_validator("sentence_1", "sentence_2", "sentence_3", mode="before")
    @classmethod
    def empty_str(cls, v: Any) -> str:
        return v or ""


# ─────────────────────────────────────────────────────────
#  Pagination — Flutter reads response["stories"]
# ─────────────────────────────────────────────────────────

class PaginatedStories(BaseModel):
    stories: list[StoryOut]             # FIX #2: was "items" — Flutter reads "stories"
    total: int
    page: int
    per_page: int
    has_more: bool


# ─────────────────────────────────────────────────────────
#  Search response — Flutter reads response["results"]
# ─────────────────────────────────────────────────────────

class SearchResults(BaseModel):
    results: list[StoryOut]             # Flutter's searchStories reads "results"


# ─────────────────────────────────────────────────────────
#  Translation request/response
# ─────────────────────────────────────────────────────────

class TranslateRequest(BaseModel):
    target_language: str = Field(
        ...,
        min_length=2,
        max_length=8,
        description="ISO 639-1 language code e.g. 'am', 'fr', 'ar'",
    )


class TranslateSummaryOut(BaseModel):
    article_id: str
    language_code: str
    title: str
    summary: str
    sentence_1: str = ""
    sentence_2: str = ""
    sentence_3: str = ""
    provider: str


# ─────────────────────────────────────────────────────────
#  Languages
# ─────────────────────────────────────────────────────────

class LanguageOut(BaseModel):
    code: str
    name: str
    native: str


class LanguagesOut(BaseModel):
    languages: list[LanguageOut]


# ─────────────────────────────────────────────────────────
#  Health check
# ─────────────────────────────────────────────────────────

class HealthOut(BaseModel):
    status: str
    version: str
    environment: str
    db: str
    cache: str


# ─────────────────────────────────────────────────────────
#  Error response
# ─────────────────────────────────────────────────────────

class ErrorOut(BaseModel):
    code: str
    message: str
    details: dict | None = None
