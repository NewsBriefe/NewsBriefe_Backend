"""
Unit tests — no DB or network required.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from app.services.summarizer import SummarizationService, Summary
from app.services.ingestion import Deduplicator, RawArticle, _strip_html, _extract_country_hint


# ─────────────────────────────────────────────────────────────
#  Summarizer tests
# ─────────────────────────────────────────────────────────────

class TestSummarizationService:
    def setup_method(self):
        self.service = SummarizationService()

    def test_parse_valid_json(self):
        raw = '{"sentence_1": "A.", "sentence_2": "B.", "sentence_3": "C."}'
        summary = self.service._parse_summary(raw)
        assert summary.sentence_1 == "A."
        assert summary.sentence_2 == "B."
        assert summary.sentence_3 == "C."

    def test_parse_with_markdown_fences(self):
        raw = '```json\n{"sentence_1": "X.", "sentence_2": "Y.", "sentence_3": "Z."}\n```'
        summary = self.service._parse_summary(raw)
        assert summary.sentence_1 == "X."

    def test_parse_invalid_json_falls_back(self):
        raw = "This is sentence one. This is sentence two. This is sentence three."
        summary = self.service._parse_summary(raw)
        assert len(summary.sentence_1) > 0

    def test_fallback_summary_from_content(self):
        content = "First sentence. Second sentence here. Third sentence at end."
        summary = self.service._fallback_summary(content)
        assert "First" in summary.sentence_1

    def test_truncate(self):
        text = " ".join(["word"] * 5000)
        result = self.service._truncate(text, max_words=3000)
        assert len(result.split()) <= 3001  # +1 for the ellipsis marker

    def test_heuristic_category_health(self):
        cat = self.service._heuristic_category("New vaccine approved for COVID disease treatment")
        assert cat == "health"

    def test_heuristic_category_tech(self):
        cat = self.service._heuristic_category("Apple releases new AI chip for iPhone")
        assert cat == "tech"

    def test_heuristic_category_science(self):
        cat = self.service._heuristic_category("NASA discovers water on distant planet")
        assert cat == "science"

    def test_heuristic_category_fallback(self):
        cat = self.service._heuristic_category("Random words with no keywords here")
        assert cat == "world"

    @pytest.mark.asyncio
    async def test_summarize_calls_claude(self):
        mock_content = MagicMock()
        mock_content.text = '{"sentence_1":"A.","sentence_2":"B.","sentence_3":"C."}'
        mock_message = MagicMock()
        mock_message.content = [mock_content]

        with patch.object(
            self.service._client.messages, "create",
            new=AsyncMock(return_value=mock_message)
        ):
            summary = await self.service.summarize("Test title", "Test content")
        assert summary.sentence_1 == "A."
        assert summary.full == "A. B. C."

    @pytest.mark.asyncio
    async def test_summarize_falls_back_on_api_error(self):
        import anthropic
        with patch.object(
            self.service._client.messages, "create",
            new=AsyncMock(side_effect=Exception("network error"))
        ):
            summary = await self.service.summarize("Title", "Content sentence one. Sentence two.")
        # Should return a non-empty fallback
        assert isinstance(summary, Summary)
        assert len(summary.sentence_1) > 0


# ─────────────────────────────────────────────────────────────
#  Deduplication tests
# ─────────────────────────────────────────────────────────────

def _make_raw(title: str, url: str, description: str = "") -> RawArticle:
    return RawArticle(
        title=title,
        content=description or title,
        description=description or title,
        url=url,
        source_name="Test",
        source_url="https://test.com",
        published_at=datetime.now(timezone.utc),
    )


class TestDeduplicator:
    def test_url_dedup(self):
        existing_urls = {"https://example.com/article-1"}
        dedup = Deduplicator(set(), existing_urls)
        articles = [
            _make_raw("Title 1", "https://example.com/article-1"),
            _make_raw("Title 2", "https://example.com/article-2"),
        ]
        unique, duped = dedup.filter(articles)
        assert len(unique) == 1
        assert duped == 1

    def test_hash_dedup(self):
        art = _make_raw("Peace talks resume after pause", "https://new.com/1")
        existing_hashes = {art.content_hash}
        dedup = Deduplicator(existing_hashes, set())
        unique, duped = dedup.filter([art])
        assert len(unique) == 0
        assert duped == 1

    def test_tfidf_dedup_similar(self):
        dedup = Deduplicator(set(), set(), similarity_threshold=0.7)
        articles = [
            _make_raw(
                "US and China sign historic trade deal",
                "https://a.com/1",
                "The United States and China signed a major trade agreement today.",
            ),
            _make_raw(
                "Historic trade deal signed between US and China",
                "https://b.com/2",
                "United States China trade agreement signed in major deal today.",
            ),
        ]
        unique, duped = dedup.filter(articles)
        assert len(unique) == 1
        assert duped == 1

    def test_tfidf_dedup_different(self):
        dedup = Deduplicator(set(), set(), similarity_threshold=0.7)
        articles = [
            _make_raw("WHO warns about new disease outbreak", "https://a.com/1",
                      "World Health Organization issued a warning about disease spreading."),
            _make_raw("Tech giants report record profits", "https://b.com/2",
                      "Apple Google Meta all reported record earnings this quarter."),
        ]
        unique, duped = dedup.filter(articles)
        assert len(unique) == 2

    def test_empty_input(self):
        dedup = Deduplicator(set(), set())
        unique, duped = dedup.filter([])
        assert unique == []
        assert duped == 0


# ─────────────────────────────────────────────────────────────
#  Utility tests
# ─────────────────────────────────────────────────────────────

class TestUtilities:
    def test_strip_html(self):
        html = "<p>Hello <b>world</b> &amp; more</p>"
        result = _strip_html(html)
        assert "<" not in result
        assert "Hello" in result
        assert "world" in result

    def test_strip_html_empty(self):
        assert _strip_html("") == ""

    def test_extract_country_usa(self):
        assert _extract_country_hint("US economy grows in Q1") == "USA"
        assert _extract_country_hint("united states president speaks") == "USA"

    def test_extract_country_brazil(self):
        assert _extract_country_hint("Brazil deforestation drops") == "Brazil"

    def test_extract_country_none(self):
        assert _extract_country_hint("Global stock markets rise") is None
