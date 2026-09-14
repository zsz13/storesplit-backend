"""Scrape orchestration: adapters -> normalization -> matching -> database.

Adapters return retailer-agnostic listings; this module decides which listings belong to a
category, parses package quantities, computes unit prices, matches canonical products and
writes current offers plus price history.

Shape of a run:

* **Fetch is concurrent.** Every retailer runs as its own task, and inside a retailer every
  (store, category) search runs concurrently too. Both fan-outs are bounded by a semaphore,
  so a run never launches an unbounded number of tasks. A retailer that fails or exceeds its
  deadline is recorded as failed and never cancels another retailer.
* **Ingest is sequential, in the requested retailer order.** Matching a listing against the
  canonical products is order dependent -- the second retailer's eggs merge into the first
  retailer's canonical product -- so writes stay serialized even though fetches overlap.
  Each retailer's ingest starts as soon as its own fetch finishes, while later retailers are
  still downloading.
* **Parsing, normalization, matching and arithmetic stay synchronous.** They are pure CPU
  work; only HTTP and the database are awaited.
"""

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import NamedTuple
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.concurrency import describe_exception, fanout_limit, gather_bounded, request_budget
from app.config import get_settings
from app.db.models import (
    STOCK_STATUS_MAX,
    CanonicalProduct,
    Offer,
    PriceHistory,
    Retailer,
    RetailerProduct,
    ScrapeRun,
    Store,
    utcnow,
)
from app.matching.ai_judge import get_judge
from app.matching.deterministic import MatchResult, ProductFeatures, match_product
from app.normalize.categories import CATEGORIES, Category, title_matches_category
from app.normalize.gtin import normalize_gtin
from app.normalize.hours import StoreHours, hours_from_unzoned, store_hours_to_json
from app.normalize.naming import extract_attributes, normalize_brand, normalize_title
from app.normalize.pricing import basis_quantity
from app.normalize.timezones import timezone_at
from app.normalize.unit_price import comparison_quantity, unit_price
from app.normalize.units import (
    Quantity,
    is_multipack,
    parse_quantity,
    quantize_money,
    quantize_unit_price,
)
from app.retailers import adapter_slugs, get_adapter, product_hosts
from app.retailers.base import (
    ProductListing,
    RetailerAdapter,
    StoreDetails,
    StoreDetailsFetcher,
    StoreLocation,
)
from app.retailers.clients import RetailerClients
from app.retailers.urls import valid_image_url, valid_product_url
from app.retailers.zipmatch import is_on_earth
from app.schemas import ScrapeRunOut
from app.services.maps import (
    PLACE_HOURS_SOURCE,
    MapsPlace,
    PlaceQuery,
    PlaceSchedule,
    fetch_place_schedule,
    place_from_retailer,
    resolve_place,
)

log = logging.getLogger("storesplit.scraper")

# Asks Google which business stands at a store's published address. Absent unless a key
# is configured, and never called for a store whose retailer named its own place.
type PlaceResolver = Callable[[PlaceQuery], Awaitable[MapsPlace | None]]
# Asks Google what a *verified* place's week and timezone are. `want_hours` is False for a
# store whose retailer already published its week and needs only the zone to read it in --
# which is the cheaper of the two Places SKUs, and is now reached only where the store's own
# coordinates cannot settle the zone offline (`normalize/timezones.py`).
type ScheduleResolver = Callable[[str, bool], Awaitable[PlaceSchedule | None]]


def _place_resolver(clients: RetailerClients, api_key: str) -> PlaceResolver | None:
    """The Places lookup, or None when no key is configured -- which is the default."""
    if not api_key:
        return None
    client = clients.shared()

    async def resolve(query: PlaceQuery) -> MapsPlace | None:
        return await resolve_place(client, query, api_key)

    return resolve


def _schedule_resolver(clients: RetailerClients, api_key: str) -> ScheduleResolver | None:
    """The Place Details lookup for hours, or None when no key is configured."""
    if not api_key:
        return None
    client = clients.shared()

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        return await fetch_place_schedule(client, place_id, api_key, want_hours=want_hours)

    return schedule


@dataclass
class IngestStats:
    seen: int = 0
    written: int = 0


@dataclass(frozen=True)
class SearchResult:
    """What one (store, category) search returned, or why it returned nothing."""

    location: StoreLocation
    category: Category
    listings: list[ProductListing]
    error: str | None = None


@dataclass
class RetailerFetch:
    """Everything one retailer downloaded, or why it downloaded nothing."""

    slug: str
    name: str
    # Hosts this retailer's product URLs may live on, taken from the adapter that produced
    # the fetch, so the check does not depend on a registry lookup by slug.
    product_hosts: frozenset[str] = frozenset()
    # When this retailer's own work began. Runs are all created together now, so the row's
    # insert time would say nothing about how long the retailer actually took.
    started_at: datetime = field(default_factory=utcnow)
    locations: list[StoreLocation] = field(default_factory=list)
    # Store external id -> what the retailer publishes about that store, or None when the
    # attempt found nothing. Only adapters with the optional `fetch_store_details` capability
    # fill this in, and only for stores whose cached copy has aged out. Recording the failed
    # attempts is what stops a store whose page 404s being re-read on every scrape.
    details: dict[str, StoreDetails | None] = field(default_factory=dict)
    # Google places resolved for stores whose retailer publishes none of its own. Empty
    # unless `GOOGLE_MAPS_API_KEY` is set; a retailer-published place always wins.
    places: dict[str, MapsPlace] = field(default_factory=dict)
    # What Google says about the week of a place already verified as one of these stores.
    # Empty unless `GOOGLE_MAPS_API_KEY` is set. It fills gaps only: a retailer that
    # published its own hours is never overridden by it.
    schedules: dict[str, PlaceSchedule] = field(default_factory=dict)
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None


# --------------------------------------------------------------------------- run

# One gate per event loop, so tests (a fresh loop each) never share a semaphore across loops.
_run_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _run_gate(limit: int) -> asyncio.Semaphore:
    """The process-wide gate on concurrent scrape runs.

    Two runs over one ZIP would fight over `expire_stale_offers`: each deletes the offers the
    other has not confirmed yet. Runs were effectively serial when scraping was sequential;
    now that a run finishes in seconds they would genuinely overlap, so the gate is explicit.
    """
    loop = asyncio.get_running_loop()
    gate = _run_gates.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(max(1, limit))
        _run_gates[loop] = gate
    return gate


async def run_scrape(
    sessionmaker: async_sessionmaker[AsyncSession],
    clients: RetailerClients,
    zip_code: str,
    retailer_slugs: list[str] | None = None,
    category_keys: list[str] | None = None,
) -> list[ScrapeRunOut]:
    get_judge()  # fails fast if the AI flag is on without an implementation
    settings = get_settings()
    slugs = retailer_slugs or adapter_slugs()
    keys = category_keys or list(CATEGORIES)
    unknown = [k for k in keys if k not in CATEGORIES]
    if unknown:
        raise ValueError(f"unknown categories: {', '.join(unknown)}")
    categories = [CATEGORIES[k] for k in keys]

    async with _run_gate(settings.scrape_max_concurrent_runs):
        run_ids = await _create_runs(sessionmaker, slugs, zip_code, keys)
        adapters = [get_adapter(slug, clients) for slug in slugs]
        retailer_slots = asyncio.Semaphore(settings.scrape_max_concurrent_retailers)
        # One read, before any fetching: which stores still hold fresh published details.
        async with sessionmaker() as session:
            fresh = {
                adapter.slug: await fresh_store_details(
                    session, adapter.slug, settings.store_details_ttl_seconds
                )
                for adapter in adapters
            }

        place_resolver = _place_resolver(clients, settings.google_maps_api_key)
        schedule_resolver = _schedule_resolver(clients, settings.google_maps_api_key)

        async def fetch(adapter: RetailerAdapter) -> RetailerFetch:
            async with retailer_slots:
                return await _guarded_fetch(
                    adapter,
                    zip_code,
                    categories,
                    fresh.get(adapter.slug, frozenset()),
                    read_store_details=settings.store_details_ttl_seconds > 0,
                    place_resolver=place_resolver,
                    schedule_resolver=schedule_resolver,
                )

        outcomes: list[ScrapeRunOut] = []
        abandon_reason: str | None = None
        failure: BaseException | None = None
        try:
            async with asyncio.timeout(settings.scrape_deadline_seconds):
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(fetch(adapter)) for adapter in adapters]
                    # Ingest in retailer order so matching sees the same history it saw when
                    # retailers ran one after another; the remaining fetches keep running.
                    for run_id, task in zip(run_ids, tasks, strict=True):
                        fetched = await task
                        outcomes.append(
                            await _ingest_fetch(sessionmaker, run_id, fetched, zip_code)
                        )
        except TimeoutError:
            abandon_reason = f"scrape deadline of {settings.scrape_deadline_seconds:g}s exceeded"
        except BaseException as exc:
            # Every run row is inserted up front, so anything that escapes here -- a dropped
            # connection on a commit, a cancelled request -- would otherwise leave the
            # remaining rows stuck at "running" with no finished_at, forever.
            abandon_reason = describe_exception(exc)
            failure = exc
        if abandon_reason is not None:
            try:
                outcomes += await _abandon_runs(
                    sessionmaker, run_ids[len(outcomes) :], abandon_reason
                )
            except Exception:  # the database is the thing that just failed; do not mask it
                log.exception("abandon_runs_failed")
        if failure is not None:
            raise failure
        return outcomes


async def _create_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    slugs: Sequence[str],
    zip_code: str,
    category_keys: list[str],
) -> list[int]:
    """One `scrape_runs` row per retailer, inserted in a single transaction."""
    async with sessionmaker() as session:
        runs = [
            ScrapeRun(retailer_slug=slug, zip_code=zip_code, categories=category_keys)
            for slug in slugs
        ]
        session.add_all(runs)
        await session.commit()
        return [run.id for run in runs]


def _needs_a_person(exc: BaseException) -> bool:
    """Is this the browser layer saying a human must confirm the session?

    Checked by name rather than by importing the browser package, so the scrape service
    keeps working -- and keeps its meaning -- when the optional extra is not installed.
    """
    return type(exc).__name__ == "ManualVerificationRequiredError"


async def fetch_retailer(
    adapter: RetailerAdapter,
    zip_code: str,
    categories: list[Category],
    *,
    max_stores: int,
    request_limit: int,
    timeout: float | None = None,
    fresh_details: frozenset[str] = frozenset(),
    read_store_details: bool = True,
    place_resolver: PlaceResolver | None = None,
    schedule_resolver: ScheduleResolver | None = None,
) -> RetailerFetch:
    """Download one retailer: its stores, then every (store, category) search concurrently.

    A search that fails or runs past the deadline is recorded on its own `SearchResult`; the
    searches that succeeded are still returned, so one bad category costs one category rather
    than the whole retailer -- which is what a sequential scrape gave, because it had already
    committed everything up to the failure.

    `request_limit` is one budget for the whole retailer: an adapter that fans out inside a
    search draws on the same slots, so the configured limit is the real number of requests in
    flight rather than a per-level allowance.
    """
    fetch = RetailerFetch(
        slug=adapter.slug, name=adapter.name, product_hosts=adapter_product_hosts(adapter)
    )
    if not adapter.is_configured():
        # Adapters that know why say so: "missing credentials" is wrong for a retailer whose
        # problem is that the browser fallback is switched off, and a wrong reason in a log
        # is time somebody spends looking in the wrong place.
        reason = getattr(adapter, "unconfigured_reason", None)
        detail = reason() if callable(reason) else "missing credentials"
        fetch.skipped_reason = f"{adapter.name} adapter is not configured ({detail})"
        return fetch
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    with request_budget(request_limit):
        try:
            async with asyncio.timeout_at(deadline):
                fetch.locations = (await adapter.find_stores(zip_code))[:max_stores]
        except TimeoutError:
            fetch.error = _deadline_error(adapter.name, timeout)
            log.warning("scrape_timeout", extra={"retailer": adapter.slug, "phase": "find_stores"})
            return fetch
        except Exception as exc:
            if _needs_a_person(exc):
                # Nothing is broken: the retailer asked for a human, and the browser layer
                # has left the page open for one. That is a retailer this run skipped, not a
                # fault -- reporting it as a failure would cry wolf on every scrape and bury
                # the failures that are real.
                fetch.skipped_reason = str(exc)
                log.warning(
                    "scrape_needs_verification",
                    extra={"retailer": adapter.slug, "phase": "find_stores"},
                )
                return fetch
            fetch.error = describe_exception(exc)
            log.exception("scrape_failed", extra={"retailer": adapter.slug})
            return fetch
        if not fetch.locations:
            log.warning("no_stores", extra={"retailer": adapter.slug, "zip_code": zip_code})
        # Hours are a garnish on a price. This never raises and never consumes the
        # retailer's deadline: a store page that hangs must not cost a scrape its prices.
        if read_store_details:
            fetch.details, fetch.places, fetch.schedules = await _store_details(
                adapter,
                fetch.locations,
                fresh_details,
                timeout,
                place_resolver,
                schedule_resolver,
            )
        pairs = [(store, category) for store in fetch.locations for category in categories]
        # An adapter whose searches share one resource may say how many of them can really run
        # at once. The browser-backed ones do: a retailer has a single page there, and that page
        # *is* the session, so its searches take turns whatever this launches. Launching a task
        # per pair anyway does not make them concurrent -- it only has the ones at the back of
        # the queue spend the retailer's deadline waiting for a page they never reach, and then
        # report a timeout for a search that never started. (The deadline is absolute, so
        # whichever search holds the page can still be cancelled mid-load; this changes how many
        # others are queued behind it, not that.)
        width = min(
            request_limit, max(1, getattr(adapter, "max_concurrent_searches", request_limit))
        )
        fetch.results = await gather_bounded(
            width,
            [
                partial(_search, adapter, store, category, deadline, timeout)
                for store, category in pairs
            ],
        )
    # The run is reported failed if any search failed, naming the first failure, as it did
    # when the first failing category aborted the retailer.
    fetch.error = next((r.error for r in fetch.results if r.error is not None), None)
    return fetch


async def _search(
    adapter: RetailerAdapter,
    store: StoreLocation,
    category: Category,
    deadline: float | None,
    timeout: float | None,
) -> SearchResult:
    """One (store, category) search. Never raises, so a sibling search is never cancelled."""
    try:
        async with asyncio.timeout_at(deadline):
            listings = await adapter.search_products(category.search_query, store)
    except TimeoutError:
        log.warning(
            "scrape_timeout",
            extra={
                "retailer": adapter.slug,
                "store": store.external_id,
                "category": category.key,
            },
        )
        return SearchResult(store, category, [], error=_deadline_error(adapter.name, timeout))
    except Exception as exc:
        detail = describe_exception(exc)
        if _needs_a_person(exc):
            # A challenge part-way through a retailer's categories. The searches that already
            # succeeded are kept and ingested; this one is recorded as unfinished rather than
            # broken, and the rest of the scrape carries on untouched.
            log.warning(
                "search_needs_verification",
                extra={
                    "retailer": adapter.slug,
                    "store": store.external_id,
                    "category": category.key,
                },
            )
        else:
            log.exception(
                "search_failed",
                extra={
                    "retailer": adapter.slug,
                    "store": store.external_id,
                    "category": category.key,
                },
            )
        return SearchResult(store, category, [], error=detail)
    return SearchResult(store, category, listings)


async def _store_details(
    adapter: RetailerAdapter,
    locations: list[StoreLocation],
    fresh: frozenset[str],
    timeout: float | None,
    resolve: PlaceResolver | None = None,
    schedule: ScheduleResolver | None = None,
) -> tuple[dict[str, StoreDetails | None], dict[str, MapsPlace], dict[str, PlaceSchedule]]:
    """Read published store details for the stores whose cached copy has aged out.

    `fetch_store_details` is an optional capability rather than part of the adapter Protocol:
    most retailers publish no hours anywhere robots.txt allows, and a method eight adapters
    would raise from is a worse contract than one they do not have.

    **Nothing here may fail a scrape.** These are opening times; the run exists to collect
    prices. So the whole phase is wrapped: a raising adapter, a hanging store page, a
    capability with the wrong signature -- each costs the hours for that retailer and nothing
    else. It also runs under its own timeout, so it cannot eat the retailer deadline and
    starve the searches that follow it.

    Every store asked about appears in the result, mapped to `None` when nothing was found,
    so the caller can record that the attempt happened.
    """
    capability: StoreDetailsFetcher | None = getattr(adapter, "fetch_store_details", None)
    wanted = [store for store in locations if store.external_id not in fresh]
    if not wanted:
        return {}, {}, {}
    found: dict[str, StoreDetails | None] = {store.external_id: None for store in wanted}
    if capability is None:
        # No published details to read, but a retailer with an address still deserves a map
        # link that opens its shop rather than its street. Every store looked at is still
        # reported -- mapped to `None`, meaning "asked, found nothing" -- so the ingest stamps
        # it and the weekly gate covers the place lookup as well. Reporting nothing here is
        # what made a store whose place failed to resolve get sent to the billed Places API
        # again on every single scrape, for ever.
        places = await _store_places(adapter, wanted, {}, timeout, resolve)
        return found, places, await _store_schedules(adapter, wanted, {}, places, timeout, schedule)
    try:
        async with asyncio.timeout(timeout):
            results = await gather_bounded(
                fanout_limit(), [partial(capability, store) for store in wanted]
            )
    except Exception as exc:
        log.warning(
            "store_details_failed",
            extra={"retailer": adapter.slug, "error": describe_exception(exc)},
        )
        return found, {}, {}
    for store, details in zip(wanted, results, strict=True):
        if isinstance(details, StoreDetails):
            found[store.external_id] = details
        else:
            log.info(
                "store_details_missing",
                extra={"retailer": adapter.slug, "store": store.external_id},
            )
    places = await _store_places(adapter, wanted, found, timeout, resolve)
    return found, places, await _store_schedules(adapter, wanted, found, places, timeout, schedule)


async def _store_places(
    adapter: RetailerAdapter,
    wanted: list[StoreLocation],
    details: dict[str, StoreDetails | None],
    timeout: float | None,
    resolve: PlaceResolver | None,
) -> dict[str, MapsPlace]:
    """Identify the business at each store's address, for stores whose retailer named none.

    Skipped entirely with no API key configured, which is the default: Target and Safeway
    publish their own Google listings and everyone else falls back to an address search.
    A store the retailer already identified is never looked up -- the business naming its own
    listing outranks anything a search could conclude about it.

    Wrapped and timed out like the details fetch beside it, and for the same reason: this is
    a map link, and a map link must never cost a scrape its prices.
    """
    if resolve is None:
        return {}

    def already_named(store: StoreLocation) -> bool:
        return bool((details.get(store.external_id) or _NO_DETAILS).maps_place_url)

    pending = [s for s in wanted if s.address_line1 and not already_named(s)]
    if not pending:
        return {}
    queries = [_place_query(adapter, store, details.get(store.external_id)) for store in pending]
    try:
        async with asyncio.timeout(timeout):
            results = await gather_bounded(fanout_limit(), [partial(resolve, q) for q in queries])
    except Exception as exc:
        log.warning(
            "store_places_failed",
            extra={"retailer": adapter.slug, "error": describe_exception(exc)},
        )
        return {}
    found: dict[str, MapsPlace] = {}
    for store, place in zip(pending, results, strict=True):
        if isinstance(place, MapsPlace):
            found[store.external_id] = place
        else:
            log.info(
                "store_place_unresolved",
                extra={"retailer": adapter.slug, "store": store.external_id},
            )
    return found


async def _store_schedules(
    adapter: RetailerAdapter,
    wanted: list[StoreLocation],
    details: dict[str, StoreDetails | None],
    places: dict[str, MapsPlace],
    timeout: float | None,
    schedule: ScheduleResolver | None,
) -> dict[str, PlaceSchedule]:
    """Ask Google about the week of places already verified as these stores.

    Two rules keep this narrow. It runs **only for a verified place** -- one the retailer
    published for itself, or one `pick_place` accepted on brand *and* house number *and*
    street -- because a place found by a bare address search could be the business next door,
    and the hours of the wrong shop are worse than no hours. And it runs **only to fill a
    gap**: a retailer that published its own week is never asked about, and neither is one
    that published a week whose zone the store's own coordinates already settle (Trader Joe's,
    wherever its locator places the store). What is left -- a week with no derivable zone --
    is asked for the zone alone, the cheaper of the two Places SKUs.

    Wrapped and timed out like the two phases beside it, and for the same reason: these are
    opening times, and opening times must never cost a scrape its prices.
    """
    if schedule is None:
        return {}
    pending: list[tuple[str, str, bool]] = []
    for store in wanted:
        published = details.get(store.external_id) or _NO_DETAILS
        if published.hours is not None:
            continue  # the retailer stated its own week; nothing to ask about
        if _week_needs_only_a_zone(published) and _derivable_zone(store, published):
            # The retailer published the week and the coordinates place it in a zone, so the
            # whole of what Google was ever asked here is already known offline. This was one
            # billed lookup per Trader Joe's store per week for a fact its own address implies.
            continue
        place = place_from_retailer(
            published.maps_place_url, source=published.source or "retailer"
        ) or places.get(store.external_id)
        if place is None or not place.place_id:
            continue
        pending.append((store.external_id, place.place_id, published.unzoned_hours is None))
    if not pending:
        return {}
    try:
        async with asyncio.timeout(timeout):
            results = await gather_bounded(
                fanout_limit(),
                [partial(schedule, place_id, want_hours) for _, place_id, want_hours in pending],
            )
    except Exception as exc:
        log.warning(
            "store_schedules_failed",
            extra={"retailer": adapter.slug, "error": describe_exception(exc)},
        )
        return {}
    found: dict[str, PlaceSchedule] = {}
    for (external_id, _, _), answer in zip(pending, results, strict=True):
        if isinstance(answer, PlaceSchedule):
            found[external_id] = answer
        else:
            log.info(
                "store_schedule_unresolved",
                extra={"retailer": adapter.slug, "store": external_id},
            )
    return found


# A stand-in for "the retailer published nothing", so the check above reads as one expression.
_NO_DETAILS = StoreDetails(external_id="")


def _derivable_zone(store: StoreLocation, details: StoreDetails | None) -> str | None:
    """The zone this store's own coordinates stand in, decided from the payload alone.

    Deliberately no `Store`: this runs in the fetch phase, which holds no row, and keeping it
    that way is what lets the saving be taken without threading the database through three
    signatures. The row's zone is consulted later, in `_resolved_hours`, where it is at hand.

    **It must never be more permissive than the write it is deciding against.** Skipping the
    lookup here and then failing to derive at ingest is the one outcome worse than paying for
    it: the store keeps no hours *and* the paid fallback was declined, and because every
    attempt is stamped it stays that way for a week with nothing in the logs. So the
    coordinates are read as an atomic pair, published over locator, which is exactly how
    `upsert_store` and `apply_store_details` write them onto the row -- a record stating a
    latitude and no longitude contributes neither, where a field-wise coalesce through
    `_place_query` would mix one source's latitude with another's longitude and derive a zone
    for a point that no payload describes.
    """
    published = details or _NO_DETAILS
    if is_on_earth(published.latitude, published.longitude):
        return timezone_at(published.latitude, published.longitude)
    return timezone_at(store.latitude, store.longitude)


def _week_needs_only_a_zone(details: StoreDetails) -> bool:
    """True where the retailer published weekday windows and nothing but a zone is missing.

    `is not None` is not enough on its own: `hours_from_unzoned` refuses an empty week
    (`hours_from_weekly` returns None when there is nothing to read), so an `UnzonedHours`
    carrying no days would let the fetch phase decline the paid lookup for a store the ingest
    can then write no hours for. This tests what the ingest will actually accept.
    """
    return details.unzoned_hours is not None and bool(details.unzoned_hours.weekly)


def _place_query(
    adapter: RetailerAdapter, store: StoreLocation, details: StoreDetails | None
) -> PlaceQuery:
    """What is known about a store, preferring its own page over the locator that found it."""
    published = details or _NO_DETAILS
    return PlaceQuery(
        retailer_name=adapter.name,
        store_name=published.name or store.name,
        address_line1=published.address_line1 or store.address_line1 or "",
        city=published.city or store.city,
        state=published.state or store.state,
        zip_code=published.zip_code or store.zip_code,
        latitude=published.latitude if published.latitude is not None else store.latitude,
        longitude=published.longitude if published.longitude is not None else store.longitude,
    )


async def fresh_store_details(session: AsyncSession, slug: str, ttl_seconds: int) -> frozenset[str]:
    """External ids of this retailer's stores read recently enough to leave alone.

    One stamp gates the whole weekly read -- published details *and* the Google place lookup
    -- because they happen together and cost the same visit. `stores.maps_updated_at` records
    when a place was last confirmed and by what, for auditing; it is not a second gate, and
    reading it as one would let a store be re-queried on a schedule of its own.

    A TTL of zero switches store details off entirely (see `Settings`), and the caller does
    not ask; nothing here is reached in that case.
    """
    if ttl_seconds <= 0:
        return frozenset()
    cutoff = utcnow() - timedelta(seconds=ttl_seconds)
    rows = await session.scalars(
        select(Store.external_id)
        .join(Retailer, Retailer.id == Store.retailer_id)
        .where(
            Retailer.slug == slug,
            Store.hours_updated_at.is_not(None),
            Store.hours_updated_at > cutoff,
        )
    )
    return frozenset(rows)


def _deadline_error(name: str, timeout: float | None) -> str:
    budget = "its" if timeout is None else f"its {timeout:g}s"
    return f"TimeoutError: {name} exceeded {budget} scrape deadline"


async def _guarded_fetch(
    adapter: RetailerAdapter,
    zip_code: str,
    categories: list[Category],
    fresh_details: frozenset[str] = frozenset(),
    read_store_details: bool = True,
    place_resolver: PlaceResolver | None = None,
    schedule_resolver: ScheduleResolver | None = None,
) -> RetailerFetch:
    """`fetch_retailer` with its deadline applied. Never raises.

    This is what keeps one slow or broken retailer from taking the run down with it.
    """
    settings = get_settings()
    started_at = utcnow()
    try:
        fetch = await fetch_retailer(
            adapter,
            zip_code,
            categories,
            max_stores=settings.scrape_stores_per_retailer,
            request_limit=settings.scrape_max_concurrent_requests_per_retailer,
            timeout=settings.scrape_retailer_timeout_seconds,
            fresh_details=fresh_details,
            read_store_details=read_store_details,
            place_resolver=place_resolver,
            schedule_resolver=schedule_resolver,
        )
    except Exception as exc:  # one retailer failing must not stop the others
        log.exception("scrape_failed", extra={"retailer": adapter.slug})
        return RetailerFetch(
            slug=adapter.slug,
            name=adapter.name,
            product_hosts=adapter_product_hosts(adapter),
            started_at=started_at,
            error=describe_exception(exc),
        )
    fetch.started_at = started_at
    return fetch


async def scrape_retailer(
    session: AsyncSession,
    adapter: RetailerAdapter,
    zip_code: str,
    categories: list[Category],
    max_stores: int,
) -> IngestStats:
    """Fetch and ingest one retailer into an existing session, without run bookkeeping."""
    settings = get_settings()
    fetch = await fetch_retailer(
        adapter,
        zip_code,
        categories,
        max_stores=max_stores,
        request_limit=settings.scrape_max_concurrent_requests_per_retailer,
        fresh_details=await fresh_store_details(
            session, adapter.slug, settings.store_details_ttl_seconds
        ),
        read_store_details=settings.store_details_ttl_seconds > 0,
    )
    return await ingest_retailer(session, fetch, zip_code)


async def _ingest_fetch(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: int,
    fetch: RetailerFetch,
    zip_code: str,
) -> ScrapeRunOut:
    """Write one retailer's download and close out its scrape run."""
    async with sessionmaker() as session:
        run = await _require_run(session, run_id)
        searched = [result for result in fetch.results if result.error is None]
        if fetch.skipped_reason is not None:
            run.status = "skipped"
            run.error = fetch.skipped_reason
            log.warning("scrape_skipped", extra={"retailer": fetch.slug, "reason": run.error})
        elif fetch.error is not None and not searched:
            # Nothing came back at all: record the failure and write nothing, as a sequential
            # run did when its first search raised.
            run.status = "failed"
            run.error = fetch.error[:2000]
        else:
            try:
                stats = await ingest_retailer(session, fetch, zip_code)
            except Exception as exc:
                await session.rollback()
                run = await _require_run(session, run_id)
                run.status = "failed"
                run.error = describe_exception(exc)[:2000]
                log.exception("ingest_failed", extra={"retailer": fetch.slug})
            else:
                run.products_seen = stats.seen
                run.offers_written = stats.written
                if fetch.error is None:
                    run.status = "succeeded"
                else:
                    # Some searches succeeded and are now committed; the run still reports
                    # the failure so a partial refresh is never mistaken for a clean one.
                    run.status = "failed"
                    run.error = fetch.error[:2000]
        # Set after any rollback, so a failed ingest still reports when the retailer began.
        run.started_at = fetch.started_at
        run.finished_at = utcnow()
        await session.commit()
        log.info(
            "scrape_run",
            extra={
                "retailer": run.retailer_slug,
                "status": run.status,
                "seen": run.products_seen,
                "written": run.offers_written,
            },
        )
        return ScrapeRunOut.model_validate(run, from_attributes=True)


async def _abandon_runs(
    sessionmaker: async_sessionmaker[AsyncSession], run_ids: Sequence[int], reason: str
) -> list[ScrapeRunOut]:
    """Close out runs whose retailer was still going when the overall deadline fired."""
    async with sessionmaker() as session:
        abandoned: list[ScrapeRun] = []
        for run_id in run_ids:
            run = await _require_run(session, run_id)
            run.status = "failed"
            run.error = f"TimeoutError: {reason}"
            run.finished_at = utcnow()
            abandoned.append(run)
        await session.commit()
        log.warning("scrape_runs_abandoned", extra={"abandoned": len(abandoned), "reason": reason})
        return [ScrapeRunOut.model_validate(run, from_attributes=True) for run in abandoned]


async def _require_run(session: AsyncSession, run_id: int) -> ScrapeRun:
    run = await session.get(ScrapeRun, run_id)
    if run is None:
        raise LookupError(f"scrape run {run_id} disappeared")
    return run


# --------------------------------------------------------------------------- ingest


async def ingest_retailer(
    session: AsyncSession, fetch: RetailerFetch, zip_code: str
) -> IngestStats:
    """Persist one retailer's search results, one transaction per (store, category)."""
    retailer = await upsert_retailer(session, fetch.slug, fetch.name)
    stores: dict[str, Store] = {}
    for location in fetch.locations:
        store = await upsert_store(session, retailer, location, zip_code)
        attempted = location.external_id in fetch.details
        resolved = fetch.places.get(location.external_id)
        scheduled = fetch.schedules.get(location.external_id)
        if attempted or resolved is not None or scheduled is not None:
            apply_store_details(
                store,
                fetch.details.get(location.external_id),
                utcnow(),
                place=resolved,
                schedule=scheduled,
            )
        stores[location.external_id] = store
    stats = IngestStats()
    for result in fetch.results:
        if result.error is not None:
            # A failed search confirms nothing. Ingesting it would expire every offer this
            # store has in the category and call the empty result the current truth.
            continue
        store = stores[result.location.external_id]
        now = utcnow()
        batch = await load_batch(session, retailer, store, result.category, result.listings)
        kept = 0
        for listing in result.listings:
            stats.seen += 1
            if await ingest_listing(
                session,
                batch,
                retailer,
                store,
                result.category,
                listing,
                now,
                fetch.product_hosts,
            ):
                stats.written += 1
                kept += 1
        expired = await expire_stale_offers(session, store, result.category, now)
        await session.commit()
        log.info(
            "scraped_category",
            extra={
                "retailer": fetch.slug,
                "store": result.location.external_id,
                "category": result.category.key,
                "listings": len(result.listings),
                "kept": kept,
                "expired": expired,
            },
        )
    return stats


async def upsert_retailer(session: AsyncSession, slug: str, name: str) -> Retailer:
    retailer = await session.scalar(select(Retailer).where(Retailer.slug == slug))
    if retailer is None:
        retailer = Retailer(slug=slug, name=name)
        session.add(retailer)
        await session.flush()
    return retailer


async def upsert_store(
    session: AsyncSession,
    retailer: Retailer,
    location: StoreLocation,
    served_zip: str | None = None,
) -> Store:
    store = await session.scalar(
        select(Store).where(
            Store.retailer_id == retailer.id, Store.external_id == location.external_id
        )
    )
    if store is None:
        store = Store(retailer_id=retailer.id, external_id=location.external_id, name=location.name)
        session.add(store)
    store.name = location.name
    store.address_line1 = location.address_line1
    store.city = location.city
    store.state = location.state
    store.zip_code = location.zip_code
    # **A locator that says nothing about where a store is has not moved it.** These two are
    # the only fields on this row a *second* source also writes: `apply_store_details` puts
    # the coordinates from a retailer's own store page here, weekly, and every locator that
    # publishes none leaves `StoreLocation.latitude` at its `None` default. Assigning that
    # `None` straight through meant the next ingest -- minutes later, long inside the
    # seven-day details TTL that stops the page being re-read -- erased the better answer,
    # and the store fell back to the centroid of its ZIP for ranking and lost its map pin.
    # A stated coordinate is still overwritten by a stated coordinate: this only refuses to
    # let an absence beat a fact, exactly as `apply_store_details` already does for the name
    # and the address. `is_on_earth` gates it for the same reason it gates the other writer:
    # `stores_near` measures every store on every request, and `math.sin(inf)` raises.
    if is_on_earth(location.latitude, location.longitude):
        store.latitude = location.latitude
        store.longitude = location.longitude
    if served_zip:
        zip5 = served_zip.strip()[:5]
        if zip5 not in (store.served_zip_codes or []):
            store.served_zip_codes = [*(store.served_zip_codes or []), zip5]
    await session.flush()
    return store


def apply_store_details(
    store: Store,
    details: StoreDetails | None,
    now: datetime,
    *,
    place: MapsPlace | None = None,
    schedule: PlaceSchedule | None = None,
) -> None:
    """Fold a retailer's own statement about a store into its row.

    The store's own page outranks whatever the locator said: it is where the retailer
    publishes the name over the door and the address on it. Only fields it actually states
    are written, so a page that omits the city does not erase one already known.

    Every value here came out of a retailer's HTML, so each is bounded before it is written.
    An over-length string would otherwise be rejected by PostgreSQL *inside the ingest
    transaction*, rolling back that retailer's prices for the sake of a store name, and a
    coordinate that is not a point on Earth would break the distance ranking every search
    performs. Neither is worth a price.

    `details` is `None` when the fetch was attempted and found nothing. The stamp is written
    anyway: without it, a store whose page 404s is re-read on every scrape -- every five
    minutes -- rather than once a week.
    """
    if details is not None:
        store.name = _bounded(details.name, 200) or store.name
        store.address_line1 = _bounded(details.address_line1, 200) or store.address_line1
        store.city = _bounded(details.city, 100) or store.city
        store.state = _bounded(details.state, 50) or store.state
        store.zip_code = _bounded(details.zip_code, 10) or store.zip_code
        if is_on_earth(details.latitude, details.longitude):
            store.latitude = details.latitude
            store.longitude = details.longitude
        store.phone = _bounded(details.phone, 32) or store.phone
    hours, hours_source = _resolved_hours(store, details, schedule)
    if hours is not None:
        store.timezone = _bounded(hours.timezone, 64)
        store.hours = store_hours_to_json(hours)
        store.hours_source = _bounded(hours_source, 100) or store.hours_source
    elif details is not None:
        store.hours_source = _bounded(details.source, 100) or store.hours_source
    # A place the retailer named for itself beats one resolved on its behalf, always.
    published = place_from_retailer(
        details.maps_place_url if details else None,
        source=(details.source if details else None) or "retailer",
    )
    _apply_maps_place(store, published or place, now)
    store.hours_updated_at = now


def _resolved_hours(
    store: Store, details: StoreDetails | None, schedule: PlaceSchedule | None
) -> tuple[StoreHours | None, str | None]:
    """This store's week and where it came from, preferring the retailer at every step.

    The ladder, highest first:

    1. **A week the retailer published with its own zone.** Nothing else is consulted.
    2. **A week the retailer published without a zone, read in a zone from elsewhere**
       -- Google's for this verified place, then the zone the store's own coordinates stand
       in, then the one already on the row. The clock is still the retailer's; only the frame
       it is read in is borrowed, and the source recorded stays the retailer's own.
    3. **Google's week for the verified place**, for a retailer that publishes none anywhere
       StoreSplit may read it -- and only where the row does not already hold a retailer's
       own answer. "First-party wins" has to be a fact about the *row*, not about this call:
       a store page that 500s once leaves `details.hours` None for that run, and without
       this check one bad afternoon would replace Safeway's week, dated holidays and all,
       with Google's and restamp its source.
    4. **Nothing**, which stays "Hours not published" rather than becoming a guess.

    **Why the derived zone sits above the row's own and not below it.** `apply_store_details`
    writes every resolved zone back to `store.timezone`, so a derived zone becomes a *cached*
    derived zone on the next pass. Consulted first, the cache would outrank the very source
    it came from and no later read could ever correct it: one locator payload with a dropped
    minus sign on the longitude writes a zone half a world away, and the store keeps it for
    good, because every subsequent scrape reads the poisoned row before the now-correct
    coordinates. Deriving first makes the answer recomputed rather than remembered, so a bad
    coordinate costs one scrape instead of being permanent. The row keeps the rung below,
    where it still serves a store whose coordinates nobody publishes. A zone Google *stated*
    for a verified place stays above both -- that one is an observation, not a derivation.

    **The derived zone is used only for the retailer's own week.** Rung 3 reads Google's
    hours in a zone Google or the row stated, never in one derived here. Google is asked for
    `timeZone` in the same request as `regularOpeningHours`, so a reply carrying hours and no
    zone is a malformed answer rather than a gap worth filling, and this function's job is to
    stop first-party hours being discarded -- not to make third-party hours storable where
    they were not before.
    """
    published = details or _NO_DETAILS
    if published.hours is not None:
        return published.hours, published.source
    stated_zone = (schedule.timezone if schedule else None) or store.timezone
    retailers_own = hours_from_unzoned(
        published.unzoned_hours,
        (schedule.timezone if schedule else None)
        or timezone_at(store.latitude, store.longitude)
        or store.timezone,
    )
    if retailers_own is not None:
        return retailers_own, published.source
    if schedule is not None and _google_may_write(store):
        return hours_from_unzoned(schedule.hours, stated_zone), PLACE_HOURS_SOURCE
    return None, None


def _google_may_write(store: Store) -> bool:
    """True where the row holds no hours, or holds hours Google itself wrote last time.

    The second case is what keeps a Google-sourced week refreshable; the first is what keeps
    it out of a row a retailer has already answered for.
    """
    return not store.hours or store.hours_source == PLACE_HOURS_SOURCE


def _apply_maps_place(store: Store, place: MapsPlace | None, now: datetime) -> None:
    """Keep a Google place once it has passed the link rules, and only then.

    Nothing that fails validation is written, because the column becomes an `href`: the same
    reasoning that put `clean_product_url` in front of every product link. A store with no
    accepted place keeps its NULLs and gets the address search instead, which is a worse
    link and never a wrong one.
    """
    if place is None:
        return
    store.maps_place_url = _bounded(place.url, 500)
    store.maps_place_id = _bounded(place.place_id, 128)
    store.maps_source = _bounded(place.source, 100)
    store.maps_updated_at = now


def _bounded(value: str | None, limit: int) -> str | None:
    """A retailer's string, cut to what the column can hold."""
    if value is None:
        return None
    text = value.strip()
    return text[:limit] if text else None


def adapter_product_hosts(adapter: RetailerAdapter) -> frozenset[str]:
    """The hosts an adapter's product URLs may use, from the site it declares.

    `site_url` is part of the adapter contract, so it is read directly: defaulting it would
    silently turn a missing declaration into "no host is valid", which drops every URL that
    retailer produces and only whispers about it in a log line.
    """
    host = urlsplit(adapter.site_url).hostname
    return frozenset({host}) if host else product_hosts(adapter.slug)


def _persistable_url(
    listing: ProductListing, retailer_slug: str, hosts: frozenset[str]
) -> str | None:
    """`listing.product_url` when it is a real page on this retailer's host, else None."""
    if listing.product_url is None:
        return None
    if valid_product_url(listing.product_url, hosts=hosts or product_hosts(retailer_slug)):
        return listing.product_url
    log.warning(
        "rejected_product_url",
        extra={
            "retailer": retailer_slug,
            "sku": listing.retailer_sku,
            "url": str(listing.product_url)[:200],
        },
    )
    return None


def _persistable_image_url(listing: ProductListing, retailer_slug: str) -> str | None:
    """`listing.image_url` when it is a real https image URL, else None.

    The image counterpart of `_persistable_url`, and the reason it exists: `image_url` was
    the one URL column with no gate at any layer, so three adapters' `str()` calls could
    persist `"{'url': None}"` -- a value the column accepts and the browser then resolves
    against StoreSplit's own origin. The host is deliberately not pinned: a retailer's
    images live on a CDN that is not its product host.
    """
    if listing.image_url is None:
        return None
    if valid_image_url(listing.image_url):
        return listing.image_url
    log.warning(
        "rejected_image_url",
        extra={
            "retailer": retailer_slug,
            "sku": listing.retailer_sku,
            "url": str(listing.image_url)[:200],
        },
    )
    return None


def listing_quantity(listing: ProductListing) -> Quantity | None:
    """What `listing.price` buys -- the size the unit price divides by.

    **A price that is already a rate buys one unit of its own basis, and nothing else is
    read.** This is the whole of the double-normalization fix. The old rule reached for a
    parsed package size first and fell back to one pound only when *no* size could be found,
    which is silent for a retailer that keeps the weight out of the title and catastrophic
    for one that puts it in: Target publishes "Boneless & Skinless Chicken Breast Value Pack
    - 2.5-5.25lbs - price per lb" with `current_retail: 2.59`, and taking 5.25 lb off that
    title divided a per-pound price by a pound count it had already accounted for --
    $2.59/lb became $0.49/lb, five times too cheap and ranked first.

    The size string and the title are read only where the retailer priced the package as a
    whole, which is the one case where a division is the right arithmetic.
    """
    basis = basis_quantity(listing.price_basis)
    if basis is not None:
        return basis
    if is_multipack(listing.title) or is_multipack(listing.size_text):
        return None
    return parse_quantity(listing.size_text) or parse_quantity(listing.title)


# --------------------------------------------------------------------------- batch state


class CanonicalIndex:
    """The category's canonical products, read once per batch instead of once per listing.

    Reloading the whole category for every listing was the scrape's worst N+1. The matcher is
    a pure function of the candidate list, so holding that list in memory and keeping it in
    step with the rows this batch writes gives identical results in one query.
    """

    def __init__(self, products: list[CanonicalProduct]) -> None:
        self._by_id = {product.id: product for product in products}
        self._features = [_features_of(product) for product in products]

    @property
    def features(self) -> list[ProductFeatures]:
        return self._features

    def find(self, canonical_id: int) -> CanonicalProduct | None:
        """The product, or None when it belongs to a category this batch did not load."""
        return self._by_id.get(canonical_id)

    def get(self, canonical_id: int) -> CanonicalProduct:
        product = self._by_id.get(canonical_id)
        if product is None:
            raise LookupError(f"canonical product {canonical_id} is not in this batch's index")
        return product

    def add(self, product: CanonicalProduct) -> None:
        """Record a canonical product created by this batch (already flushed, so it has an id)."""
        self._by_id[product.id] = product
        self._features.append(_features_of(product))

    def refresh(self, product: CanonicalProduct) -> None:
        """Re-read a product whose fields changed (a GTIN backfill) into the candidate list."""
        for index, features in enumerate(self._features):
            if features.canonical_id == product.id:
                self._features[index] = _features_of(product)
                return


def _features_of(product: CanonicalProduct) -> ProductFeatures:
    return ProductFeatures(
        category=product.category,
        title=product.normalized_name,
        brand=product.brand,
        comparison_quantity=product.comparison_quantity,
        count=product.count,
        gtin=product.gtin,
        canonical_id=product.id,
        # The stored reading of how this product is priced, written when it was created.
        sold_by=str((product.attributes or {}).get("sold_by") or "unit"),
    )


class PriceSnapshot(NamedTuple):
    """Everything that has to be equal for a scrape to decide the price did not change.

    Named rather than a bare tuple because it is assembled in two places -- from a listing
    and from the newest history row -- and two of its fields are adjacent money columns: a
    reordering or a sixth field would type-check clean and silently stop recording history,
    which is the one rule the whole feature rests on.

    **Every field is quantized to the scale of the column that stores it**, and that is not
    cosmetic. `price` lands in `Numeric(10, 2)`, so a retailer publishing `4.999` is read
    back from the database as `5.00` while the in-memory value is still `4.999`; the two
    never compare equal, and a byte-identical payload writes a new row on every scrape --
    288 a day at the five-minute refresh cadence, for a price that never moved. Kroger and
    Whole Foods both build prices straight from their payloads without rounding, so this is
    a live path and not a hypothetical one.
    """

    price: Decimal
    regular_price: Decimal
    loyalty_price: Decimal | None
    unit_price: Decimal | None
    # NULL only for a row whose basis was never recorded; a scrape always knows it.
    price_basis: str | None


def price_snapshot(
    price: Decimal,
    regular_price: Decimal,
    loyalty_price: Decimal | None,
    unit_price: Decimal | None,
    price_basis: str | None,
) -> PriceSnapshot:
    """A snapshot as the database will hold it, so a round trip cannot change it."""
    return PriceSnapshot(
        quantize_money(price),
        quantize_money(regular_price),
        None if loyalty_price is None else quantize_money(loyalty_price),
        None if unit_price is None else quantize_unit_price(unit_price),
        price_basis,
    )


@dataclass
class IngestBatch:
    """Rows one (store, category) ingest will need, read up front rather than per listing."""

    canonicals: CanonicalIndex
    retailer_products: dict[str, RetailerProduct]  # by retailer_sku
    offers: dict[int, Offer]  # by retailer_product_id, this store only
    # The newest history row's `PriceSnapshot`, by retailer_product_id, for this store.
    last_prices: dict[int, PriceSnapshot]


async def load_batch(
    session: AsyncSession,
    retailer: Retailer,
    store: Store,
    category: Category,
    listings: list[ProductListing],
) -> IngestBatch:
    canonicals = list(
        await session.scalars(
            select(CanonicalProduct)
            .where(CanonicalProduct.category == category.key)
            .order_by(CanonicalProduct.id)
        )
    )
    skus = {listing.retailer_sku for listing in listings}
    products: dict[str, RetailerProduct] = {}
    if skus:
        rows = await session.scalars(
            select(RetailerProduct).where(
                RetailerProduct.retailer_id == retailer.id,
                RetailerProduct.retailer_sku.in_(skus),
            )
        )
        products = {product.retailer_sku: product for product in rows}
    product_ids = [product.id for product in products.values()]
    offers: dict[int, Offer] = {}
    last_prices: dict[int, PriceSnapshot] = {}
    if product_ids:
        offer_rows = await session.scalars(
            select(Offer).where(
                Offer.store_id == store.id, Offer.retailer_product_id.in_(product_ids)
            )
        )
        offers = {offer.retailer_product_id: offer for offer in offer_rows}
        last_prices = await _latest_prices(session, store.id, product_ids)
    return IngestBatch(CanonicalIndex(canonicals), products, offers, last_prices)


async def _latest_prices(
    session: AsyncSession, store_id: int, retailer_product_ids: list[int]
) -> dict[int, PriceSnapshot]:
    """The newest price_history row per retailer product at one store, in a single query."""
    newest = (
        func.row_number()
        .over(
            partition_by=PriceHistory.retailer_product_id,
            order_by=(PriceHistory.scraped_at.desc(), PriceHistory.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(
            PriceHistory.retailer_product_id,
            PriceHistory.price,
            PriceHistory.regular_price,
            PriceHistory.loyalty_price,
            PriceHistory.unit_price,
            PriceHistory.price_basis,
            newest,
        )
        .where(
            PriceHistory.store_id == store_id,
            PriceHistory.retailer_product_id.in_(retailer_product_ids),
        )
        .subquery()
    )
    rows = await session.execute(
        select(
            ranked.c.retailer_product_id,
            ranked.c.price,
            ranked.c.regular_price,
            ranked.c.loyalty_price,
            ranked.c.unit_price,
            ranked.c.price_basis,
        ).where(ranked.c.rank == 1)
    )
    return {row[0]: price_snapshot(row[1], row[2], row[3], row[4], row[5]) for row in rows}


# --------------------------------------------------------------------------- one listing


async def ingest_listing(
    session: AsyncSession,
    batch: IngestBatch,
    retailer: Retailer,
    store: Store,
    category: Category,
    listing: ProductListing,
    now: datetime,
    product_hosts: frozenset[str] = frozenset(),
) -> bool:
    """Persist one listing as retailer product + offer. Returns False when skipped."""
    if not title_matches_category(listing.title, category):
        return False
    if listing.store_context and listing.store_context != store.external_id:
        # The retailer answered for a different store than the one it was asked about. Its
        # price belongs to that other store's shelf, and writing it here would attach a real
        # price to the wrong location -- worse than having no price, because it looks right.
        log.warning(
            "store_context_mismatch",
            extra={
                "retailer": retailer.slug,
                "store": store.external_id,
                "answered_for": listing.store_context,
                "sku": listing.retailer_sku,
            },
        )
        return False
    listing = replace(listing, gtin=normalize_gtin(listing.gtin))
    quantity = listing_quantity(listing)
    if quantity is None:
        return False
    size = comparison_quantity(quantity, category.comparison_unit)
    if size is None:
        return False  # e.g. eggs sold by the ounce: not comparable in this category
    per_unit = unit_price(listing.price, quantity, category.comparison_unit)

    retailer_product = batch.retailer_products.get(listing.retailer_sku)
    if retailer_product is None:
        retailer_product = RetailerProduct(
            retailer_id=retailer.id,
            retailer_sku=listing.retailer_sku,
            title=listing.title,
            scrape_source=listing.source,
        )
        session.add(retailer_product)
        batch.retailer_products[listing.retailer_sku] = retailer_product

    if retailer_product.canonical_product_id is not None:
        # An existing retailer SKU mapping is itself a matching signal: keep it stable.
        canonical = batch.canonicals.find(retailer_product.canonical_product_id)
        if canonical is None:
            # The SKU is already mapped into another category, which this batch never loaded.
            canonical = await session.get(CanonicalProduct, retailer_product.canonical_product_id)
            if canonical is None:
                raise LookupError(
                    f"retailer product {retailer_product.retailer_sku} points at missing "
                    f"canonical {retailer_product.canonical_product_id}"
                )
        if canonical.category != category.key:
            # e.g. "Brown Rice Bread" matches rice and bread; the first category keeps it and
            # its unit price must not be overwritten in another category's unit.
            return False
    else:
        canonical, result = await resolve_canonical(
            session, batch, category, listing, quantity, size
        )
        retailer_product.match_status = result.status
        retailer_product.match_confidence = result.confidence
        retailer_product.match_candidates = result.candidates
    retailer_product.canonical_product_id = canonical.id
    retailer_product.title = listing.title
    retailer_product.brand_raw = listing.brand
    # Defence in depth: adapters build URLs through `retailers/urls.py`, and nothing that
    # would not pass those rules for *this* retailer is allowed into the column, whatever
    # path produced the listing. A rejected URL is dropped, never stored and never shown.
    retailer_product.product_url = _persistable_url(listing, retailer.slug, product_hosts)
    retailer_product.image_url = _persistable_image_url(listing, retailer.slug)
    retailer_product.gtin = listing.gtin
    retailer_product.size_text = listing.size_text
    # The published weight span, or all three columns back to NULL: a product that stops
    # being sold by the tray must not keep yesterday's range next to today's fixed size.
    weight_range = listing.weight_range
    retailer_product.min_weight = weight_range.minimum.value if weight_range else None
    retailer_product.max_weight = weight_range.maximum.value if weight_range else None
    retailer_product.weight_unit = weight_range.maximum.unit if weight_range else None
    retailer_product.scrape_source = listing.source
    retailer_product.last_scraped_at = now
    await session.flush()

    offer = batch.offers.get(retailer_product.id)
    # No live offer means this store was not listing the product when the batch was read --
    # either it is new here, or it was delisted and is back. Both are a fresh observation.
    relisted = offer is None
    if offer is None:
        offer = Offer(
            retailer_product_id=retailer_product.id,
            store_id=store.id,
            price=listing.price,
            regular_price=listing.regular_price,
            price_basis=listing.price_basis,
            availability=listing.availability,
            store_context=listing.store_context,
            scrape_source=listing.source,
        )
        session.add(offer)
        batch.offers[retailer_product.id] = offer
    offer.price = listing.price
    offer.regular_price = listing.regular_price
    offer.loyalty_price = listing.loyalty_price
    # The basis travels with the price it qualifies. A bare `2.59` cannot be read back into
    # "per pound" by anything downstream, so it is written here or it is lost.
    offer.price_basis = listing.price_basis
    offer.max_total_price = listing.max_total_price
    offer.currency = listing.currency
    offer.availability = listing.availability
    # Bounded like every other retailer string, because a diagnostic must never be what
    # fails a scrape. The column is wide enough that no wording a registered adapter builds
    # reaches this, so it is a backstop rather than routine truncation.
    offer.stock_status = _bounded(listing.stock_status, STOCK_STATUS_MAX)
    offer.store_context = listing.store_context
    offer.unit_price = per_unit
    offer.unit_price_unit = category.comparison_label
    offer.scrape_source = listing.source
    offer.scraped_at = now
    # No flush here: price history reads the offer's in-memory values, and the batch's
    # closing query autoflushes the pending rows in one round trip.
    record_price_history(session, batch, retailer_product, store, offer, now, relisted=relisted)
    return True


async def resolve_canonical(
    session: AsyncSession,
    batch: IngestBatch,
    category: Category,
    listing: ProductListing,
    quantity: Quantity,
    size: Decimal,
) -> tuple[CanonicalProduct, MatchResult]:
    count = int(quantity.value) if quantity.unit == "count" else None
    candidate = ProductFeatures(
        category=category.key,
        title=listing.title,
        brand=listing.brand,
        comparison_quantity=size,
        count=count,
        gtin=listing.gtin,
        sold_by=listing.sold_by,
    )
    result = match_product(candidate, batch.canonicals.features)
    if result.canonical_id is not None:
        canonical = batch.canonicals.get(result.canonical_id)
        if listing.gtin and not canonical.gtin:
            canonical.gtin = listing.gtin
            batch.canonicals.refresh(canonical)
    else:
        canonical = CanonicalProduct(
            category=category.key,
            brand=normalize_brand(listing.brand),
            normalized_name=normalize_title(listing.title, listing.brand),
            quantity=quantity.value,
            quantity_unit=quantity.unit,
            count=count,
            gtin=listing.gtin,
            comparison_unit=category.comparison_unit,
            comparison_quantity=size,
            attributes={
                "organic": "organic" in listing.title.lower(),
                "sold_by": listing.sold_by,
                **extract_attributes(listing.title),
            },
        )
        session.add(canonical)
        await session.flush()
        batch.canonicals.add(canonical)
    return canonical, result


async def expire_stale_offers(
    session: AsyncSession, store: Store, category: Category, now: datetime
) -> int:
    """Delete this store's offers in the category that the current scrape did not confirm.

    "Current offers" means exactly what the latest successful search returned; delisted
    products must not keep competing. Price history is kept.
    """
    stale = list(
        await session.scalars(
            select(Offer)
            .join(Offer.retailer_product)
            .join(RetailerProduct.canonical_product)
            .where(
                Offer.store_id == store.id,
                CanonicalProduct.category == category.key,
                Offer.scraped_at < now,
            )
        )
    )
    for offer in stale:
        await session.delete(offer)
    if stale:
        await session.flush()
    return len(stale)


def record_price_history(
    session: AsyncSession,
    batch: IngestBatch,
    retailer_product: RetailerProduct,
    store: Store,
    offer: Offer,
    now: datetime,
    *,
    relisted: bool = False,
) -> None:
    """Append a history row only when the price actually changed (or none exists).

    This is the whole deduplication policy, and it is the reason a refresh every five
    minutes does not produce 288 identical points a day. What it deliberately does *not* do
    is record "we looked again and it was the same": a series is a list of changes, each
    price holding until the next row, and `offers.scraped_at` already says when the current
    one was last confirmed. That also settles the cached-scrape case for free -- a reused
    capture carries the prices it was captured with, so it matches and writes nothing.

    The comparison covers everything that decides what is plotted. `unit_price` and
    `price_basis` are in it because a pack that shrinks from 16 oz to 12 oz at the same
    $3.99, or a retailer that switches from a package total to a rate per pound, has changed
    its price in the only sense a comparison shopper cares about -- and on the old
    three-column test neither recorded anything at all.

    `relisted` forces a row for a product that had no live offer when the batch was read.
    History outlives the offer it described, so a product delisted at $3.99 in March and
    listed again at $3.99 in June otherwise matches the March row and records nothing -- and
    the chart then draws one unbroken line across three months it was not sold at all. The
    price is the same; that it is on sale again is the new fact.

    Pure bookkeeping over rows the batch already loaded, so it needs no query of its own.
    """
    snapshot = price_snapshot(
        offer.price,
        offer.regular_price,
        offer.loyalty_price,
        offer.unit_price,
        offer.price_basis,
    )
    if not relisted and batch.last_prices.get(retailer_product.id) == snapshot:
        return
    session.add(
        PriceHistory(
            retailer_product_id=retailer_product.id,
            store_id=store.id,
            # The snapshot's values, not the offer's: what was compared has to be what is
            # stored, or the next scrape reads back a number it never compared against.
            price=snapshot.price,
            regular_price=snapshot.regular_price,
            loyalty_price=snapshot.loyalty_price,
            unit_price=snapshot.unit_price,
            price_basis=offer.price_basis,
            unit_price_unit=offer.unit_price_unit,
            scrape_source=offer.scrape_source,
            scraped_at=now,
        )
    )
    batch.last_prices[retailer_product.id] = snapshot
