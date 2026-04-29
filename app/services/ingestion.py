"""
News Ingestion Service

Sources:
  1. RSS feeds from major outlets (free, no key needed)
  2. NewsAPI (free tier: 100 req/day)

FIX: NewsApiClient.get_top_headlines() is synchronous and blocks the
     async event loop. All calls are now wrapped in asyncio.to_thread().

FIX: RSS category "culture" renamed to "arts" to match Flutter's categories.
     Flutter expects: world science business health tech sports climate arts
"""
import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from newsapi import NewsApiClient
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)


@dataclass
class RawArticle:
    title: str
    content: str
    description: str
    url: str
    source_name: str
    source_url: str
    published_at: datetime
    image_url: str | None = None
    category: str = "world"
    country: str | None = None

    @property
    def content_hash(self) -> str:
        norm = re.sub(r"\W+", " ", self.title.lower()).strip()
        return hashlib.sha256(norm.encode()).hexdigest()[:16]


# FIX: "culture" → "arts" to match Flutter's category list
RSS_SOURCES: list[dict[str, Any]] = [
    # World
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml",              "source": "BBC",          "category": "world"},
    {"url": "https://feeds.reuters.com/reuters/topNews",                 "source": "Reuters",      "category": "world"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml",                "source": "Al Jazeera",   "category": "world"},
    {"url": "https://apnews.com/rss",                                    "source": "AP News",      "category": "world"},
    # Science
    {"url": "https://www.sciencedaily.com/rss/top.xml",                  "source": "ScienceDaily", "category": "science"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",  "source": "NYT Science",  "category": "science"},
    # Health
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",   "source": "NYT Health",   "category": "health"},
    {"url": "https://www.who.int/rss-feeds/news-english.xml",            "source": "WHO",          "category": "health"},
    # Business
    {"url": "https://feeds.bloomberg.com/markets/news.rss",              "source": "Bloomberg",    "category": "business"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "source": "NYT Business", "category": "business"},
    # Tech
    {"url": "https://feeds.arstechnica.com/arstechnica/index",           "source": "Ars Technica", "category": "tech"},
    {"url": "https://www.theverge.com/rss/index.xml",                    "source": "The Verge",    "category": "tech"},
    # Sports — FIX: added missing sports RSS sources
    {"url": "https://www.espn.com/espn/rss/news",                        "source": "ESPN",         "category": "sports"},
    {"url": "https://feeds.bbci.co.uk/sport/rss.xml",                    "source": "BBC Sport",    "category": "sports"},
    # Climate — FIX: added missing climate RSS sources
    {"url": "https://insideclimatenews.org/feed/",                       "source": "Inside Climate News", "category": "climate"},
    {"url": "https://grist.org/feed/",                                   "source": "Grist",        "category": "climate"},
    # Arts — FIX: was "culture", must be "arts" to match Flutter
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml",    "source": "NYT Arts",     "category": "arts"},
    {"url": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "source": "BBC Arts", "category": "arts"},
]


class RSSFetcher:
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
        log.info("rss_all_fetched", total=len(articles), sources=len(RSS_SOURCES))
        return articles

    async def _fetch_feed(self, client: httpx.AsyncClient, src: dict) -> list[RawArticle]:
        r = await client.get(src["url"])
        r.raise_for_status()
        # feedparser.parse is CPU-bound — run in thread
        feed = await asyncio.to_thread(feedparser.parse, r.text)
        results = []
        for entry in feed.entries[:20]:
            try:
                article = self._entry_to_raw(entry, src)
                if article:
                    results.append(article)
            except Exception:
                continue
        return results

    @staticmethod
    def _entry_to_raw(entry: Any, src: dict) -> RawArticle | None:
        url   = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not url or not title or len(title) < 10:
            return None

        published_at = datetime.now(timezone.utc)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import time
            published_at = datetime.fromtimestamp(
                time.mktime(entry.published_parsed), tz=timezone.utc
            )

        content     = ""
        description = entry.get("summary", entry.get("description", ""))
        if hasattr(entry, "content") and entry.content:
            content = entry.content[0].get("value", "")
        content = content or description

        image_url = None
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            image_url = entry.media_thumbnail[0].get("url")
        elif hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("image/"):
                    image_url = enc.get("href")
                    break

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
            country=_extract_country_hint(title + " " + url),
        )


class NewsAPIFetcher:
    """
    FIX: newsapi-python's get_top_headlines() is synchronous.
    All calls are now wrapped in asyncio.to_thread() so the async
    event loop is never blocked during the HTTP call.
    """

    # NewsAPI category names (their API uses these exact strings)
    NEWSAPI_CATEGORIES = ["general", "business", "entertainment", "health", "science", "sports", "technology"]

    def __init__(self) -> None:
        self._client: NewsApiClient | None = None
        if settings.newsapi_key:
            self._client = NewsApiClient(api_key=settings.newsapi_key)
            log.info("newsapi_client_ready")
        else:
            log.warning("newsapi_key_missing", hint="Set NEWSAPI_KEY env var to enable NewsAPI fetching")

    async def fetch_top_headlines(self) -> list[RawArticle]:
        if not self._client:
            return []

        all_articles: list[RawArticle] = []

        # Fetch across all NewsAPI categories to get broad coverage
        for api_category in self.NEWSAPI_CATEGORIES:
            try:
                # FIX: wrap synchronous call in asyncio.to_thread()
                resp = await asyncio.to_thread(
                    self._client.get_top_headlines,
                    category=api_category,
                    page_size=20,
                    language="en",
                )
                if resp.get("status") != "ok":
                    log.warning("newsapi_bad_response", category=api_category, resp=resp.get("status"))
                    continue

                articles = resp.get("articles", [])
                # Map NewsAPI category → our internal category
                internal_cat = _newsapi_category_map(api_category)
                parsed = [
                    self._article_to_raw(a, internal_cat)
                    for a in articles
                    if a.get("url") and a.get("title") and "[Removed]" not in a.get("title", "")
                ]
                all_articles.extend(parsed)
                log.debug("newsapi_category_fetched", category=api_category, count=len(parsed))

            except Exception as e:
                log.error("newsapi_fetch_failed", category=api_category, error=str(e))
                continue

        log.info("newsapi_all_fetched", total=len(all_articles))
        return all_articles

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


# ── Deduplication ─────────────────────────────────────────────

class Deduplicator:
    def __init__(
        self,
        existing_hashes: set[str],
        existing_urls: set[str],
        similarity_threshold: float = settings.dedup_similarity_threshold,
    ):
        self._hashes    = existing_hashes
        self._urls      = existing_urls
        self._threshold = similarity_threshold

    def filter(self, articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
        after_url  = [a for a in articles if a.url not in self._urls]
        duped_url  = len(articles) - len(after_url)

        after_hash = [a for a in after_url if a.content_hash not in self._hashes]
        duped_hash = len(after_url) - len(after_hash)

        after_tfidf, duped_tfidf = self._tfidf_dedup(after_hash)

        total_duped = duped_url + duped_hash + duped_tfidf
        log.info(
            "deduplication_complete",
            total=len(articles), url_duped=duped_url,
            hash_duped=duped_hash, tfidf_duped=duped_tfidf,
            unique=len(after_tfidf),
        )
        return after_tfidf, total_duped

    def _tfidf_dedup(self, articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
        if len(articles) < 2:
            return articles, 0
        corpus = [f"{a.title} {a.description}" for a in articles]
        try:
            matrix = TfidfVectorizer(stop_words="english", max_features=500).fit_transform(corpus)
            sim    = cosine_similarity(matrix)
        except Exception:
            return articles, 0

        keep    = []
        dropped: set[int] = set()
        for i in range(len(articles)):
            if i in dropped:
                continue
            keep.append(articles[i])
            for j in range(i + 1, len(articles)):
                if sim[i, j] >= self._threshold:
                    dropped.add(j)
        return keep, len(dropped)


# ── Helpers ───────────────────────────────────────────────────

def _newsapi_category_map(newsapi_cat: str) -> str:
    """Map NewsAPI category names to our internal category names."""
    return {
        "general":       "world",
        "business":      "business",
        "entertainment": "arts",
        "health":        "health",
        "science":       "science",
        "sports":        "sports",
        "technology":    "tech",
    }.get(newsapi_cat, "world")


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-z]+;", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _domain(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return f"https://{match.group(1)}" if match else url


_COUNTRY_KEYWORDS: dict[str, str] = {
    "usa": "USA",           "united states": "USA",     "america": "USA",
    "u.s.": "USA",          "uk": "UK",                 "britain": "UK",
    "england": "UK",        "iran": "Iran",             "brazil": "Brazil",
    "india": "India",       "china": "China",           "japan": "Japan",
    "germany": "Germany",   "france": "France",         "russia": "Russia",
    "ethiopia": "Ethiopia", "kenya": "Kenya",           "nigeria": "Nigeria",
    "egypt": "Egypt",       "indonesia": "Indonesia",   "mexico": "Mexico",
    "canada": "Canada",     "australia": "Australia",   "turkey": "Turkey",
    "saudi": "Saudi Arabia","south africa": "South Africa",
    "ukraine": "Ukraine",   "israel": "Israel",         "palestine": "Palestine",
    "pakistan": "Pakistan", "bangladesh": "Bangladesh",
}


def _extract_country_hint(text: str) -> str | None:
    lower = text.lower()
    for keyword, country in _COUNTRY_KEYWORDS.items():
        if keyword in lower:
            return country
    return None
