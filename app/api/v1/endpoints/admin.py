"""
Admin endpoints — manual triggers for background tasks.

These let you kick off a news fetch or summarization run immediately
without waiting for the Celery schedule.

Protected by X-Admin-Key header (set ADMIN_KEY env var).
If ADMIN_KEY is not set, these endpoints are disabled entirely.

Endpoints:
  POST /v1/admin/fetch         — fetch + store new articles from all sources
  POST /v1/admin/summarize     — summarize pending articles + pre-translate
  POST /v1/admin/fetch-and-run — fetch then immediately summarize (most useful)
  GET  /v1/admin/status        — see pending article count + DB stats
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated
from app.core.config import get_settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.models.orm import Article
from app.workers.tasks import fetch_and_process_news, summarize_pending

settings = get_settings()
log      = get_logger(__name__)
router   = APIRouter(prefix="/admin", tags=["admin"])


# ── Admin auth ────────────────────────────────────────────────
async def verify_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    admin_key = getattr(settings, "admin_key", "")
    if not admin_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "admin_disabled", "message": "Admin endpoints are disabled. Set ADMIN_KEY env var to enable."},
        )
    if x_admin_key != admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid or missing X-Admin-Key header"},
        )

AdminDep = Annotated[None, Depends(verify_admin_key)]
DBDep    = Annotated[AsyncSession, Depends(get_db)]


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/fetch", summary="Trigger news fetch from all sources")
async def trigger_fetch(_auth: AdminDep):
    """
    Enqueues fetch_and_process_news as a Celery task.
    Returns immediately — the task runs in the background worker.
    Watch worker logs to see progress.
    """
    task = fetch_and_process_news.delay()
    log.info("admin_fetch_triggered", task_id=task.id)
    return {
        "status": "queued",
        "task_id": task.id,
        "message": "Fetch task sent to worker. Watch worker logs for progress.",
    }


@router.post("/summarize", summary="Trigger summarization of pending articles")
async def trigger_summarize(_auth: AdminDep):
    """
    Enqueues summarize_pending as a Celery task.
    Summarizes up to 50 unsummarized articles and pre-translates them.
    """
    task = summarize_pending.delay()
    log.info("admin_summarize_triggered", task_id=task.id)
    return {
        "status": "queued",
        "task_id": task.id,
        "message": "Summarize task sent to worker.",
    }


@router.post("/fetch-and-run", summary="Fetch news then summarize immediately")
async def trigger_fetch_and_run(_auth: AdminDep):
    """
    The most useful endpoint when starting fresh.
    Triggers a fetch. The fetch task automatically enqueues summarize_pending
    when it completes, so you get the full pipeline in one call.

    Sequence:
      1. Articles fetched from RSS + NewsAPI → stored in DB (unsummarized)
      2. Worker runs summarize_pending → Claude/Bedrock generates summaries
      3. Worker pre-translates top 10 languages
      4. Cache invalidated → /v1/stories returns fresh results
    """
    task = fetch_and_process_news.delay()
    log.info("admin_fetch_and_run_triggered", task_id=task.id)
    return {
        "status": "queued",
        "task_id": task.id,
        "message": (
            "Fetch task queued. Summarization will start automatically after fetch completes. "
            "Stories will appear in /v1/stories once summarization finishes. "
            "Watch worker logs: docker compose logs -f worker"
        ),
    }


@router.get("/status", summary="View pending article count and DB stats")
async def admin_status(_auth: AdminDep, db: DBDep):
    """
    Returns counts of articles in each processing state.
    Use this to check how many articles are waiting to be summarized.
    """
    total = await db.scalar(select(func.count()).select_from(Article))
    summarized = await db.scalar(
        select(func.count()).select_from(Article).where(Article.is_summarized == True)
    )
    pending = await db.scalar(
        select(func.count()).select_from(Article).where(Article.is_summarized == False)
    )
    active = await db.scalar(
        select(func.count()).select_from(Article).where(Article.is_active == True)
    )
    breaking = await db.scalar(
        select(func.count()).select_from(Article).where(
            Article.is_breaking == True, Article.is_active == True
        )
    )

    # Category breakdown
    cat_rows = (await db.execute(
        select(Article.category, func.count().label("n"))
        .where(Article.is_active == True, Article.is_summarized == True)
        .group_by(Article.category)
        .order_by(func.count().desc())
    )).all()

    return {
        "articles": {
            "total":      total,
            "summarized": summarized,
            "pending":    pending,
            "active":     active,
            "breaking":   breaking,
        },
        "by_category": {row.category: row.n for row in cat_rows},
        "hint": (
            "If pending > 0 and summarized == 0, call POST /v1/admin/fetch-and-run first."
            if (pending or 0) > 0 and (summarized or 0) == 0
            else "Looking good."
        ),
    }
