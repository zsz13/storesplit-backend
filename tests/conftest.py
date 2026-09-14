"""Test database and API client: async throughout, like the application itself.

SQLite in-memory (aiosqlite) by default, PostgreSQL (asyncpg) when TEST_DATABASE_URL is set.
The API is exercised over an in-process ASGI transport rather than a thread portal, so the
tests, the app and the database all share one event loop.

Whatever TEST_DATABASE_URL names is *dropped* before every test, so `app.db.safety` decides
whether it may be: once for the whole session in `pytest_sessionstart`, before a fixture has
run or a test has been collected, and again inside the `engine` fixture so the helper that
does the dropping is the helper that asks. See that module for what has to be true.
"""

import asyncio
import os
from collections.abc import AsyncGenerator

import pytest
from app.config import get_settings
from app.db.models import Base
from app.db.safety import UnsafeTestDatabaseError, require_disposable_test_database
from app.db.session import async_database_url, get_db, get_sessionmaker
from app.main import app
from app.retailers.clients import RetailerClients
from app.services.refresh import registry
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

TEST_DATABASE_URL = async_database_url(
    os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
)


def pytest_sessionstart() -> None:
    """Prove the test database is disposable before anything at all runs.

    Here rather than only in the fixtures because it is the one place no future helper can
    route around: a destructive fixture added later inherits the protection without knowing
    about it, and a rejected database stops the session instead of the first test.

    The refusal is turned into `pytest.exit` rather than left to propagate: an INTERNALERROR
    prints the reason under thirty lines of traceback, and the reason is the whole point.
    """
    try:
        asyncio.run(require_disposable_test_database(TEST_DATABASE_URL))
    except UnsafeTestDatabaseError as refused:
        pytest.exit(f"\n{refused}", returncode=pytest.ExitCode.USAGE_ERROR)


@pytest.fixture
async def engine():
    # Cheap after `pytest_sessionstart` (the verdict is remembered per URL), and stated here
    # because this fixture is one of the two that can destroy a database.
    await require_disposable_test_database(TEST_DATABASE_URL)
    if TEST_DATABASE_URL.startswith("sqlite"):
        # One shared in-memory connection, so every session in the test sees the same data.
        engine = create_async_engine(
            TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    else:
        engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
async def db(sessionmaker) -> AsyncGenerator[AsyncSession]:
    async with sessionmaker() as session:
        yield session


@pytest.fixture
async def clients() -> AsyncGenerator[RetailerClients]:
    """A real client pool; adapters take one but the tests never let them reach the network."""
    async with RetailerClients() as pool:
        yield pool


@pytest.fixture
async def client(db, sessionmaker) -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def refresh_registry(monkeypatch: pytest.MonkeyPatch):
    """Background refreshes are off unless a test asks for them, and never leak between tests.

    A refresh is a real scrape running on its own task with its own sessions. Left enabled,
    every search test would start one against the fake adapters and, on SQLite's single
    shared in-memory connection, interleave its transaction with the request under test.
    Tests that exercise refreshing turn it on with `settings_override` and drive it.
    """
    monkeypatch.setenv("SEARCH_AUTO_REFRESH", "false")
    get_settings.cache_clear()
    registry.reset()
    yield registry
    registry.cancel_all()
    get_settings.cache_clear()


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch):
    """Set environment-backed settings for one test and rebuild the cached Settings."""

    def apply(**values: object) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()
