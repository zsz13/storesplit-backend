"""The guard that stands between a test run and somebody's database.

A `TEST_DATABASE_URL` that points at the application's own database would hand
`DROP SCHEMA public CASCADE` to live data. These tests hold that line: each rule in
`app.db.safety` is exercised against a database that must be refused and a database that
must be allowed.

The URL-only rules are tested without a server, deliberately -- they are the ones that have
to fire before anything connects. The live rules (`current_database()`, the disposable
marker) need PostgreSQL and skip without it; they are then run against the *protected* test
database only, and they never write anything to it beyond its own disposable marker.
"""

import os

import pytest
from app.db import safety
from app.db.safety import (
    UnsafeTestDatabaseError,
    check_test_database_url,
    identify,
    marker_for,
    require_disposable_test_database,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEV = "postgresql+asyncpg://storesplit:storesplit@localhost:5432/storesplit"
DEV_IN_COMPOSE = "postgresql+asyncpg://storesplit:storesplit@db:5432/storesplit"
TEST_DB = "postgresql+asyncpg://storesplit:storesplit@localhost:5432/storesplit_test"

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
requires_postgres = pytest.mark.skipif(
    "postgresql" not in TEST_URL, reason="the live identity checks need PostgreSQL"
)


@pytest.fixture
def protecting(monkeypatch: pytest.MonkeyPatch):
    """Pretend the application's database is `url`, for one test.

    `safety.PROTECTED` is frozen at import on purpose -- a guard that re-read the
    environment would be disarmed by the very monkeypatching the migration tests do -- so
    the tests reach past the front door rather than setting DATABASE_URL.
    """

    def apply(url: str) -> None:
        monkeypatch.setattr(safety, "PROTECTED", identify(url))

    return apply


# ------------------------------------------------------------------ the most dangerous shape


def test_the_development_database_is_refused(protecting) -> None:
    """The most dangerous shape: TEST_DATABASE_URL == DATABASE_URL."""
    protecting(DEV)
    with pytest.raises(UnsafeTestDatabaseError) as raised:
        check_test_database_url(DEV)
    message = str(raised.value)
    assert "storesplit" in message  # which database was rejected
    assert "TEST_DATABASE_URL == DATABASE_URL" in message  # and why
    assert "storesplit:storesplit" not in message  # never the password


def test_the_same_database_reached_by_another_hostname_is_refused(protecting) -> None:
    """Compose's `db` and the host's `localhost` are one PostgreSQL. A different spelling
    of the same server must not read as a different server."""
    protecting(DEV_IN_COMPOSE)
    with pytest.raises(UnsafeTestDatabaseError):
        check_test_database_url(DEV)


def test_a_dedicated_test_database_is_permitted(protecting) -> None:
    protecting(DEV)
    assert check_test_database_url(TEST_DB).database == "storesplit_test"


# ------------------------------------------------------------------------------- the rules


def test_a_shared_name_on_another_host_is_refused(protecting) -> None:
    """Rule 2: the bare name is compared too, for a host spelling nobody taught us."""
    protecting(DEV)
    with pytest.raises(UnsafeTestDatabaseError, match="its name is the application's own"):
        check_test_database_url("postgresql+asyncpg://u:p@some-other-host:5432/storesplit")


@pytest.mark.parametrize("name", ["storesplit_dev", "prod", "storesplit_fresh", "analytics"])
def test_a_name_that_does_not_read_as_a_test_database_is_refused(protecting, name: str) -> None:
    protecting(DEV)
    with pytest.raises(UnsafeTestDatabaseError, match="does not read as a test database"):
        check_test_database_url(f"postgresql+asyncpg://u:p@localhost:5432/{name}")


@pytest.mark.parametrize("name", ["storesplit_test", "test_storesplit", "ci-test-3", "tests"])
def test_names_that_do_read_as_a_test_database(protecting, name: str) -> None:
    protecting(DEV)
    assert check_test_database_url(f"postgresql+asyncpg://u:p@localhost:5432/{name}")


@pytest.mark.parametrize("name", ["latest", "contest", "attestation"])
def test_a_word_merely_containing_test_is_not_a_test_database(protecting, name: str) -> None:
    """`latest` is not a test database, and a substring match would say it was."""
    protecting(DEV)
    with pytest.raises(UnsafeTestDatabaseError, match="does not read as a test database"):
        check_test_database_url(f"postgresql+asyncpg://u:p@localhost:5432/{name}")


@pytest.mark.parametrize("name", ["postgres", "template1", "template0"])
def test_server_owned_databases_are_never_disposable(
    protecting, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    protecting(DEV)
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("STORESPLIT_TEST_DB_IS_DISPOSABLE", "1")
    with pytest.raises(UnsafeTestDatabaseError, match="never disposable"):
        check_test_database_url(f"postgresql+asyncpg://u:p@localhost:5432/{name}")


def test_ci_may_waive_the_name_rule(protecting, monkeypatch: pytest.MonkeyPatch) -> None:
    protecting(DEV)
    url = "postgresql+asyncpg://u:p@localhost:5432/ephemeral_db_9f2c"
    with pytest.raises(UnsafeTestDatabaseError):
        check_test_database_url(url)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("STORESPLIT_TEST_DB_IS_DISPOSABLE", "1")
    assert check_test_database_url(url)


def test_ci_alone_waives_nothing(protecting, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hosted runner sets CI, including ones with a real database attached."""
    protecting(DEV)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("STORESPLIT_TEST_DB_IS_DISPOSABLE", raising=False)
    with pytest.raises(UnsafeTestDatabaseError):
        check_test_database_url("postgresql+asyncpg://u:p@localhost:5432/ephemeral_db_9f2c")


def test_ci_never_waives_the_identity_rule(protecting, monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch opens the name rule and nothing else."""
    protecting(DEV)
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("STORESPLIT_TEST_DB_IS_DISPOSABLE", "1")
    with pytest.raises(UnsafeTestDatabaseError):
        check_test_database_url(DEV)


def test_a_sync_driver_url_is_the_same_database(protecting) -> None:
    """An old `.env` spelling must not become a second identity that passes the guard."""
    protecting("postgresql://storesplit:storesplit@localhost:5432/storesplit")
    with pytest.raises(UnsafeTestDatabaseError):
        check_test_database_url(DEV)


def test_sqlite_in_memory_is_always_allowed(protecting) -> None:
    protecting(DEV)
    assert check_test_database_url("sqlite+aiosqlite:///:memory:").database == ":memory:"


def test_a_sqlite_file_obeys_the_same_rules(protecting) -> None:
    protecting("sqlite+aiosqlite:///./storesplit.db")
    with pytest.raises(UnsafeTestDatabaseError):
        check_test_database_url("sqlite+aiosqlite:///./storesplit.db")
    assert check_test_database_url("sqlite+aiosqlite:///./storesplit_test.db")


def test_a_directory_called_tests_does_not_make_a_database_disposable(protecting) -> None:
    """The identity carries an absolute path, and matching "test" anywhere in it made every
    SQLite file under a `tests/` directory droppable -- including a development one."""
    protecting(DEV)
    with pytest.raises(UnsafeTestDatabaseError, match="does not read as a test database"):
        check_test_database_url("sqlite+aiosqlite:////Users/me/code/tests/data/dev.db")
    assert check_test_database_url("sqlite+aiosqlite:////Users/me/code/data/dev_test.db")


@pytest.mark.parametrize(
    "url", ["mysql+aiomysql://u:p@localhost:3306/app_test", "cockroachdb://u@h:26257/app_test"]
)
async def test_a_backend_with_no_live_check_is_refused(protecting, url: str) -> None:
    """Rules 4 and 5 are written against PostgreSQL's catalogue. Anywhere else the name is
    the only evidence there is, and a guard whose strongest rule silently does not apply is
    not a guard."""
    protecting(DEV)
    with pytest.raises(UnsafeTestDatabaseError, match="cannot be confirmed from the live"):
        await require_disposable_test_database(url)


async def test_in_memory_sqlite_needs_no_live_check(protecting) -> None:
    protecting(DEV)
    safety._verified.clear()
    assert await require_disposable_test_database("sqlite+aiosqlite:///:memory:")


# -------------------------------------------------------------------- the live-connection rules


@requires_postgres
async def test_the_live_connection_is_asked_which_database_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 4: `current_database()` is compared with the name in the URL rather than trusted.

    A URL that reaches a database it does not describe cannot be built from outside (a
    wrong name is a connection error), so the disagreement is staged: the URL-only check is
    made to report a name the server will not confirm.
    """
    safety._verified.clear()
    real = identify(TEST_URL)
    elsewhere = safety.DatabaseIdentity(real.backend, real.host, real.port, "some_other_test_db")
    monkeypatch.setattr(safety, "check_test_database_url", lambda _url: elsewhere)
    with pytest.raises(UnsafeTestDatabaseError, match="does not describe the database it reaches"):
        await require_disposable_test_database(TEST_URL)
    safety._verified.clear()


@requires_postgres
async def test_the_protected_test_database_carries_a_marker_naming_itself() -> None:
    """Rule 5, the half that lets a test run happen: the suite's own database is empty on
    first use and claims itself.

    The marker names the database it was written for, so a `pg_dump` restored under another
    name arrives unclaimed rather than inheriting disposability from the database it was
    copied from."""
    safety._verified.clear()
    await require_disposable_test_database(TEST_URL)
    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as connection:
            marker = await connection.scalar(
                text(
                    "select shobj_description(oid, 'pg_database') from pg_database "
                    "where datname = current_database()"
                )
            )
    finally:
        await engine.dispose()
    identity = identify(TEST_URL)
    assert marker == marker_for(identity)
    assert identity.database in str(marker)


@requires_postgres
async def test_a_marker_written_for_another_database_does_not_claim_this_one() -> None:
    safety._verified.clear()
    identity = identify(TEST_URL)
    elsewhere = safety.DatabaseIdentity(
        identity.backend, "some-other-host", identity.port, identity.database
    )
    assert marker_for(elsewhere) != marker_for(identity)


@requires_postgres
async def test_a_database_holding_only_a_materialized_view_is_not_empty() -> None:
    """`DROP SCHEMA public CASCADE` takes matviews, foreign tables, views and sequences with
    it, so counting only ordinary tables would have read a database full of them as having
    nothing to lose -- and then stamped it disposable."""
    safety._verified.clear()
    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("create materialized view _guard_mv as select 1 as x"))
    finally:
        await engine.dispose()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(safety, "DISPOSABLE_MARKER", "storesplit:a-marker-nobody-wrote")
        with pytest.raises(UnsafeTestDatabaseError, match="relation"):
            await require_disposable_test_database(TEST_URL)

    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("drop materialized view _guard_mv"))
    finally:
        await engine.dispose()
    safety._verified.clear()


@requires_postgres
async def test_a_populated_unmarked_database_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 5, the half that stops one: a database holding tables and carrying no marker is
    somebody's, whatever it is called. Simulated by hiding the marker, because the only
    database this suite is allowed to touch is its own."""
    safety._verified.clear()
    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("create table if not exists _guard_probe (id int)"))
    finally:
        await engine.dispose()

    monkeypatch.setattr(safety, "DISPOSABLE_MARKER", "storesplit:a-marker-nobody-wrote")
    try:
        with pytest.raises(UnsafeTestDatabaseError, match="disposability marker"):
            await require_disposable_test_database(TEST_URL)
    finally:
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("drop table if exists _guard_probe"))
        finally:
            await engine.dispose()
        safety._verified.clear()
