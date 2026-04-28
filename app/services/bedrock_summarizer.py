"""
AWS Bedrock — DeepSeek R1 Summarization Provider

Alternative AI provider to Claude (Anthropic).
Uses DeepSeek R1 via AWS Bedrock with boto3.

Usage:
  Set AI_PROVIDER=bedrock in your environment to use this instead of Claude.
  Set AI_PROVIDER=claude (default) to use the Anthropic Claude provider.

Required env vars when AI_PROVIDER=bedrock:
  AWS_ACCESS_KEY_ID      — your AWS access key
  AWS_SECRET_ACCESS_KEY  — your AWS secret key
  AWS_REGION             — e.g. us-east-1 (must support Bedrock)
  BEDROCK_MODEL_ID       — e.g. us.deepseek.r1-v1:0

The interface is identical to SummarizationService in summarizer.py
so they can be swapped without changing any other code.
"""

import json
import re
from dataclasses import dataclass
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, NoCredentialsError
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)

# ── Prompt templates ─────────────────────────────────────────
# DeepSeek R1 uses a different prompt format than Claude.
# <think> tags are the model's internal reasoning — we strip them from output.

SUMMARIZE_PROMPT = """\
You are NewsBrief's editorial AI. Read the following news article and write \
a 3-sentence neutral summary. Each sentence must be:
- Plain English, under 30 words
- Factual — no opinion, no spin, no filler
- Self-contained

Sentence 1 — What happened: the core event in one clear sentence.
Sentence 2 — Why it matters: the significance or impact.
Sentence 3 — What comes next: the likely next development or open question.

Respond ONLY with valid JSON in this exact shape — no text outside it:
{{
  "sentence_1": "...",
  "sentence_2": "...",
  "sentence_3": "..."
}}

Article:
Title: {title}

Content:
{content}"""

CATEGORIZE_PROMPT = """\
You are a news categorizer. Given the article title and snippet below, \
return the single best category from this list:
world, science, business, health, tech, sports, climate, arts

Respond with ONLY the category word, nothing else.

Title: {title}
Snippet: {snippet}"""


@dataclass
class Summary:
    """Identical shape to summarizer.py Summary so they're interchangeable."""
    sentence_1: str
    sentence_2: str
    sentence_3: str

    @property
    def full(self) -> str:
        return f"{self.sentence_1} {self.sentence_2} {self.sentence_3}".strip()


class BedrockSummarizationService:
    """
    Drop-in replacement for SummarizationService using AWS Bedrock + DeepSeek R1.

    DeepSeek R1 on Bedrock uses the 'invoke_model' API with a messages-style body.
    The model returns a <think>...</think> block followed by the actual response —
    we strip the think block before parsing.
    """

    def __init__(self) -> None:
        self._model_id: str = getattr(settings, "bedrock_model_id", "us.deepseek.r1-v1:0")

        try:
            self._client = boto3.client(
                service_name="bedrock-runtime",
                region_name=getattr(settings, "aws_region", "us-east-1"),
                aws_access_key_id=getattr(settings, "aws_access_key_id", None),
                aws_secret_access_key=getattr(settings, "aws_secret_access_key", None),
                config=BotoConfig(
                    read_timeout=60,
                    connect_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
            log.info("bedrock_client_ready", model=self._model_id)
        except NoCredentialsError:
            log.error("bedrock_no_credentials")
            raise

    # ── Public API (same as SummarizationService) ─────────────

    async def summarize(self, title: str, content: str) -> Summary:
        import asyncio
        truncated = self._truncate(content, max_words=3000)
        prompt = SUMMARIZE_PROMPT.format(title=title, content=truncated)
        try:
            raw = await asyncio.to_thread(self._invoke, prompt, max_tokens=600)
            return self._parse_summary(raw)
        except Exception as e:
            log.error("bedrock_summarize_failed", error=str(e))
            return self._fallback_summary(content)

    async def categorize(self, title: str, snippet: str) -> str:
        import asyncio
        valid = {"world", "science", "business", "health", "tech", "sports", "climate", "arts"}
        prompt = CATEGORIZE_PROMPT.format(title=title, snippet=snippet[:400])
        try:
            raw = await asyncio.to_thread(self._invoke, prompt, max_tokens=10)
            cat = self._strip_think(raw).strip().lower()
            return cat if cat in valid else "world"
        except Exception as e:
            log.warning("bedrock_categorize_failed", error=str(e))
            return self._heuristic_category(title)

    async def summarize_batch(self, articles: list[tuple[str, str]]) -> list[Summary]:
        results = []
        for title, content in articles:
            results.append(await self.summarize(title, content))
        return results

    # ── Bedrock invocation ────────────────────────────────────

    def _invoke(self, prompt: str, max_tokens: int = 512) -> str:
        """
        Synchronous Bedrock call — always wrap in asyncio.to_thread().
        DeepSeek R1 on Bedrock uses the converse-style messages body.
        """
        body = json.dumps({
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,   # low temp = more consistent factual output
        })

        try:
            response = self._client.invoke_model(
                modelId=self._model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            result = json.loads(response["body"].read())

            # DeepSeek R1 response shape:
            # {"choices": [{"message": {"content": "<think>...</think>\n{...}"}}]}
            content = (
                result.get("choices", [{}])[0]
                      .get("message", {})
                      .get("content", "")
            )
            return self._strip_think(content)

        except ClientError as e:
            code = e.response["Error"]["Code"]
            log.error("bedrock_client_error", code=code, error=str(e))
            raise

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _strip_think(text: str) -> str:
        """Remove DeepSeek R1's <think>...</think> reasoning block."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def _parse_summary(self, raw: str) -> Summary:
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        try:
            data = json.loads(clean)
            return Summary(
                sentence_1=data.get("sentence_1", ""),
                sentence_2=data.get("sentence_2", ""),
                sentence_3=data.get("sentence_3", ""),
            )
        except (json.JSONDecodeError, KeyError):
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
        return text if len(words) <= max_words else " ".join(words[:max_words]) + "…"

    @staticmethod
    def _heuristic_category(title: str) -> str:
        t = title.lower()
        if any(w in t for w in ["health", "disease", "vaccine", "hospital", "cancer", "covid"]):
            return "health"
        if any(w in t for w in ["climate", "carbon", "emissions", "renewable", "drought", "flood"]):
            return "climate"
        if any(w in t for w in ["football", "soccer", "basketball", "tennis", "olympic", "sport", "nba", "nfl"]):
            return "sports"
        if any(w in t for w in ["science", "research", "nasa", "space", "biology", "physics"]):
            return "science"
        if any(w in t for w in ["economy", "stock", "gdp", "trade", "bank", "inflation"]):
            return "business"
        if any(w in t for w in ["ai", "tech", "software", "apple", "google", "chip", "cyber"]):
            return "tech"
        if any(w in t for w in ["art", "music", "film", "movie", "culture", "book", "oscar"]):
            return "arts"
        return "world"