import ssl
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

# ── SSL config ─────────────────────────────────────────────
# Local Docker: ssl=False (postgres has no cert)
# Cloud (Neon, Supabase, etc.): ssl=True (TLS required)
# Auto-detected from DATABASE_URL:
#   - contains "localhost" or "postgres" (Docker service) → no SSL
#   - contains "neon.tech", "supabase", etc. → SSL
def _needs_ssl(url: str) -> bool:
    no_ssl_hosts = ("localhost", "@postgres:", "@127.0.0.1")
    return not any(h in url for h in no_ssl_hosts)

if _needs_ssl(settings.database_url):
    _ssl_ctx = ssl.create_default_context()
else:
    _ssl_ctx = False

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    echo=settings.debug,
    pool_pre_ping=True,
    connect_args={"ssl": _ssl_ctx},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


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
