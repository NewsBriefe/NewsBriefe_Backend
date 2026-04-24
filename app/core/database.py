from datetime import datetime, timezone
from typing import AsyncGenerator
from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.core.config import get_settings

settings = get_settings()

# ── Engine ─────────────────────────────────────────────────
# FIX: asyncpg by default attempts an SSL handshake before plain TCP.
# PostgreSQL in Docker does not have SSL configured, so it responds
# with 'N' (not supported). Some asyncpg + uvloop versions misinterpret
# this as ConnectionRefusedError instead of falling back to plain TCP.
# ssl=False tells asyncpg to skip SSL entirely — correct for Docker.
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    echo=settings.debug,
    pool_pre_ping=True,
    connect_args={"ssl": False},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ── Base model with timestamps ─────────────────────────────
class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ── Dependency ─────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
