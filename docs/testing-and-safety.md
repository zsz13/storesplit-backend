# Testing, quality gates and database safety

## Destructive test helpers must never target the development database

`tests/conftest.py` runs `Base.metadata.drop_all` and `tests/test_migrations.py` runs
`DROP SCHEMA public CASCADE`, both against whatever `TEST_DATABASE_URL` names. `app/db/safety.py`
stands in the way and **fails closed**: a database is reset only when it is not `DATABASE_URL`
(hostnames folded first), does not share the application database's name, reads as a test
database by name, is confirmed by `current_database()` on the live connection, and either
carries a disposability marker naming that exact host and database or is provably empty. The
marker is the rule that depends on neither a name nor an environment variable, and it is what
stops a *development* database that merely happens to be named `storesplit_test` from being
reset. `tests/test_db_safety.py` exercises each rule against a database that must be refused
and one that must be allowed.

## Quality gates

```bash
uv run pytest                              # unit + API tests on in-memory SQLite (fast)
TEST_DATABASE_URL=postgresql+asyncpg://storesplit:storesplit@localhost:5432/storesplit_test uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

Tests use fixtures captured from the retailers (`tests/fixtures/`) and an in-memory fake
adapter; they never hit live websites. Create the Postgres test database once with
`docker compose exec db psql -U storesplit -c 'CREATE DATABASE storesplit_test'`.

## Migrations


```bash
uv run alembic upgrade head                                   # apply
uv run alembic revision --autogenerate -m "describe change"   # after editing app/db/models.py
uv run alembic downgrade -1                                   # roll back one revision
```

Autogenerate needs a reachable PostgreSQL (`DATABASE_URL`). Review generated files before committing.

