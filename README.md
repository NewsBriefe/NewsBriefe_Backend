# NewsBrief Backend

> FastAPI · PostgreSQL · Redis · Celery · Claude AI · DeepL

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Flutter App                              │
│                    (iOS · Android · Desktop)                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │  HTTPS REST
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI (port 8000)                        │
│  GET /v1/articles     GET /v1/articles/:id                      │
│  GET /v1/articles/search   POST /v1/articles/:id/translate      │
│  GET /v1/languages    GET /v1/health                            │
└──────┬────────────────────────────┬───────────────────────────┘
       │                            │
       ▼                            ▼
┌─────────────┐           ┌──────────────────┐
│  PostgreSQL │           │   Redis Cache    │
│  Articles   │           │  4h TTL          │
│  Translations│          │  Session state   │
│  Fetch logs │           └──────────────────┘
└─────────────┘
       ▲
       │ writes
       │
┌──────────────────────────────────────────────────────────────┐
│                   Celery Workers                             │
│                                                              │
│  fetch_and_process_news   (every 4 hours)                    │
│  ├── RSSFetcher.fetch_all()          — 12 RSS feeds          │
│  ├── NewsAPIFetcher.fetch_headlines() — NewsAPI              │
│  ├── Deduplicator.filter()           — URL + hash + TF-IDF  │
│  └── ArticleRepository.bulk_create_raw()                     │
│                                                              │
│  summarize_pending   (every 15 minutes)                      │
│  ├── SummarizationService.summarize() — Claude API           │
│  ├── SummarizationService.categorize()                       │
│  ├── TranslationService.translate_pair() — DeepL/Google      │
│  └── CacheClient.delete_pattern("articles:*")                │
│                                                              │
│  cleanup_old_articles   (daily 03:00 UTC)                    │
│  └── soft-delete articles > 7 days                           │
└──────────────────────────────────────────────────────────────┘
       │
       ▼ calls
┌───────────────────────────────────────┐
│  External Services                    │
│  • Anthropic Claude — summarization   │
│  • DeepL — translation (primary)      │
│  • Google Translate — fallback        │
│  • NewsAPI — top headlines            │
│  • RSS feeds — 12 outlets (free)      │
└───────────────────────────────────────┘
```

---

## Project structure

```
newsbrief-backend/
├── app/
│   ├── main.py                  # FastAPI app factory + middleware
│   ├── core/
│   │   ├── config.py            # Pydantic Settings (env vars)
│   │   ├── database.py          # Async SQLAlchemy engine
│   │   ├── cache.py             # Redis client + typed helpers
│   │   └── logging.py           # Structlog setup
│   ├── models/
│   │   ├── orm.py               # SQLAlchemy ORM models
│   │   └── schemas.py           # Pydantic API schemas
│   ├── services/
│   │   ├── ingestion.py         # RSS + NewsAPI fetchers + dedup
│   │   ├── summarizer.py        # Claude AI summarization
│   │   ├── translator.py        # DeepL + Google translation
│   │   └── repository.py       # All DB queries
│   ├── workers/
│   │   └── tasks.py             # Celery tasks + schedules
│   └── api/v1/endpoints/
│       └── routes.py            # All API endpoints
├── tests/
│   ├── unit/test_services.py    # Pure logic tests (no DB)
│   └── integration/test_api.py  # Full API tests (SQLite)
├── scripts/001_initial.py       # Alembic migration
├── docker-compose.yml           # Full local stack
├── Dockerfile
├── alembic.ini
├── pyproject.toml
└── .env.example
```

---

## Quickstart (Docker — recommended)

```bash
# 1. Clone and enter directory
cd newsbrief-backend

# 2. Copy env and fill in API keys
cp .env.example .env
# → Edit .env: add ANTHROPIC_API_KEY at minimum
#   RSS feeds work without any key — app is fully functional

# 3. Start everything
docker compose up -d

# 4. Trigger first news fetch manually
docker compose exec worker celery -A app.workers.tasks.celery_app call \
  app.workers.tasks.fetch_and_process_news

# 5. Check it's working
curl http://localhost:8000/v1/health
curl "http://localhost:8000/v1/articles?lang=en&per_page=5"
```

Swagger UI: http://localhost:8000/docs
Flower (Celery monitor): http://localhost:5555

---

## Quickstart (local Python)

```bash
# Python 3.11+ required
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

# Start Postgres + Redis (Docker for just the DBs)
docker compose up postgres redis -d

# Run migrations
alembic upgrade head

# Start API server
uvicorn app.main:app --reload --port 8000

# Start worker (separate terminal)
celery -A app.workers.tasks.celery_app worker --loglevel=info

# Start scheduler (separate terminal)
celery -A app.workers.tasks.celery_app beat --loglevel=info \
  --scheduler celery_redbeat.RedBeatScheduler
```

---

## API Reference

All endpoints prefixed with `/v1`.

### `GET /articles`
Fetch top articles, paginated, in any language.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `lang` | string | `en` | ISO 639-1 language code |
| `category` | string | — | `world` `science` `business` `health` `tech` `culture` |
| `page` | int | `1` | Page number |
| `per_page` | int | `20` | Items per page (max 100) |

**Response:**
```json
{
  "items": [
    {
      "id": "uuid",
      "title": "Peace talks resume after pause",
      "summary": "Sentence 1. Sentence 2. Sentence 3.",
      "source_name": "Reuters",
      "source_url": "https://reuters.com",
      "original_url": "https://reuters.com/article/...",
      "image_url": null,
      "category": "world",
      "country": null,
      "published_at": "2025-04-09T10:00:00Z",
      "language_code": "en"
    }
  ],
  "total": 87,
  "page": 1,
  "per_page": 20,
  "has_more": true
}
```

### `GET /articles/search`
Search by keyword and/or country.

| Param | Required | Description |
|-------|----------|-------------|
| `q` | ✅ | Search query (1–200 chars) |
| `country` | — | e.g. `Brazil`, `Japan` |
| `lang` | — | Response language |
| `category` | — | Category filter |
| `limit` | — | Max results (default 20) |

### `GET /articles/{id}`
Single article with 3 sentences broken out individually.

**Response adds:**
```json
{
  "sentence_1": "What happened.",
  "sentence_2": "Why it matters.",
  "sentence_3": "What comes next."
}
```

### `POST /articles/{id}/translate`
On-demand translation. Result is cached in DB.

**Body:** `{ "target_language": "am" }`

### `GET /languages`
List all 20 supported languages with native names.

### `GET /health`
Service health check (DB + Redis status).

---

## Connecting the Flutter app

In `lib/core/services/news_service.dart`:

```dart
// 1. Flip mock flag
const _useMock = false;

// 2. Run with your backend URL
flutter run --dart-define=API_BASE_URL=http://localhost:8000/v1
```

For device testing (phone on same WiFi):
```bash
flutter run --dart-define=API_BASE_URL=http://YOUR_LOCAL_IP:8000/v1
```

---

## Running tests

```bash
# Unit tests only (fast, no DB needed)
pytest tests/unit/ -v

# All tests
pytest -v --cov=app --cov-report=term-missing

# Type check
mypy app/

# Lint
ruff check app/
```

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `REDIS_URL` | ✅ | Redis connection string |
| `DEEPL_API_KEY` | Recommended | DeepL translation (500k chars/month free) |
| `NEWSAPI_KEY` | Optional | NewsAPI.org key (RSS feeds work without it) |
| `ENVIRONMENT` | — | `development` / `production` |
| `DEBUG` | — | Enable SQL + request logging |
| `SENTRY_DSN` | Optional | Error monitoring |
| `RATE_LIMIT_PER_MINUTE` | — | Default: 60 |

---

## Production checklist

- [ ] Set `ENVIRONMENT=production` (disables Swagger UI)
- [ ] Use a real secret for database password
- [ ] Set `ALLOWED_ORIGINS` to your Flutter app's domains
- [ ] Configure Sentry DSN
- [ ] Set up HTTPS (nginx reverse proxy or Caddy)
- [ ] Use Alembic for migrations: `alembic upgrade head`
- [ ] Set up log aggregation (structlog outputs JSON in production)
- [ ] Monitor Celery via Flower (restrict access in prod)

---

## Step 4 — what's next

| Step | What |
|------|------|
| ✅ Step 2 | Flutter app foundation |
| ✅ Step 3 | FastAPI backend (this) |
| Step 4 | Wire Flutter ↔ Backend + run on device |
| Step 5 | Android/iOS permissions (mic, speech) |
| Step 6 | Offline mode + article caching |
| Step 7 | App Store / Play Store deployment |
