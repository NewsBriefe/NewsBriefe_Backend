"""
Integration tests — spins up the real FastAPI app with
an in-memory SQLite DB and mocked external services.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.main import app
from app.core.database import get_db, Base
from app.core.cache import get_redis
from app.core.config import get_settings
from app.models.orm import Article, ArticleTranslation

settings = get_settings()

# ── Test DB (SQLite in-memory) ────────────────────────────────
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Mock Redis ────────────────────────────────────────────────
class MockRedis:
    def __init__(self):
        self._store = {}

    async def get(self, key):
        return self._store.get(key)

    async def setex(self, key, ttl, value):
        self._store[key] = value

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    async def keys(self, pattern):
        import fnmatch
        return [k for k in self._store if fnmatch.fnmatch(k, pattern)]

    async def exists(self, key):
        return int(key in self._store)

    async def ping(self):
        return True


mock_redis = MockRedis()


async def override_get_redis():
    return mock_redis


# ── Fixtures ──────────────────────────────────────────────────
@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    mock_redis._store.clear()


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def db_session():
    async with TestSessionLocal() as session:
        yield session


# ── Helpers ───────────────────────────────────────────────────
def make_article(**kwargs) -> Article:
    defaults = dict(
        id="test-article-001",
        title_en="Peace talks resume after three-year pause",
        summary_en="Both sides agreed to a 90-day ceasefire in Geneva. Economic pressure was the key motivator. A deal may be signed by March.",
        source_name="Reuters",
        source_url="https://reuters.com",
        original_url="https://reuters.com/article/001",
        category="world",
        published_at=datetime.now(timezone.utc),
        fetched_at=datetime.now(timezone.utc),
        is_summarized=True,
        is_active=True,
    )
    defaults.update(kwargs)
    return Article(**defaults)


# ─────────────────────────────────────────────────────────────
#  /v1/articles  tests
# ─────────────────────────────────────────────────────────────

class TestGetArticles:
    async def test_returns_empty_list_when_no_articles(self, client: AsyncClient):
        r = await client.get("/v1/articles?lang=en")
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["has_more"] is False

    async def test_returns_articles_in_english(self, client: AsyncClient, db_session: AsyncSession):
        db_session.add(make_article())
        await db_session.commit()

        r = await client.get("/v1/articles?lang=en")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Peace talks resume after three-year pause"
        assert item["language_code"] == "en"

    async def test_returns_translated_summary_when_available(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        article = make_article()
        db_session.add(article)
        await db_session.flush()

        translation = ArticleTranslation(
            article_id=article.id,
            language_code="fr",
            title="Les pourparlers de paix reprennent",
            summary="Les deux parties ont convenu d'un cessez-le-feu. La pression économique était la clé. Un accord pourrait être signé en mars.",
            translation_provider="deepl",
        )
        db_session.add(translation)
        await db_session.commit()

        r = await client.get("/v1/articles?lang=fr")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Les pourparlers de paix reprennent"
        assert item["language_code"] == "fr"

    async def test_falls_back_to_english_when_no_translation(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article())
        await db_session.commit()

        r = await client.get("/v1/articles?lang=am")
        assert r.status_code == 200
        data = r.json()
        item = data["items"][0]
        assert item["language_code"] == "en"  # fallback

    async def test_filters_by_category(self, client: AsyncClient, db_session: AsyncSession):
        db_session.add(make_article(id="art-1", original_url="https://a.com/1", category="world"))
        db_session.add(make_article(id="art-2", original_url="https://a.com/2", category="science"))
        await db_session.commit()

        r = await client.get("/v1/articles?lang=en&category=science")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["category"] == "science"

    async def test_pagination(self, client: AsyncClient, db_session: AsyncSession):
        for i in range(5):
            db_session.add(make_article(
                id=f"art-{i}",
                original_url=f"https://a.com/{i}",
                title_en=f"Article {i}",
            ))
        await db_session.commit()

        r = await client.get("/v1/articles?lang=en&page=1&per_page=2")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 2
        assert data["has_more"] is True
        assert data["total"] == 5

    async def test_unsummarized_articles_excluded(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article(is_summarized=False))
        await db_session.commit()

        r = await client.get("/v1/articles?lang=en")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    async def test_cache_is_used_on_second_request(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article())
        await db_session.commit()

        r1 = await client.get("/v1/articles?lang=en")
        r2 = await client.get("/v1/articles?lang=en")
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Both should return same data (second from cache)
        assert r1.json()["total"] == r2.json()["total"]


# ─────────────────────────────────────────────────────────────
#  /v1/articles/{id}  tests
# ─────────────────────────────────────────────────────────────

class TestGetArticleById:
    async def test_returns_404_for_missing_article(self, client: AsyncClient):
        r = await client.get("/v1/articles/nonexistent-id?lang=en")
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"

    async def test_returns_article_with_sentences(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article())
        await db_session.commit()

        r = await client.get("/v1/articles/test-article-001?lang=en")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "test-article-001"
        assert data["sentence_1"] != ""
        assert data["sentence_2"] != ""
        assert data["sentence_3"] != ""


# ─────────────────────────────────────────────────────────────
#  /v1/articles/search  tests
# ─────────────────────────────────────────────────────────────

class TestSearch:
    async def test_search_returns_matching_articles(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article(
            id="art-1",
            original_url="https://a.com/1",
            title_en="Peace talks resume after three-year pause",
        ))
        db_session.add(make_article(
            id="art-2",
            original_url="https://a.com/2",
            title_en="Stock markets reach all-time high",
            category="business",
        ))
        await db_session.commit()

        r = await client.get("/v1/articles/search?q=peace&lang=en")
        assert r.status_code == 200

    async def test_search_requires_query(self, client: AsyncClient):
        r = await client.get("/v1/articles/search?lang=en")
        assert r.status_code == 422

    async def test_search_with_country_filter(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article(
            id="art-1", original_url="https://a.com/1", country="Brazil"
        ))
        db_session.add(make_article(
            id="art-2", original_url="https://a.com/2", country="Japan"
        ))
        await db_session.commit()

        r = await client.get("/v1/articles/search?q=peace&country=Brazil&lang=en")
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────
#  /v1/articles/{id}/translate  tests
# ─────────────────────────────────────────────────────────────

class TestTranslate:
    async def test_returns_existing_translation(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        article = make_article()
        db_session.add(article)
        await db_session.flush()
        db_session.add(ArticleTranslation(
            article_id=article.id,
            language_code="fr",
            title="Titre en français",
            summary="Résumé en français. Deuxième phrase. Troisième phrase.",
            translation_provider="deepl",
        ))
        await db_session.commit()

        r = await client.post(
            f"/v1/articles/{article.id}/translate",
            json={"target_language": "fr"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["language_code"] == "fr"
        assert data["title"] == "Titre en français"
        assert data["provider"] == "deepl"

    async def test_translates_on_demand_and_caches(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article())
        await db_session.commit()

        with patch(
            "app.api.v1.endpoints.routes._translator.translate_pair",
            new=AsyncMock(return_value=("Título traduzido", "Resumo traduzido. Segunda frase. Terceira frase.")),
        ):
            r = await client.post(
                "/v1/articles/test-article-001/translate",
                json={"target_language": "pt"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "Título traduzido"
        assert data["language_code"] == "pt"

    async def test_rejects_unsupported_language(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(make_article())
        await db_session.commit()

        r = await client.post(
            "/v1/articles/test-article-001/translate",
            json={"target_language": "xx"},
        )
        assert r.status_code == 422

    async def test_returns_404_for_missing_article(self, client: AsyncClient):
        r = await client.post(
            "/v1/articles/nonexistent/translate",
            json={"target_language": "fr"},
        )
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────
#  /v1/languages  tests
# ─────────────────────────────────────────────────────────────

class TestLanguages:
    async def test_returns_language_list(self, client: AsyncClient):
        r = await client.get("/v1/languages")
        assert r.status_code == 200
        data = r.json()
        assert "languages" in data
        assert len(data["languages"]) > 0
        codes = [lang["code"] for lang in data["languages"]]
        assert "en" in codes
        assert "fr" in codes
        assert "am" in codes

    async def test_each_language_has_required_fields(self, client: AsyncClient):
        r = await client.get("/v1/languages")
        for lang in r.json()["languages"]:
            assert "code" in lang
            assert "name" in lang
            assert "native" in lang


# ─────────────────────────────────────────────────────────────
#  /v1/health  tests
# ─────────────────────────────────────────────────────────────

class TestHealth:
    async def test_health_ok(self, client: AsyncClient):
        r = await client.get("/v1/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("ok", "degraded")
        assert "version" in data
        assert "db" in data
        assert "cache" in data
