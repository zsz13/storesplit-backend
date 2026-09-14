"""The engine is one per process, and old-style database URLs still work."""

import pytest
from app.config import get_settings
from app.db import session as session_module
from app.db.session import async_database_url, dispose_engine, get_engine, get_session_factory
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture(autouse=True)
async def isolated_engine():
    """Keep the module's singletons out of other tests."""
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # The previous default and its psycopg2 spelling keep working after the move.
        ("postgresql+psycopg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("postgresql+psycopg2://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("postgres://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("sqlite+pysqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
        ("sqlite:///./local.db", "sqlite+aiosqlite:///./local.db"),
        # Already async, or a driver we know nothing about: left exactly as given.
        ("postgresql+asyncpg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("mysql+aiomysql://u@h/d", "mysql+aiomysql://u@h/d"),
        ("not-a-url", "not-a-url"),
    ],
)
def test_database_url_is_spelled_with_an_async_driver(given: str, expected: str) -> None:
    assert async_database_url(given) == expected


async def test_one_engine_per_process_not_one_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    try:
        engine = get_engine()
        assert isinstance(engine, AsyncEngine)
        assert get_engine() is engine, "a second call must reuse the pool, not open another"
        assert get_session_factory() is get_session_factory()
        assert get_session_factory().kw["bind"] is engine
        await dispose_engine()
        assert session_module._engine is None
        assert get_engine() is not engine, "after disposal a fresh engine is built"
    finally:
        get_settings.cache_clear()


async def test_pool_settings_reach_a_postgres_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/d")
    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "3")
    get_settings.cache_clear()
    try:
        engine = get_engine()  # lazy: no connection is opened here
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 3
    finally:
        get_settings.cache_clear()


async def test_sqlite_engine_omits_the_pool_settings_its_dialect_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    try:
        assert get_engine().url.drivername == "sqlite+aiosqlite"
    finally:
        get_settings.cache_clear()
