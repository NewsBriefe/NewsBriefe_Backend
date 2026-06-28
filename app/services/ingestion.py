"""
News Ingestion Service

FIX: NewsAPI source allowlist added — prevents low-quality publishers
(STYLECASTER, Minecraft.net, SB Nation, etc.) from flooding the DB
with 3-word stubs that waste Bedrock calls.

Only articles from TRUSTED_SOURCES pass the filter. RSS feeds already
use a curated source list so they are not filtered.
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

from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)


# ── NewsAPI source allowlist ──────────────────────────────────
# Only articles from these sources are ingested via NewsAPI.
# Add new sources here as needed.
TRUSTED_SOURCES: set[str] = {
    # Wire services — highest quality, always include
    "Reuters", "Associated Press", "AP News", "AFP",
    # International broadcast
    "BBC News", "BBC Sport", "Al Jazeera", "DW News",
    "France 24", "NHK World",
    # US national
    "The New York Times", "Washington Post", "The Wall Street Journal",
    "NPR", "PBS NewsHour", "Axios", "The Hill", "Politico",
    "USA Today", "NBC News", "ABC News", "CBS News", "CNN",
    # Business / finance
    "Bloomberg", "Financial Times", "Forbes", "Business Insider",
    "CNBC", "MarketWatch",
    # Tech
    "The Verge", "Ars Technica", "Wired", "TechCrunch",
    "Engadget", "MIT Technology Review",
    # Science / health
    "ScienceDaily", "NASA", "WHO", "Nature", "New Scientist",
    "Medical News Today", "Healthline",
    # Sports
    "ESPN", "Sports Illustrated",
    # Climate / environment
    "Inside Climate News", "Grist", "Yale Environment 360",
    # International
    "The Guardian", "The Independent", "Sky News",
    "South China Morning Post", "Times of India",
    "Al-Monitor", "Middle East Eye",
}


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


RSS_SOURCES: list[dict[str, Any]] = [
    # World
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml",               "source": "BBC",           "category": "world"},
    {"url": "https://feeds.reuters.com/reuters/topNews",                  "source": "Reuters",       "category": "world"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml",                 "source": "Al Jazeera",    "category": "world"},
    {"url": "https://apnews.com/rss",                                     "source": "AP News",       "category": "world"},
    # Science
    {"url": "https://www.sciencedaily.com/rss/top.xml",                   "source": "ScienceDaily",  "category": "science"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",   "source": "NYT Science",   "category": "science"},
    # Health
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",    "source": "NYT Health",    "category": "health"},
    {"url": "https://www.who.int/rss-feeds/news-english.xml",             "source": "WHO",           "category": "health"},
    # Business
    {"url": "https://feeds.bloomberg.com/markets/news.rss",               "source": "Bloomberg",     "category": "business"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",  "source": "NYT Business",  "category": "business"},
    # Tech
    {"url": "https://feeds.arstechnica.com/arstechnica/index",            "source": "Ars Technica",  "category": "tech"},
    {"url": "https://www.theverge.com/rss/index.xml",                     "source": "The Verge",     "category": "tech"},
    # Sports
    {"url": "https://www.espn.com/espn/rss/news",                         "source": "ESPN",          "category": "sports"},
    {"url": "https://feeds.bbci.co.uk/sport/rss.xml",                     "source": "BBC Sport",     "category": "sports"},
    # Climate
    {"url": "https://insideclimatenews.org/feed/",                        "source": "Inside Climate News", "category": "climate"},
    {"url": "https://grist.org/feed/",                                    "source": "Grist",         "category": "climate"},
    # Arts
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml",      "source": "NYT Arts",      "category": "arts"},
    {"url": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "source": "BBC Arts",   "category": "arts"},
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
        log.info("rss_all_fetched", total=len(articles))
        return articles

    async def _fetch_feed(self, client: httpx.AsyncClient, src: dict) -> list[RawArticle]:
        r = await client.get(src["url"])
        r.raise_for_status()
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
    NEWSAPI_CATEGORIES = [
        "general", "business", "entertainment",
        "health", "science", "sports", "technology"
    ]

    def __init__(self) -> None:
        self._client: NewsApiClient | None = None
        if settings.newsapi_key:
            self._client = NewsApiClient(api_key=settings.newsapi_key)
            log.info("newsapi_client_ready")
        else:
            log.warning("newsapi_key_missing")

    async def fetch_top_headlines(self) -> list[RawArticle]:
        if not self._client:
            return []

        all_articles: list[RawArticle] = []
        for api_category in self.NEWSAPI_CATEGORIES:
            try:
                resp = await asyncio.to_thread(
                    self._client.get_top_headlines,
                    category=api_category,
                    page_size=20,
                    language="en",
                )
                if resp.get("status") != "ok":
                    continue

                internal_cat = _newsapi_category_map(api_category)
                accepted = 0
                rejected = 0
                for a in resp.get("articles", []):
                    source_name = a.get("source", {}).get("name", "")
                    title       = a.get("title", "")

                    # Skip removed articles
                    if "[Removed]" in title or not a.get("url"):
                        rejected += 1
                        continue

                    # SOURCE FILTER: only accept trusted publishers
                    if source_name not in TRUSTED_SOURCES:
                        rejected += 1
                        log.debug("newsapi_source_rejected", source=source_name)
                        continue

                    all_articles.append(self._article_to_raw(a, internal_cat))
                    accepted += 1

                log.debug("newsapi_category_done",
                          category=api_category, accepted=accepted, rejected=rejected)

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


class Deduplicator:
    def __init__(self, existing_hashes: set[str], existing_urls: set[str]):
        self._hashes = existing_hashes
        self._urls   = existing_urls

    def filter(self, articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
        unique      = []
        seen_urls   = set(self._urls)
        seen_hashes = set(self._hashes)
        duped       = 0

        for a in articles:
            if a.url in seen_urls or a.content_hash in seen_hashes:
                duped += 1
                continue
            unique.append(a)
            seen_urls.add(a.url)
            seen_hashes.add(a.content_hash)

        log.info("dedup_done", total=len(articles), unique=len(unique), duped=duped)
        return unique, duped


# ── Helpers ──────────────────────────────────────────────────

def _newsapi_category_map(newsapi_cat: str) -> str:
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
    "usa": "USA", "united states": "USA", "america": "USA", "u.s.": "USA",
    "uk": "UK", "britain": "UK", "england": "UK",
    "ethiopia": "Ethiopia", "kenya": "Kenya", "nigeria": "Nigeria",
    "egypt": "Egypt", "ghana": "Ghana", "south africa": "South Africa",
    "iran": "Iran", "israel": "Israel", "palestine": "Palestine",
    "ukraine": "Ukraine", "russia": "Russia", "china": "China",
    "india": "India", "japan": "Japan", "brazil": "Brazil",
    "germany": "Germany", "france": "France", "turkey": "Turkey",
    "pakistan": "Pakistan", "bangladesh": "Bangladesh",
    "indonesia": "Indonesia", "mexico": "Mexico",
    "canada": "Canada", "australia": "Australia",
    "saudi": "Saudi Arabia",
}


def _extract_country_hint(text: str) -> str | None:
    lower = text.lower()
    for keyword, country in _COUNTRY_KEYWORDS.items():
        if keyword in lower:
            return country
    return None
