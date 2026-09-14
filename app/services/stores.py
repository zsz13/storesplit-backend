"""Store lookup shared by search and basket."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Store
from app.normalize.hours import HoursState, hours_today, store_hours_from_json, utc_now
from app.retailers import product_hosts, stock_reporting
from app.retailers.zipmatch import (
    default_radius_miles,
    rank_within_radius,
    store_point,
    zip_centroid,
)
from app.schemas import HoursTodayOut, StoreOut
from app.services.maps import maps_url

log = logging.getLogger("storesplit.services.stores")


def zip_distance(zip_code: str | None, target: str) -> int:
    """Numeric distance between two 5-digit US ZIPs; a large constant for anything else."""
    if zip_code and zip_code.isdigit() and target.isdigit():
        return abs(int(zip_code) - int(target))
    return 10**6


async def stores_near(db: AsyncSession, zip_code: str) -> list[Store]:
    """The stores a ZIP code means, nearest first.

    One rule, the same one the scrape ranked the retailer's own directory with: great-circle
    distance from the ZIP's Census centroid to the store's point, inside
    `SEARCH_STORE_RADIUS_MILES`, capped per retailer at `SCRAPE_STORES_PER_RETAILER`.

    The cap is what makes the chosen ZIP the source of truth rather than a filter. Whole
    Foods Ocean and Stonestown were resolved and scraped only for 94014; under the ZIP-prefix
    rule this replaced they also answered 94105 searches, because 94112, 94132 and 94105 all
    start `941`. Distance alone does not fix that -- Ocean is well inside 30 miles of
    downtown -- but 94105's own two nearest Whole Foods are SoMa and Trinity, and that is
    what a shopper who typed 94105 asked for.

    On this path `served_zip_codes` deliberately does not readmit a store: a scrape having
    once discovered one says nothing about whether this ZIP's ranking would choose it. It is
    still consulted by the prefix fallback below, where there is no distance to rank by and a
    scrape having reached this exact ZIP is the best evidence left.

    Store counts are small, so ranking happens in Python and stays portable across SQLite and
    PostgreSQL.
    """
    settings = get_settings()
    zip5 = zip_code.strip()[:5]
    rows = list(await db.scalars(select(Store).options(selectinload(Store.retailer))))
    if zip_centroid(zip5) is None:
        # No centroid to measure from. The prefix heuristic is all that is left, and saying
        # so in the log is the difference between a known limitation and a silent default.
        log.info("stores_near_prefix_fallback", extra={"zip_code": zip5})
        return _cap_per_retailer(_by_zip_prefix(rows, zip5), settings.scrape_stores_per_retailer)
    ranked = rank_within_radius(
        rows,
        zip5,
        point=lambda store: store_point(store.latitude, store.longitude, store.zip_code),
        max_miles=default_radius_miles(),
        tiebreak=lambda store: store.id,
    )
    return _cap_per_retailer([store for _, store in ranked], settings.scrape_stores_per_retailer)


def _cap_per_retailer(stores: list[Store], limit: int) -> list[Store]:
    """The first `limit` stores of each retailer, keeping the order they arrived in."""
    taken: dict[int, int] = {}
    kept: list[Store] = []
    for store in stores:
        seen = taken.get(store.retailer_id, 0)
        if seen >= limit:
            continue
        taken[store.retailer_id] = seen + 1
        kept.append(store)
    return kept


def _by_zip_prefix(rows: list[Store], zip5: str) -> list[Store]:
    matches = [
        s
        for s in rows
        if (s.zip_code and s.zip_code[:3] == zip5[:3]) or zip5 in (s.served_zip_codes or [])
    ]
    matches.sort(key=lambda s: (s.zip_code != zip5, zip_distance(s.zip_code, zip5), s.id))
    return matches


def store_hours_state(store: Store, now: datetime | None = None) -> HoursState:
    """Whether this store is open, shut or unaccounted for, in its **own** timezone.

    The server's clock is not the shopper's and neither is the store's: a California shop
    closing at 22:00 closes at 06:00 UTC, so the decision belongs to `hours_today`, which
    reads the schedule in the zone the retailer stated for that store, and nowhere else.
    """
    return hours_today(store_hours_from_json(store.hours, store.timezone), now or utc_now()).state


def open_now_stores(stores: list[Store], now: datetime | None = None) -> list[Store]:
    """The stores an "Open now" search may answer from: everything not known to be shut.

    **Only a confirmed closure removes a store.** A store whose retailer publishes no hours
    is not evidence that it is shut, and dropping it would quietly delete whole retailers
    from the comparison -- every Raley's and every Kroger -- on the strength of a fact nobody
    has. It stays, and the client marks it and ranks it below the stores that really are
    open, so nothing unknown is ever *presented* as open either.

    Ordering puts the confirmed-open first and the unaccounted-for after them, preserving
    the distance order this was handed inside each group. It is a stable partition, not a
    sort: `stores_near` ranked these by distance and that ranking still means something
    within each group.
    """
    moment = now or utc_now()
    states = {store.id: store_hours_state(store, moment) for store in stores}
    return [s for s in stores if states[s.id] == "open"] + [
        s for s in stores if states[s.id] == "unknown"
    ]


def store_out(store: Store, *, now: datetime | None = None) -> StoreOut:
    today = hours_today(store_hours_from_json(store.hours, store.timezone), now or utc_now())
    return StoreOut(
        id=store.id,
        retailer_slug=store.retailer.slug,
        retailer_name=store.retailer.name,
        retailer_host=next(iter(sorted(product_hosts(store.retailer.slug))), None),
        stock_reporting=stock_reporting(store.retailer.slug),
        name=store.name,
        address_line1=store.address_line1,
        city=store.city,
        state=store.state,
        zip_code=store.zip_code,
        latitude=store.latitude,
        longitude=store.longitude,
        timezone=store.timezone,
        maps_url=maps_url(store),
        hours_today=HoursTodayOut(
            state=today.state,
            opens_at=today.opens_at,
            closes_at=today.closes_at,
            opens_day=today.opens_day,
            closed_all_day=today.closed_all_day,
            next_open_at=today.next_open_at,
        ),
    )
