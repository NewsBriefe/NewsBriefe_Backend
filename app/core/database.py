import ssl
import re
from datetime import datetime, timezone
from typing import AsyncGenerator
from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.core.config import get_settings

settings = get_settings()


def _clean_db_url(url: str) -> tuple[str, bool]:
    """
    Strip query parameters asyncpg doesn't understand (sslmode, channel_binding, etc.)
    and return (clean_url, needs_ssl).

    Neon gives:  postgresql+asyncpg://user:pass@host/db?sslmode=require&channel_binding=require
    asyncpg needs: postgresql+asyncpg://user:pass@host/db  + ssl via connect_args
    """
    # Remove everything after the ? in the URL
    clean = re.sub(r'\?.*$', '', url)

    # Determine if SSL is needed based on the host
    # Local Docker uses "localhost" or the "postgres" service name
    no_ssl_hosts = ("localhost", "@postgres:", "@127.0.0.1", "127.0.0.1")
    needs_ssl = not any(h in url for h in no_ssl_hosts)

    return clean, needs_ssl


_clean_url, _needs_ssl = _clean_db_url(settings.database_url)

if _needs_ssl:
    _ssl_ctx = ssl.create_default_context()
else:
    _ssl_ctx = False

engine = create_async_engine(
    _clean_url,
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
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
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
