"""Async SQLAlchemy engine/session wiring.

One `AsyncEngine` (and therefore one connection pool) per process, created on first use and
disposed once on shutdown. Sessions are short-lived: one per request or per logical
transaction, never shared between concurrent tasks (an `AsyncSession` is not concurrency
safe).
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

# Sync drivers that may still appear in an existing DATABASE_URL, mapped to their async
# counterpart so an old .env keeps working instead of failing deep inside SQLAlchemy.
_ASYNC_DRIVERS = {
    "postgresql": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
    "postgresql+psycopg": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
    "sqlite": "sqlite+aiosqlite",
    "sqlite+pysqlite": "sqlite+aiosqlite",
}

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def async_database_url(url: str) -> str:
    """The same database URL, spelled with an async driver."""
    scheme, separator, rest = url.partition("://")
    if not separator:
        return url
    return f"{_ASYNC_DRIVERS.get(scheme, scheme)}://{rest}"


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = async_database_url(settings.database_url)
        kwargs: dict[str, object] = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            # SQLite's aiosqlite dialect uses a pool that takes none of these.
            kwargs |= {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout_seconds,
            }
        _engine = create_async_engine(url, **kwargs)  # pyright: ignore[reportArgumentType]
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


async def dispose_engine() -> None:
    """Close the connection pool. Called once on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding one session per request."""
    async with get_session_factory()() as session:
        yield session


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency for work that owns its own transactions (the scrape service).

    A scrape run is many transactions over minutes, so it must not borrow the request's
    session. Tests override this dependency to hand back a factory bound to the test engine.
    """
    return get_session_factory()
