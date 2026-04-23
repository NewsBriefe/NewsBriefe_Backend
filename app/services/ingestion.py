"""
News Ingestion Service

Sources:
  1. NewsAPI (paid tier recommended — free tier: 100 req/day)
  2. RSS feeds from major outlets (free, no key needed)

Deduplication strategy:
  - URL exact match (fastest)
  - Title fuzzy hash (catches rephrased duplicates)
  - TF-IDF cosine similarity on content (catches near-duplicates)

After ingestion, articles are queued for summarization.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import feedparser
import httpx
from newsapi import NewsApiClient
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from langdetect import detect, LangDetectException
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)


@dataclass
class RawArticle:
    """Intermediate representation before DB storage."""
    title: str
    content: str          # full text if available, else description
    description: str      # short excerpt
    url: str
    source_name: str
    source_url: str
    published_at: datetime
    image_url: str | None = None
    category: str = "world"
    country: str | None = None

    @property
    def content_hash(self) -> str:
        """SHA-256 of normalized title for fast dedup."""
        norm = re.sub(r"\W+", " ", self.title.lower()).strip()
        return hashlib.sha256(norm.encode()).hexdigest()[:16]


# ── RSS Feed sources ─────────────────────────────────────────
RSS_SOURCES: list[dict[str, Any]] = [
    # World
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml",          "source": "BBC",      "category": "world"},
    {"url": "http://feeds.reuters.com/reuters/topNews",              "source": "Reuters",  "category": "world"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml",            "source": "Al Jazeera","category": "world"},
    {"url": "https://apnews.com/rss",                               "source": "AP News",  "category": "world"},
    # Science
    {"url": "https://www.sciencedaily.com/rss/top.xml",             "source": "ScienceDaily","category": "science"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml","source": "NYT Science","category": "science"},
    # Health
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml","source": "NYT Health","category": "health"},
    {"url": "https://www.who.int/rss-feeds/news-english.xml",       "source": "WHO",      "category": "health"},
    # Business
    {"url": "https://feeds.bloomberg.com/markets/news.rss",         "source": "Bloomberg","category": "business"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml","source": "NYT Business","category": "business"},
    # Technology
    {"url": "https://feeds.arstechnica.com/arstechnica/index",      "source": "Ars Technica","category": "tech"},
    {"url": "https://www.theverge.com/rss/index.xml",               "source": "The Verge","category": "tech"},
    # Culture
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml","source": "NYT Arts", "category": "culture"},
]


class RSSFetcher:
    """Fetches articles from RSS feeds."""

    async def fetch_all(self) -> list[RawArticle]:
        articles: list[RawArticle] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for src in RSS_SOURCES:
                try:
                    fetched = await self._fetch_feed(client, src)
                    articles.extend(fetched)
                    log.debug("rss_fetched", source=src["source"], count=len(fetched))
                except Exception as e:
                    log.warning("rss_fetch_failed", source=src["source"], error=str(e))
        return articles

    async def _fetch_feed(
        self, client: httpx.AsyncClient, src: dict
    ) -> list[RawArticle]:
        r = await client.get(src["url"])
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        results = []
        for entry in feed.entries[:20]:  # cap per feed
            try:
                article = self._entry_to_raw(entry, src)
                if article:
                    results.append(article)
            except Exception:
                continue
        return results

    @staticmethod
    def _entry_to_raw(entry: Any, src: dict) -> RawArticle | None:
        url = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not url or not title or len(title) < 10:
            return None

        # Published date
        published_at = datetime.now(timezone.utc)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import time
            published_at = datetime.fromtimestamp(
                time.mktime(entry.published_parsed), tz=timezone.utc
            )

        # Content
        content = ""
        if hasattr(entry, "content") and entry.content:
            content = entry.content[0].get("value", "")
        description = entry.get("summary", entry.get("description", ""))
        content = content or description

        # Image
        image_url = None
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            image_url = entry.media_thumbnail[0].get("url")
        elif hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("image/"):
                    image_url = enc.get("href")
                    break

        # Extract country hint from source URL / title
        country = _extract_country_hint(title + " " + url)

        return RawArticle(
            title=title,
            content=_strip_html(content),
            description=_strip_html(description),
            url=url,
            source_name=src["source"],
            source_url=_domain(url),
            published_at=published_at,
            image_url=image_url,
            category=src["category"],
            country=country,
        )


class NewsAPIFetcher:
    """Fetches top headlines from NewsAPI."""

    def __init__(self) -> None:
        self._client: NewsApiClient | None = None
        if settings.newsapi_key:
            self._client = NewsApiClient(api_key=settings.newsapi_key)

    async def fetch_top_headlines(
        self,
        category: str | None = None,
        country: str | None = None,
        page_size: int = 50,
    ) -> list[RawArticle]:
        if not self._client:
            log.info("newsapi_skipped", reason="no api key configured")
            return []

        try:
            resp = self._client.get_top_headlines(
                category=category,
                country=country,
                page_size=page_size,
                language="en",
            )
            if resp.get("status") != "ok":
                return []
            return [
                self._article_to_raw(a, category or "world")
                for a in resp.get("articles", [])
                if a.get("url") and a.get("title")
            ]
        except Exception as e:
            log.error("newsapi_fetch_failed", error=str(e))
            return []

    @staticmethod
    def _article_to_raw(a: dict, category: str) -> RawArticle:
        source = a.get("source", {})
        published = a.get("publishedAt", "")
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            published_at = datetime.now(timezone.utc)

        return RawArticle(
            title=a.get("title", "").strip(),
            content=_strip_html(a.get("content") or a.get("description") or ""),
            description=_strip_html(a.get("description") or ""),
            url=a.get("url", ""),
            source_name=source.get("name", "Unknown"),
            source_url=_domain(a.get("url", "")),
            published_at=published_at,
            image_url=a.get("urlToImage"),
            category=category,
            country=_extract_country_hint(a.get("title", "")),
        )


# ── Deduplication ────────────────────────────────────────────

class Deduplicator:
    """
    Three-layer deduplication:
    1. Exact URL hash (O(1))
    2. Title content hash (O(n))
    3. TF-IDF cosine similarity on title+description (O(n²), batched)
    """

    def __init__(
        self,
        existing_hashes: set[str],
        existing_urls: set[str],
        similarity_threshold: float = settings.dedup_similarity_threshold,
    ):
        self._hashes = existing_hashes
        self._urls = existing_urls
        self._threshold = similarity_threshold

    def filter(self, articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
        """Returns (unique_articles, duped_count)."""
        # Layer 1: URL dedup
        after_url = [a for a in articles if a.url not in self._urls]
        duped_url = len(articles) - len(after_url)

        # Layer 2: Title hash dedup
        after_hash = [a for a in after_url if a.content_hash not in self._hashes]
        duped_hash = len(after_url) - len(after_hash)

        # Layer 3: TF-IDF similarity dedup (within the new batch)
        after_tfidf, duped_tfidf = self._tfidf_dedup(after_hash)

        total_duped = duped_url + duped_hash + duped_tfidf
        log.info(
            "deduplication_complete",
            total=len(articles),
            url_duped=duped_url,
            hash_duped=duped_hash,
            tfidf_duped=duped_tfidf,
            unique=len(after_tfidf),
        )
        return after_tfidf, total_duped

    def _tfidf_dedup(
        self, articles: list[RawArticle]
    ) -> tuple[list[RawArticle], int]:
        if len(articles) < 2:
            return articles, 0

        corpus = [f"{a.title} {a.description}" for a in articles]
        try:
            vectorizer = TfidfVectorizer(stop_words="english", max_features=500)
            matrix = vectorizer.fit_transform(corpus)
            sim = cosine_similarity(matrix)
        except Exception:
            return articles, 0

        keep = []
        dropped = set()
        for i in range(len(articles)):
            if i in dropped:
                continue
            keep.append(articles[i])
            for j in range(i + 1, len(articles)):
                if sim[i, j] >= self._threshold:
                    dropped.add(j)

        return keep, len(dropped)


# ── Utilities ────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-z]+;", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _domain(url: str) -> str:
    """Extract base domain from URL."""
    match = re.match(r"https?://([^/]+)", url)
    return f"https://{match.group(1)}" if match else url


_COUNTRY_KEYWORDS: dict[str, str] = {
    "usa": "USA",        "united states": "USA",  "america": "USA",
    "u.s.": "USA",       "uk": "UK",              "britain": "UK",
    "england": "UK",     "iran": "Iran",           "brazil": "Brazil",
    "india": "India",    "china": "China",          "japan": "Japan",
    "germany": "Germany","france": "France",        "russia": "Russia",
    "ethiopia": "Ethiopia","kenya": "Kenya",         "nigeria": "Nigeria",
    "egypt": "Egypt",    "indonesia": "Indonesia",  "mexico": "Mexico",
    "canada": "Canada",  "australia": "Australia",  "turkey": "Turkey",
    "saudi": "Saudi Arabia","south africa": "South Africa",
    "ukraine": "Ukraine","israel": "Israel",        "palestine": "Palestine",
    "pakistan": "Pakistan","bangladesh": "Bangladesh",
}


def _extract_country_hint(text: str) -> str | None:
    lower = text.lower()
    for keyword, country in _COUNTRY_KEYWORDS.items():
        if keyword in lower:
            return country
    return None


def detect_language(text: str) -> str:
    try:
        return detect(text[:500]) or "en"
    except LangDetectException:
        return "en"
