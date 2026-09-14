"""Refuse to let a test destroy a database that is not a disposable test database.

The tests build their schema by dropping it first: `tests/conftest.py` runs
`Base.metadata.drop_all`, and `tests/test_migrations.py` runs `DROP SCHEMA public CASCADE`
so it can replay the real Alembic revisions from an empty database. Both aim at whatever
`TEST_DATABASE_URL` names, so a misconfigured value would destroy whichever database that
variable happens to point at -- the development database included.

So this module is the thing those helpers have to get past first, and it **fails closed**:
a database is destroyed only when every one of these holds.

1. It is not the application's own database. `DATABASE_URL` is read once, at import, and
   frozen -- `tests/test_migrations.py` legitimately monkeypatches `DATABASE_URL` to point
   Alembic at the test database, and a guard that re-read it afterwards would compare the
   test URL against itself and wave it through.
2. It does not merely *share a name* with the application's database. Hostnames are folded
   first -- `db:5432/storesplit` inside Compose and `localhost:5432/storesplit` from the
   host are one database with two spellings, which is the shape a misconfiguration most
   easily takes -- and then the bare name is compared as well, because a spelling this
   module has not been taught about must not read as "a different server".
3. Its name reads as a test database (`storesplit_test`, `test_storesplit`, ...). A
   disposable CI database may waive *this* rule, and only this one, by setting both `CI` and
   `STORESPLIT_TEST_DB_IS_DISPOSABLE`. `postgres` and the templates are never allowed.
4. PostgreSQL only -- the live connection agrees. `current_database()` is asked what the
   server actually opened, rather than trusting the string we dialled, and the name rules
   are applied again to the answer.
5. PostgreSQL only -- the database says it is disposable, or is provably empty. An empty
   database has nothing to lose, so the guard claims it by writing a marker into the
   database's comment (which survives `DROP SCHEMA public CASCADE`). A database that holds
   tables and carries no marker is somebody's, and is refused.

Rule 5 is the one that does not depend on a name or an environment variable, and it is the
one that stops a *development* database that merely happens to be named `storesplit_test`
from being destroyed.
"""

import logging
import os
import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import get_settings
from app.db.session import async_database_url

# Written into the PostgreSQL database comment, which `DROP SCHEMA public CASCADE` does not
# touch, so a claimed database stays claimed across runs.
#
# The marker **names the database it was written for**. A bare constant would travel: a
# `pg_dumpall` of a claimed test database emits its `COMMENT ON DATABASE`, so restoring that
# dump anywhere -- under another name, on another server -- would hand disposability to a
# database nobody ever claimed. Tying it to host and name means a restored copy is simply
# unclaimed, which is the safe answer.
DISPOSABLE_MARKER = "storesplit:disposable-test-database"

# Databases that are never an application's own, and never safe to reset, whatever they are
# called and whatever the environment says.
NEVER_DISPOSABLE = frozenset({"", "postgres", "template0", "template1"})

# "test" or "tests" as a word of its own: storesplit_test, test_storesplit, ci-test-3.
# `latest` and `contest` are not test databases and must not read as one.
_TEST_NAME = re.compile(r"(?:^|[^a-z0-9])tests?(?:[^a-z0-9]|$)", re.IGNORECASE)

# Every loopback spelling is the same machine. `db` is Compose's own hostname for the same
# PostgreSQL this repo talks to from the host, so it is folded in too -- rule 2 is what
# actually catches that case, but leaving the two spellings looking different here would
# make rule 1 report "a different server" about one server.
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "db"})

_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}


def marker_for(identity: "DatabaseIdentity") -> str:
    """The disposability marker this exact database would carry."""
    return f"{DISPOSABLE_MARKER}:{identity.host}/{identity.database}"


class UnsafeTestDatabaseError(RuntimeError):
    """Raised instead of destroying a database that has not proved it is disposable."""


@dataclass(frozen=True)
class DatabaseIdentity:
    """A database URL reduced to the four things that decide whether two URLs are one
    database. The password is deliberately not among them and is never carried here."""

    backend: str
    host: str
    port: int | None
    database: str

    @property
    def location(self) -> str:
        """How this database is named in an error message, with no credentials in it."""
        if self.backend == "sqlite":
            return f"sqlite {self.database}"
        port = f":{self.port}" if self.port else ""
        return f"{self.backend}://{self.host}{port}/{self.database}"

    def same_server(self, other: "DatabaseIdentity") -> bool:
        return (self.backend, self.host, self.port) == (other.backend, other.host, other.port)


def identify(url: str) -> DatabaseIdentity:
    """The identity of the database `url` names, with the driver and credentials removed."""
    parts = urlsplit(async_database_url(url))
    backend = parts.scheme.split("+", 1)[0].lower()
    backend = "postgresql" if backend == "postgres" else backend
    database = parts.path.lstrip("/")
    if backend == "sqlite":
        # A file path is one database however it is spelled; `:memory:` is not a file.
        database = database if database in ("", ":memory:") else os.path.abspath(database)
        return DatabaseIdentity(backend, "", None, database)
    host = (parts.hostname or "").lower()
    host = "localhost" if host in _LOCAL_HOSTS else host
    return DatabaseIdentity(backend, host, parts.port or _DEFAULT_PORTS.get(backend), database)


# The application's own database, read once and then frozen -- see rule 1 in the module
# docstring. Reading it at import is what makes it the *real* one: pytest imports `conftest`
# and every test module before it runs a single fixture, so nothing has monkeypatched
# `DATABASE_URL` yet.
PROTECTED = identify(get_settings().database_url)
# Whether that came from the environment (or a `.env` this process could find) or from the
# built-in default. It changes nothing about the rules -- 3, 4 and 5 do not consult it -- but
# a refusal that names a database the operator never configured is confusing, and a *pass*
# on rules 1 and 2 means less than it looks like when the comparison was against a guess.
PROTECTED_IS_EXPLICIT = bool(os.environ.get("DATABASE_URL")) or "database_url" in (
    get_settings().model_fields_set
)


def _looks_like_a_test_database(identity: DatabaseIdentity) -> bool:
    """Whether the *name* reads as a test database.

    For SQLite the identity carries an absolute path, and the rule is applied to the file's
    own name and nothing above it: `/Users/me/code/tests/data/dev.db` is a development
    database that happens to live under a directory called `tests`, and matching the whole
    path would have made it droppable.
    """
    name = (
        os.path.basename(identity.database) if identity.backend == "sqlite" else identity.database
    )
    return bool(_TEST_NAME.search(name))


def _ci_says_it_is_disposable() -> bool:
    """Both, and both explicitly. `CI` alone is set by every hosted runner including ones
    that have a real database attached; the second variable is the deliberate statement that
    *this* database exists to be thrown away."""
    return _truthy(os.environ.get("CI")) and _truthy(
        os.environ.get("STORESPLIT_TEST_DB_IS_DISPOSABLE")
    )


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _refuse(identity: DatabaseIdentity, reason: str, remedy: str) -> UnsafeTestDatabaseError:
    provenance = (
        "" if PROTECTED_IS_EXPLICIT else " (the built-in default; DATABASE_URL is not set here)"
    )
    return UnsafeTestDatabaseError(
        f"Refusing to reset the database {identity.location}: {reason}\n"
        f"The application's own database is {PROTECTED.location}{provenance}.\n"
        f"{remedy}"
    )


def check_test_database_url(url: str) -> DatabaseIdentity:
    """Everything that can be decided from the URL alone. Raises, or returns the identity.

    Separate from the live check so it can be exercised without a server, and so the cheap
    refusals happen before anything opens a connection to the database in question.
    """
    identity = identify(url)
    name = identity.database

    if identity == PROTECTED:
        raise _refuse(
            identity,
            "it is the application's own database (TEST_DATABASE_URL == DATABASE_URL).",
            "Point TEST_DATABASE_URL at a dedicated, disposable database "
            f"(for example {PROTECTED.location}_test).",
        )
    if name and name == PROTECTED.database:
        raise _refuse(
            identity,
            f"its name is the application's own database name ({name!r}). Two hostnames can "
            "reach one server -- Compose's 'db' and the host's 'localhost' are the same "
            "PostgreSQL -- so a different host is not evidence of a different database.",
            f"Rename the test database, for example {name}_test.",
        )
    if name.lower() in NEVER_DISPOSABLE:
        raise _refuse(
            identity,
            f"{name!r} is a server-owned database and is never disposable.",
            "Create a database of your own for the tests and point TEST_DATABASE_URL at it.",
        )
    # `:memory:` dies with the process; there is nothing to protect.
    if identity.backend == "sqlite" and name == ":memory:":
        return identity
    if not _looks_like_a_test_database(identity) and not _ci_says_it_is_disposable():
        raise _refuse(
            identity,
            f"{name!r} does not read as a test database; the name must contain 'test' as a "
            "word of its own.",
            "Point TEST_DATABASE_URL at a dedicated test database, or -- only for a database "
            "a CI job created and will throw away -- set CI=1 and "
            "STORESPLIT_TEST_DB_IS_DISPOSABLE=1.",
        )
    return identity


async def _check_live_database(url: str, identity: DatabaseIdentity) -> None:
    """Ask the server what it actually opened, and whether that database is disposable."""
    try:
        engine = create_async_engine(async_database_url(url))
    except Exception as problem:
        # SQLAlchemy puts the whole URL, password included, in its parse errors. Every other
        # message this module produces is careful not to; this one must be too.
        raise _refuse(
            identity,
            f"the URL could not be parsed ({type(problem).__name__}).",
            "Check TEST_DATABASE_URL's spelling.",
        ) from None
    try:
        async with engine.begin() as connection:
            live = await connection.scalar(text("select current_database()"))
            live_name = str(live or "")
            if live_name != identity.database:
                raise _refuse(
                    identity,
                    f"the server opened {live_name!r}, not {identity.database!r}. The URL "
                    "does not describe the database it reaches.",
                    "Fix TEST_DATABASE_URL so it names the database it connects to.",
                )
            if live_name.lower() in NEVER_DISPOSABLE or (
                not _looks_like_a_test_database(replace(identity, database=live_name))
                and not _ci_says_it_is_disposable()
            ):
                raise _refuse(
                    identity,
                    f"the live connection reports {live_name!r}, which does not read as a "
                    "test database.",
                    "Point TEST_DATABASE_URL at a dedicated test database.",
                )

            marker = await connection.scalar(
                text(
                    "select shobj_description(oid, 'pg_database') from pg_database "
                    "where datname = current_database()"
                )
            )
            if marker == marker_for(identity):
                return

            # Every user relation, not just ordinary tables. `DROP SCHEMA public CASCADE`
            # takes materialized views, foreign tables, views and sequences with it, so a
            # database holding only those is not "nothing to lose" -- and counting only
            # 'r' and 'p' would have read a foreign-data wrapper over a production
            # database as empty and then stamped it disposable.
            tables = await connection.scalar(
                text(
                    "select count(*) from pg_class c join pg_namespace n on n.oid = "
                    "c.relnamespace where c.relkind in ('r', 'p', 'm', 'f', 'v', 'S') "
                    "and n.nspname not in ('pg_catalog', 'information_schema')"
                )
            )
            if tables:
                raise _refuse(
                    identity,
                    f"it holds {tables} relation(s) and does not carry this suite's "
                    "disposability marker, so it is somebody's database rather than a test "
                    "database this suite created and emptied.",
                    "The usual cause is that something else built a schema in it -- letting "
                    "`pytest` create and drop it is enough, so nothing needs to be migrated "
                    "into it first. If it really is disposable, drop it and let the tests "
                    "recreate it, or claim it once with:\n"
                    f'  COMMENT ON DATABASE "{identity.database}" '
                    f"IS '{marker_for(identity)}';",
                )
            # Empty: there is nothing to lose, so claim it for next time. Best effort --
            # stamping needs ownership, and failing to stamp an empty database is not a
            # reason to refuse to use one.
            await _claim(connection, identity)
    finally:
        await engine.dispose()


async def _claim(connection: AsyncConnection, identity: DatabaseIdentity) -> None:
    """Stamp an empty database as disposable, so rule 5 recognises it next time.

    `COMMENT ON` takes a literal, not a bind parameter, so both halves are escaped here:
    the name as an identifier, the marker as a string.
    """
    name = identity.database.replace('"', '""')
    marker = marker_for(identity).replace("'", "''")
    try:
        await connection.execute(text(f"comment on database \"{name}\" is '{marker}'"))
    except Exception:
        # Stamping needs ownership of the database. An empty database is safe to use whether
        # or not it can be claimed -- but the memo for next time is lost, so the moment it
        # holds anything it will be refused with a remedy the same role cannot run either.
        # That is worth a warning rather than a debug line nobody sees.
        logging.getLogger(__name__).warning(
            "could not mark %s disposable; it will be refused once it is no longer empty",
            identity.location,
        )


_verified: set[str] = set()


async def require_disposable_test_database(url: str) -> str:
    """Return `url`, having proved it names a database that exists to be destroyed.

    Every helper that drops a schema, drops every table or otherwise resets a database calls
    this first. Verification is remembered per URL so a per-test fixture pays for the live
    round trip once per session, not once per test.
    """
    if url in _verified:
        return url
    identity = check_test_database_url(url)
    if identity.backend == "postgresql":
        await _check_live_database(url, identity)
    elif not (identity.backend == "sqlite" and identity.database == ":memory:"):
        # Rules 4 and 5 are the ones that do not depend on a name, and they are written
        # against PostgreSQL's catalogue. Anywhere else there is no live confirmation to
        # have, so the name is the only evidence -- and a guard whose strongest rule
        # silently does not apply is not a guard. An in-memory SQLite database is the one
        # exception: it cannot outlive the process, so there is nothing to protect.
        raise _refuse(
            identity,
            f"{identity.backend!r} cannot be confirmed from the live connection; only "
            "PostgreSQL and in-memory SQLite are verifiable here.",
            "Point TEST_DATABASE_URL at a PostgreSQL test database, or at "
            "sqlite+aiosqlite:///:memory:.",
        )
    _verified.add(url)
    return url
