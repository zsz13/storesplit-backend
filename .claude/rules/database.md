---
paths:
  - "app/db/**"
  - "alembic/**"
  - "app/services/scraper.py"
  - "tests/test_migrations.py"
  - "tests/test_db_*.py"
---

# Database, sessions and migrations

Root `CLAUDE.md` carries the fail-closed test-database invariant, because it must hold
whatever file is open. Everything else about the schema, the engine and migrations is here.

## Engine and sessions

- **One engine, many short sessions.** `app/db/session.py` owns a single `AsyncEngine`
  (asyncpg in Docker, aiosqlite in tests), disposed once on shutdown. Never build an engine
  per request. Request handlers depend on `get_db`; work that owns its own transactions
  depends on `get_sessionmaker`.

## Reading a batch, not a row

- **Read the batch, not the row.** `load_batch` reads a (store, category) ingest's canonical
  products, retailer products, offers and latest price points in four queries; per-listing
  lookups are the N+1 that made scrapes slow.

## Migrations

Edit `app/db/models.py`, then `uv run alembic revision --autogenerate -m "..."` against a
running Postgres, review the file in `alembic/versions/`, then `upgrade head`. Keep column
types portable (String/Numeric/JSON/DateTime) so tests run on SQLite. `app/db/models.py` must
not import `app.retailers`: that package pulls in every adapter and httpx, and `alembic/env.py`
imports the models, so a broken adapter would break migrations. Shared vocabulary the schema
needs goes in `app/normalize/`. A migration that repairs existing rows is covered by
`tests/test_migrations.py`, which runs the real `alembic upgrade` (Postgres only).

