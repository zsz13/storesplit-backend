---
paths:
  - "tests/**"
  - "conftest.py"
  - "app/db/safety.py"
---

# Tests and the destructive-database guard

## The test database must prove it is disposable

**The test database must prove it is disposable before anything drops anything.**
`tests/conftest.py` runs `Base.metadata.drop_all` and `tests/test_migrations.py` runs
`DROP SCHEMA public CASCADE`, both against whatever `TEST_DATABASE_URL` names -- so a
misconfigured value would destroy the database it points at. `app/db/safety.py` stands in
the way, and it **fails closed**: a database is reset only if it is not `DATABASE_URL` (hostnames
folded first, so Compose's `db` and the host's `localhost` are one machine), does not share
the application database's *name*, reads as a test database by name (waivable only by setting
both `CI` and `STORESPLIT_TEST_DB_IS_DISPOSABLE`, and never for `postgres` or the templates),
is confirmed by `current_database()` on the live connection, and either carries a
disposability marker **naming that exact host and database** in its PostgreSQL database
comment or is provably empty -- in which case it is stamped for next time. The marker is the
rule that does not depend on a name or an environment variable, and it is what stops a
*development* database that merely happens to be named `storesplit_test` from being reset. It names its own database
so a `pg_dump` restored elsewhere arrives unclaimed instead of inheriting disposability, and
"empty" counts every user relation (`r p m f v S`), not just ordinary tables, because
`DROP SCHEMA public CASCADE` takes materialized views and foreign tables with it too.
PostgreSQL and in-memory SQLite are the only targets accepted at all: rules 4 and 5 are
written against PostgreSQL's catalogue, and a guard whose strongest rule silently does not
apply is not a guard.

- `DATABASE_URL` is read once, at import, and frozen: `tests/test_migrations.py` legitimately
  monkeypatches it to aim Alembic at the test database, and a guard that re-read it would
  compare the test URL with itself and wave it through.
- It runs in `pytest_sessionstart` (so a rejected database stops the session before a fixture
  or a collection, and a helper added later inherits the protection) **and** inside each
  fixture that can destroy a database -- `conftest.engine` and `test_migrations.alembic_config`.
  Any new fixture or script that drops a schema, drops every table, or truncates must call
  `require_disposable_test_database` too.
- Refusal is a `pytest.exit` with the database and the reason, not a traceback, and never
  prints a password. `tests/test_db_safety.py` is the regression: the dev URL is refused and
  `storesplit_test` is permitted, rule by rule.

Fixtures in `tests/fixtures/` are real captured payloads (trimmed); `tests/fakes.py` has an
in-memory async adapter and a `Tracker` that counts overlapping calls. Tests are async
(`asyncio_mode = "auto"`) and drive the API over an in-process ASGI transport, so tests, app
and database share one event loop. Cover: unit parsing/conversion, unit price, category
rules, matching, adapter parsing, scrape -> search -> basket through the API, price-history
deduplication and its endpoint, and (`tests/test_concurrency.py`) real overlap, the configured
bounds, retailer isolation and async transaction rollback. Never depend on live sites.

