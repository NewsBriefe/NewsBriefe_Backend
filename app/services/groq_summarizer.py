"""
Groq Summarization Provider

Uses Groq's OpenAI-compatible API with GPT-OSS 20B by default.
Exact same interface as BedrockSummarizationService — swap by setting
AI_PROVIDER=groq in your environment.

Rate limits vary by model and plan. Check Groq's current limits before
setting the worker's daily summary budget.
"""
import asyncio
import json
import re
from dataclasses import dataclass
from groq import Groq
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log      = get_logger(__name__)

SUMMARIZE_PROMPT = """\
You are a news editor. Write a 3-sentence summary of this article.
Sentence 1: What happened. Sentence 2: Why it matters. Sentence 3: What comes next.
Rules: plain English, no more than 30 words each, factual only. No opinions.
Respond ONLY with JSON — no markdown, no explanation:
{{"sentence_1":"...","sentence_2":"...","sentence_3":"..."}}

Title: {title}
Content: {content}"""

CATEGORIZE_PROMPT = """\
Categorize this news article. Choose exactly ONE word from this list:
world, science, business, health, tech, sports, climate, arts

Title: {title}
Snippet: {snippet}

Reply with the single category word only. No punctuation."""


@dataclass
class Summary:
    sentence_1: str
    sentence_2: str
    sentence_3: str

    @property
    def full(self) -> str:
        return f"{self.sentence_1} {self.sentence_2} {self.sentence_3}".strip()


class GroqSummarizationService:

    def __init__(self) -> None:
        api_key = getattr(settings, "groq_api_key", "")
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set. Get a free key at console.groq.com")
        self._client = Groq(
            api_key=api_key,
            timeout=settings.groq_timeout_seconds,
            max_retries=settings.groq_max_retries,
        )
        self._model = settings.groq_model
        log.info("groq_client_ready", model=self._model)

    async def summarize(self, title: str, content: str) -> Summary:
        truncated = self._truncate(content, max_words=settings.groq_max_input_words)
        prompt = SUMMARIZE_PROMPT.format(title=title, content=truncated)
        raw = await asyncio.to_thread(
            self._invoke, prompt, max_tokens=settings.groq_max_output_tokens, json_mode=True
        )
        return self._parse_summary(raw)

    async def categorize(self, title: str, snippet: str) -> str:
        """Only called when heuristic returns uncertain 'world'."""
        valid  = {"world","science","business","health","tech","sports","climate","arts"}
        prompt = CATEGORIZE_PROMPT.format(title=title, snippet=snippet[:300])
        try:
            raw = await asyncio.to_thread(self._invoke, prompt, max_tokens=10)
            cat = raw.strip().lower().strip(".")
            return cat if cat in valid else "world"
        except Exception:
            return "world"

    def _invoke(
        self, prompt: str, max_tokens: int = 300, json_mode: bool = False
    ) -> str:
        request = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        if self._model.startswith("openai/gpt-oss-"):
            request["reasoning_effort"] = "low"
        response = self._client.chat.completions.create(**request)
        if not response.choices or not response.choices[0].message.content:
            raise ValueError("Groq returned an empty completion")
        return response.choices[0].message.content

    def _parse_summary(self, raw: str) -> Summary:
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        try:
            data = json.loads(clean)
        except json.JSONDecodeError as exc:
            raise ValueError("Groq returned invalid JSON") from exc

        expected = {"sentence_1", "sentence_2", "sentence_3"}
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError(
                "Groq summary must contain exactly sentence_1, sentence_2, sentence_3"
            )

        values = []
        for key in ("sentence_1", "sentence_2", "sentence_3"):
            value = data[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Groq summary field {key} must be a non-empty string")
            values.append(value.strip())

        return Summary(*values)

    def _sentences_fallback(self, text: str) -> Summary:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
        return Summary(
            sentence_1=sentences[0] if sentences       else text[:200],
            sentence_2=sentences[1] if len(sentences) > 1 else "",
            sentence_3=sentences[2] if len(sentences) > 2 else "",
        )

    def _fallback_summary(self, content: str) -> Summary:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", content.strip())
                     if len(s.strip()) > 20][:3]
        return Summary(
            sentence_1=sentences[0][:200] if sentences       else content[:200],
            sentence_2=sentences[1][:200] if len(sentences) > 1 else "",
            sentence_3=sentences[2][:200] if len(sentences) > 2 else "",
        )

    @staticmethod
    def _truncate(text: str, max_words: int) -> str:
        words = text.split()
        return text if len(words) <= max_words else " ".join(words[:max_words]) + "…"

    @staticmethod
    def heuristic_category(title: str) -> str:
        t = title.lower()
        if any(w in t for w in ["health","disease","vaccine","hospital","cancer","covid","drug","medical"]):
            return "health"
        if any(w in t for w in ["climate","carbon","emissions","renewable","drought","flood","wildfire"]):
            return "climate"
        if any(w in t for w in ["football","soccer","basketball","tennis","olympic","sport","nba","nfl","fifa"]):
            return "sports"
        if any(w in t for w in ["science","research","nasa","space","biology","physics"]):
            return "science"
        if any(w in t for w in ["economy","stock","gdp","trade","market","bank","inflation"]):
            return "business"
        if any(w in t for w in ["ai","tech","software","apple","google","chip","cyber","robot","startup"]):
            return "tech"
        if any(w in t for w in ["art","music","film","movie","culture","book","oscar"]):
            return "arts"
        return "world"
