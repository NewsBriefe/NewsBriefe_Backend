"""
FastAPI Application Factory
"""
import time
import sentry_sdk
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.cache import close_redis
from app.api.v1.endpoints.routes import router

settings = get_settings()
log = get_logger(__name__)


# ── Lifespan (startup / shutdown) ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("newsbrief_api_starting", version=settings.app_version, env=settings.environment)

    # Create DB tables (dev only — use alembic in production)
    if settings.is_development:
        from app.core.database import engine
        from app.models.orm import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("db_tables_created")

    yield

    # Shutdown
    await close_redis()
    log.info("newsbrief_api_shutdown")


# ── Rate limiter ─────────────────────────────────────────────
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.rate_limit_per_minute}/minute"],
)


def create_app() -> FastAPI:
    # ── Sentry (production only) ─────────────────────────
    if settings.sentry_dsn and settings.is_production:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=0.2,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                SqlalchemyIntegration(),
            ],
            environment=settings.environment,
        )

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "NewsBrief API — Short, clear, translated news for everyone.\n\n"
            "## Features\n"
            "- 3-sentence AI summaries (Claude)\n"
            "- 20+ language translations (DeepL + Google)\n"
            "- Deduplicated from 12+ RSS sources + NewsAPI\n"
            "- 4-hour cache refresh cycle\n"
        ),
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── CORS ─────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ── Rate limiting ─────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Request timing middleware ─────────────────────────
    @app.middleware("http")
    async def add_process_time(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        response.headers["X-Process-Time"] = f"{duration:.4f}"
        log.debug(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration * 1000, 1),
        )
        return response

    # ── Error handlers ────────────────────────────────────
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "code": "validation_error",
                "message": "Invalid request parameters",
                "details": exc.errors(),
            },
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        log.error("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": "internal_error",
                "message": "An unexpected error occurred",
            },
        )

    # ── Routes ───────────────────────────────────────────
    app.include_router(
        router,
        prefix=settings.api_prefix,
        tags=["news"],
    )

    return app


app = create_app()
