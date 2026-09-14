"""One refresh per (ZIP, category), shared by the automatic and the manual path.

A search that finds stale prices answers from what it has and revalidates behind the answer;
a shopper who presses "Refresh prices" asks for the same thing sooner. Both go through
`ensure_refresh`, so they cannot start two scrapes over the same data -- which matters more
than it sounds: `run_scrape` expires the offers a (store, category) search did not confirm,
so two overlapping runs would each delete what the other had not written yet.

Four things are enforced here:

* **Single flight.** A key with a live task starts nothing; the caller joins the one that is
  already running.
* **Cooldown.** `SEARCH_REFRESH_COOLDOWN_SECONDS` is the minimum spacing between refresh
  *starts* for one key. It is one number for both paths: it stops a failing key being retried
  on every keystroke, and it is what the manual button counts down from.
* **A durable floor.** The cooldown is not only in memory. `scrape_runs` already records when
  each run started, so a restarted process reads back a cooldown it cannot remember, and a
  second browser tab or a reload cannot re-arm the button.
* **A global budget.** `SEARCH_MAX_CONCURRENT_REFRESHES` caps refreshes in flight across every
  key, and the registry remembers a bounded number of keys. A per-key cooldown is not a
  budget: the key is chosen by the caller, so a thousand ZIPs are a thousand uncooled keys,
  each of which would queue a collection from ten real retailers. Both entry points are
  unauthenticated by design, so what has to be bounded is the total, not the repeat rate.
  Over budget, a refresh is refused rather than queued -- the caller is told to come back and
  the retailers are sent nothing.

The registry is process-local, which is the right scope for this MVP: one API process, one
Postgres, no Redis (see the repo's "No extra infrastructure" rule). The DB floor is what
makes it survive a restart.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import ScrapeRun
from app.normalize.categories import CATEGORIES
from app.retailers.clients import RetailerClients
from app.services.scraper import run_scrape

log = logging.getLogger("storesplit.services.refresh")

# The category component of a key when a refresh covers every category -- what the "no
# prices collected here yet" state needs, and what a query matching no category falls back
# to when a refresh is asked for explicitly.
ALL_CATEGORIES = "*"

RefreshState = Literal["started", "already_running", "cooling_down"]


@dataclass
class RefreshEntry:
    """What this process knows about one key's refreshes."""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass(frozen=True)
class RefreshStatus:
    """The refresh picture for one key, as the API reports it."""

    refreshing: bool
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    cooldown_seconds: int
    available_in_seconds: int

    @property
    def can_refresh(self) -> bool:
        return not self.refreshing and self.available_in_seconds == 0


def refresh_key(zip_code: str, category: str | None) -> tuple[str, str]:
    """Normalized (ZIP, category). A five-digit ZIP is the unit stores are looked up by."""
    return (zip_code.strip()[:5], category or ALL_CATEGORIES)


class RefreshRegistry:
    """The process's in-flight refreshes. One instance, created at import."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], RefreshEntry] = {}
        # The lock is created lazily rather than here. This registry is built at import, and
        # an `asyncio.Lock` binds to the first event loop that awaits it and then refuses to
        # be used from any other -- so a lock made at import time breaks the moment the
        # process runs a second loop, which is what a reloading dev server and every test
        # after the first one do. `RuntimeError: ... is bound to a different event loop`.
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        # Tasks are held here as well as on their entry: `asyncio` keeps only a weak
        # reference to a running task, so a task nobody holds can be garbage collected
        # mid-scrape.
        self._tasks: set[asyncio.Task[None]] = set()

    def _guard(self) -> asyncio.Lock:
        """This loop's lock, made on first use.

        Rebinding when the loop changes is safe: a lock bound to a loop that is no longer
        running has no waiters left to strand. Within one loop this returns the same object
        every time -- there is no await between the check and the assignment, so two
        coroutines cannot each make one.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def status(self, db: AsyncSession, key: tuple[str, str]) -> RefreshStatus:
        """What the API reports for this key, without starting anything."""
        settings = get_settings()
        cooldown = settings.search_refresh_cooldown_seconds
        async with self._guard():
            entry = self._entries.get(key)
            running = entry.running if entry else False
            started_at = entry.started_at if entry else None
            finished_at = entry.finished_at if entry else None
            last_error = entry.last_error if entry else None
        last_start = await self._last_start(db, key, cooldown, started_at)
        return RefreshStatus(
            refreshing=running,
            started_at=started_at,
            finished_at=finished_at,
            last_error=last_error,
            cooldown_seconds=cooldown,
            available_in_seconds=_seconds_left(last_start, cooldown),
        )

    async def ensure_refresh(
        self,
        db: AsyncSession,
        sessionmaker: async_sessionmaker[AsyncSession],
        clients: RetailerClients,
        key: tuple[str, str],
    ) -> tuple[RefreshState, RefreshStatus]:
        """Start a refresh for this key, or explain why one was not started.

        `already_running` and `cooling_down` are ordinary outcomes of a correct request, not
        errors: in both, no scrape begins, which is the whole enforcement.
        """
        settings = get_settings()
        cooldown = settings.search_refresh_cooldown_seconds
        zip_code, category = key

        async with self._guard():
            entry = self._entries.setdefault(key, RefreshEntry())
            if entry.running:
                return "already_running", self._status_from(entry, cooldown, entry.started_at)

            # A run over every category and a run over one of them cover the same offers, and
            # `run_scrape` expires whatever a (store, category) search did not confirm -- so
            # two overlapping keys running at once would each delete the other's work. They
            # only serialize today because `SCRAPE_MAX_CONCURRENT_RUNS` is 1, which is a
            # tuning knob the repo openly invites raising.
            overlapping = self._overlapping_key(key)
            if overlapping is not None:
                return "already_running", self._status_from(
                    overlapping, cooldown, overlapping.started_at
                )

            # Read the durable floor while holding the lock, so two requests arriving
            # together cannot both find the key free and both start a scrape.
            last_start = await self._last_start(db, key, cooldown, entry.started_at)
            if _seconds_left(last_start, cooldown) > 0:
                return "cooling_down", self._status_from(entry, cooldown, last_start)

            if self._in_flight() >= settings.search_max_concurrent_refreshes:
                # Refused, not queued. Reported as cooling down because that is what the
                # caller must do: wait and ask again.
                return "cooling_down", self._status_from(entry, cooldown, last_start)

            self._evict(settings.search_refresh_registry_max_keys, keep=key)

            now = datetime.now(UTC)
            entry.started_at = now
            entry.finished_at = None
            entry.last_error = None
            categories = None if category == ALL_CATEGORIES else [category]
            task = asyncio.create_task(
                self._run(sessionmaker, clients, zip_code, categories, entry),
                name=f"refresh:{zip_code}:{category}",
            )
            entry.task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            log.info(
                "refresh_started",
                extra={"zip_code": zip_code, "category": category, "trigger": "ensure_refresh"},
            )
            return "started", self._status_from(entry, cooldown, now)

    def _in_flight(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.running)

    def _overlapping_key(self, key: tuple[str, str]) -> RefreshEntry | None:
        """A running refresh for this ZIP whose categories overlap this key's."""
        zip_code, category = key
        for (other_zip, other_category), entry in self._entries.items():
            if other_zip != zip_code or not entry.running:
                continue
            if ALL_CATEGORIES in (category, other_category) or category == other_category:
                return entry
        return None

    def _evict(self, max_keys: int, *, keep: tuple[str, str]) -> None:
        """Forget the oldest idle keys once the registry grows past its bound.

        Dropping a key costs at most a remembered cooldown, and `scrape_runs` still supplies
        that, so eviction can never make a refused refresh permitted for longer than the
        durable floor allows.
        """
        if len(self._entries) <= max_keys:
            return
        idle = [
            (entry.started_at or _EPOCH, other)
            for other, entry in self._entries.items()
            if not entry.running and other != keep
        ]
        idle.sort()
        for _, other in idle[: len(self._entries) - max_keys]:
            self._entries.pop(other, None)

    async def _run(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        clients: RetailerClients,
        zip_code: str,
        categories: list[str] | None,
        entry: RefreshEntry,
    ) -> None:
        """The background scrape. It owns its own sessions; the request's is long gone.

        The fields written here are not taken under the lock. That is safe because each is a
        single assignment and there is no await between them, so no reader can observe a
        half-written entry -- but a future change that has to update two of them together
        must take the lock, or a reader will see one old value and one new.
        """
        try:
            runs = await run_scrape(sessionmaker, clients, zip_code, None, categories)
        except asyncio.CancelledError:
            entry.last_error = "cancelled"
            entry.finished_at = datetime.now(UTC)
            raise
        except Exception as error:
            entry.last_error = f"{type(error).__name__}: {error}"[:200]
            entry.finished_at = datetime.now(UTC)
            log.warning(
                "refresh_failed",
                extra={"zip_code": zip_code, "error": entry.last_error},
            )
            return
        # A run that recorded failures is not a failure of the refresh: the retailers that
        # answered were written, and their prices are now current. Naming the ones that did
        # not is what the compact error line in the UI is for.
        failed = [run.retailer_slug for run in runs if run.status == "failed"]
        entry.last_error = (
            f"{len(failed)} retailer(s) failed: {', '.join(sorted(failed))}"[:200]
            if failed
            else None
        )
        entry.finished_at = datetime.now(UTC)
        log.info(
            "refresh_finished",
            extra={
                "zip_code": zip_code,
                "retailers": len(runs),
                "failed": len(failed),
                "offers": sum(run.offers_written for run in runs),
            },
        )

    async def _last_start(
        self,
        db: AsyncSession,
        key: tuple[str, str],
        cooldown: int,
        remembered: datetime | None,
    ) -> datetime | None:
        """When a refresh for this key last started, in this process or a previous one.

        `scrape_runs` is the durable half. Rows are narrowed by ZIP and by start time first,
        so the scan is bounded by the cooldown window itself, and the JSON `categories` list
        is matched in Python -- JSON containment is spelled differently on SQLite and
        PostgreSQL, and this table is small enough that it does not need the index.
        """
        zip_code, category = key
        if cooldown <= 0:
            return remembered
        since = datetime.now(UTC) - timedelta(seconds=cooldown)
        rows = list(
            await db.scalars(
                select(ScrapeRun)
                .where(ScrapeRun.zip_code == zip_code, ScrapeRun.started_at >= since)
                .order_by(ScrapeRun.started_at.desc())
            )
        )
        starts = [remembered] if remembered else []
        for row in rows:
            if _covers(category, row.categories or []):
                starts.append(_aware(row.started_at))
        return max(starts) if starts else None

    def _status_from(
        self, entry: RefreshEntry, cooldown: int, last_start: datetime | None
    ) -> RefreshStatus:
        return RefreshStatus(
            refreshing=entry.running,
            started_at=entry.started_at,
            finished_at=entry.finished_at,
            last_error=entry.last_error,
            cooldown_seconds=cooldown,
            available_in_seconds=_seconds_left(last_start, cooldown),
        )

    async def aclose(self) -> None:
        """Cancel whatever is still refreshing. Called once, by the application lifespan."""
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            # Shutdown is best effort: a refresh that fails while being cancelled must not
            # stop the application from closing its clients and its database pool.
            with suppress(BaseException):
                await task
        self._tasks.clear()
        self._entries.clear()
        self._lock = None
        self._lock_loop = None

    def cancel_all(self) -> None:
        """Cancel outstanding refreshes without waiting for them.

        The synchronous half of `aclose`, for a caller that cannot await -- a test tearing
        down between cases. `aclose` is what the application lifespan uses, because on the
        way out the tasks must be *finished* before their clients and engine are closed.
        """
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._entries.clear()
        self._lock = None
        self._lock_loop = None

    def reset(self) -> None:
        """Forget everything, cancelling anything still running first.

        Tests only; production has one registry for the process. It delegates rather than
        clearing the dictionaries itself: dropping `_tasks` is dropping the only strong
        reference a running task has, which is precisely what that set exists to prevent.
        """
        self.cancel_all()


def _covers(category: str, ran: list[str]) -> bool:
    """Did a run over `ran` collect what this key asks for?

    The all-categories key is only satisfied by a run that really covered every category:
    collecting eggs five minutes ago says nothing about milk, so it must not lock out a
    request for the whole ZIP. A single-category key is satisfied by any run that included
    it -- which an all-categories run does.
    """
    if category == ALL_CATEGORIES:
        return set(CATEGORIES) <= set(ran)
    return category in ran


_EPOCH = datetime.min.replace(tzinfo=UTC)


def _seconds_left(last_start: datetime | None, cooldown: int) -> int:
    if last_start is None or cooldown <= 0:
        return 0
    elapsed = (datetime.now(UTC) - _aware(last_start)).total_seconds()
    return max(0, round(cooldown - elapsed))


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything here compares in UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


registry = RefreshRegistry()
