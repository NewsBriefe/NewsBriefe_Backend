"""
Summarization Service — uses Claude to produce 3-sentence summaries.

Each summary answers exactly:
  1. What happened?
  2. Why does it matter?
  3. What comes next?

FIX #13: Added sports, climate, arts to valid categories (was missing — only had
world/science/business/health/tech/culture). Prompt and heuristics updated.
"""
import json
import re
from dataclasses import dataclass
import anthropic
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)

SUMMARIZE_SYSTEM = """\
You are NewsBrief's editorial AI. Your only job is to read news articles \
and write a 3-sentence neutral summary. Each sentence must be:
- Plain English, under 30 words
- Factual — no opinion, no spin, no filler
- Self-contained — a reader who knows nothing about the story can \
understand each sentence

Sentence 1 — What happened: the core event in one clear sentence.
Sentence 2 — Why it matters: the significance or impact.
Sentence 3 — What comes next: the likely next development or open question.

Respond ONLY with valid JSON in this exact shape:
{
  "sentence_1": "...",
  "sentence_2": "...",
  "sentence_3": "..."
}

Do not include any text outside the JSON object."""

# FIX #13: Added sports, climate, arts to match Flutter's 9 categories.
# Flutter categories: World, Science, Business, Health, Tech, Sports, Climate, Arts
# Backend stores lowercase: world, science, business, health, tech, sports, climate, arts
CATEGORIZE_SYSTEM = """\
You are a news categorizer. Given a news article title and snippet, \
return the single best category from this list:
world, science, business, health, tech, sports, climate, arts

Definitions:
- world: international politics, conflict, diplomacy, elections
- science: research, space, medicine discoveries, environment science
- business: economy, markets, companies, trade, finance
- health: public health, medicine, drugs, diseases, hospitals
- tech: technology, AI, software, cybersecurity, gadgets
- sports: athletics, football, basketball, tennis, Olympics, any sport
- climate: climate change, environment, energy, sustainability, weather events
- arts: culture, film, music, books, art, entertainment

Respond with ONLY the category word, nothing else."""


@dataclass
class Summary:
    sentence_1: str   # What happened
    sentence_2: str   # Why it matters
    sentence_3: str   # What comes next

    @property
    def full(self) -> str:
        return f"{self.sentence_1} {self.sentence_2} {self.sentence_3}"


class SummarizationService:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.claude_timeout_seconds,
        )

    async def summarize(self, title: str, content: str) -> Summary:
        truncated = self._truncate(content, max_words=3000)
        user_msg = f"Title: {title}\n\nContent:\n{truncated}"

        try:
            message = await self._client.messages.create(
                model=settings.claude_model,
                max_tokens=settings.claude_max_tokens,
                system=SUMMARIZE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = message.content[0].text.strip()
            return self._parse_summary(raw)

        except anthropic.APIStatusError as e:
            log.error("claude_api_error", status=e.status_code, message=str(e))
            return self._fallback_summary(content)
        except Exception as e:
            log.error("summarization_failed", error=str(e))
            return self._fallback_summary(content)

    async def categorize(self, title: str, snippet: str) -> str:
        # FIX #13: updated valid set to match Flutter's 9 categories
        valid = {"world", "science", "business", "health", "tech", "sports", "climate", "arts"}
        user_msg = f"Title: {title}\nSnippet: {snippet[:400]}"

        try:
            message = await self._client.messages.create(
                model=settings.claude_model,
                max_tokens=8,
                system=CATEGORIZE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            cat = message.content[0].text.strip().lower()
            return cat if cat in valid else "world"
        except Exception:
            return self._heuristic_category(title)

    async def summarize_batch(
        self,
        articles: list[tuple[str, str]],
    ) -> list[Summary]:
        results = []
        for title, content in articles:
            summary = await self.summarize(title, content)
            results.append(summary)
        return results

    # ── Helpers ──────────────────────────────────────────────

    def _parse_summary(self, raw: str) -> Summary:
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        try:
            data = json.loads(clean)
            return Summary(
                sentence_1=data.get("sentence_1", ""),
                sentence_2=data.get("sentence_2", ""),
                sentence_3=data.get("sentence_3", ""),
            )
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("summary_parse_failed", raw=raw[:200], error=str(e))
            return self._sentences_fallback(raw)

    def _sentences_fallback(self, text: str) -> Summary:
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        sentences = [s.strip() for s in sentences if s.strip()]
        return Summary(
            sentence_1=sentences[0] if len(sentences) > 0 else text[:150],
            sentence_2=sentences[1] if len(sentences) > 1 else "",
            sentence_3=sentences[2] if len(sentences) > 2 else "",
        )

    def _fallback_summary(self, content: str) -> Summary:
        sentences = re.split(r"(?<=[.!?])\s+", content.strip())
        sentences = [s.strip() for s in sentences if len(s) > 20][:3]
        return Summary(
            sentence_1=sentences[0][:200] if sentences else content[:200],
            sentence_2=sentences[1][:200] if len(sentences) > 1 else "",
            sentence_3=sentences[2][:200] if len(sentences) > 2 else "",
        )

    @staticmethod
    def _truncate(text: str, max_words: int) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "…"

    @staticmethod
    def _heuristic_category(title: str) -> str:
        """Keyword-based fallback — FIX #13: added sports, climate, arts."""
        t = title.lower()
        if any(w in t for w in ["health", "disease", "vaccine", "hospital", "cancer", "covid", "drug", "medical"]):
            return "health"
        if any(w in t for w in ["climate", "carbon", "emissions", "renewable", "solar", "fossil", "drought", "flood", "wildfire"]):
            return "climate"
        if any(w in t for w in ["football", "soccer", "basketball", "tennis", "olympic", "sport", "athlete", "championship", "tournament", "nba", "nfl", "fifa"]):
            return "sports"
        if any(w in t for w in ["science", "research", "study", "nasa", "space", "universe", "biology", "physics", "chemistry"]):
            return "science"
        if any(w in t for w in ["economy", "stock", "gdp", "trade", "market", "bank", "inflation", "recession", "finance"]):
            return "business"
        if any(w in t for w in ["ai", "tech", "software", "apple", "google", "chip", "cyber", "robot", "startup", "data"]):
            return "tech"
        if any(w in t for w in ["art", "music", "film", "movie", "culture", "fashion", "book", "novel", "theater", "museum", "award", "oscar"]):
            return "arts"
        return "world"
