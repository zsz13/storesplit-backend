"""What one product has cost over time, per retailer, per store, per SKU.

The unit of this feature is the **series**: one (retailer product, store) pair, which is one
physical shop selling one retailer's own SKU. Nothing merges two of those. A canonical
product is a comparison across retailers, not a thing with a price -- averaging Safeway's
eggs and Trader Joe's into a single line would draw a price nobody was ever charged, and
would move whenever a scrape happened to reach one store and not the other.

Three things decide what the shape of the response has to be.

*A series is a list of changes.* `record_price_history` writes a row only when a scrape sees
a price differ from the newest row for that pair, so a price holds from its own row until the
next one. A window query alone is therefore not enough: a product whose price last moved
forty days ago has no row inside a thirty-day window and would render as "no history" despite
being observed every hour since. So the newest row *before* the window comes back too,
flagged, purely to give the line its starting value.

*The present is not in the table.* The right edge of the line is the live offer, which is the
only row that knows the price is still being charged and when it was last confirmed. A series
whose offer has been expired ends at its last observation and is not drawn forward -- a
delisted product must not appear to still cost something today.

*Two prices, and they are not interchangeable.* `unit_price` is the normalized comparison
price (per lb, per egg, per gal) and is what the chart plots, because it is the only figure
comparable across pack sizes and across a per-pound rate. `price` is what the retailer
quotes, and `price_basis` says what it buys. Both travel with every point; the second is
context, never the axis.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    CanonicalProduct,
    Offer,
    PriceHistory,
    Retailer,
    RetailerProduct,
    Store,
)
from app.normalize.availability import (
    AVAILABILITY_RANK,
    UNKNOWN,
    as_availability,
    availability_rank,
)
from app.normalize.categories import CATEGORIES
from app.normalize.pricing import normalize_basis
from app.retailers import stock_reporting
from app.schemas import (
    PriceHistoryResponse,
    PriceObservationOut,
    PriceSeriesOut,
)

DEFAULT_RANGE_DAYS = 30
# Ten years. The ceiling exists so a caller cannot ask for an unbounded scan, not because
# any range below it is expected to hold data; the frontend's "All" asks for this.
MAX_RANGE_DAYS = 3650
# A bound on one series' response, not a product decision: with change-only deduplication a
# grocery price produces a handful of rows a month, and a series that reaches this many has
# a flapping adapter behind it rather than a shopper's question.
MAX_POINTS_PER_SERIES = 500
# A bound on how many stores one answer describes. Twenty-four is far above what a real
# product reaches -- ten registered retailers at the configured two stores a ZIP -- and is
# here so that a product collected across dozens of ZIPs cannot turn one request into an
# arbitrarily large response.
MAX_SERIES = 24


@dataclass(frozen=True)
class _SeriesKey:
    retailer_product_id: int
    store_id: int


def _base(product_id: int) -> Select:
    """Every history row for one canonical product, labelled with the series it belongs to.

    The labels are joined in rather than looked up per series afterwards: a store whose offer
    has since been expired still has history, so the names cannot come from the live offers,
    and one join is one query where a lookup per pair is N+1.
    """
    return (
        select(
            PriceHistory.retailer_product_id,
            PriceHistory.store_id,
            PriceHistory.price,
            PriceHistory.regular_price,
            PriceHistory.loyalty_price,
            PriceHistory.unit_price,
            PriceHistory.price_basis,
            PriceHistory.unit_price_unit,
            PriceHistory.scraped_at,
            RetailerProduct.retailer_sku,
            Retailer.slug.label("retailer_slug"),
            Retailer.name.label("retailer_name"),
            Store.name.label("store_name"),
            Store.city.label("store_city"),
        )
        .join(RetailerProduct, RetailerProduct.id == PriceHistory.retailer_product_id)
        .join(Retailer, Retailer.id == RetailerProduct.retailer_id)
        .join(Store, Store.id == PriceHistory.store_id)
        .where(RetailerProduct.canonical_product_id == product_id)
    )


def _newest_first() -> Any:
    return func.row_number().over(
        partition_by=(PriceHistory.retailer_product_id, PriceHistory.store_id),
        order_by=(PriceHistory.scraped_at.desc(), PriceHistory.id.desc()),
    )


@dataclass(frozen=True)
class _SeriesLabels:
    """Who and where a series is. Read off a history row where there is one, and off the
    live offer for a series whose first observation is still to be written."""

    retailer_slug: str
    retailer_name: str
    store_name: str
    store_city: str | None
    retailer_sku: str


def _labels_from_row(row: Any) -> _SeriesLabels:
    return _SeriesLabels(
        row.retailer_slug, row.retailer_name, row.store_name, row.store_city, row.retailer_sku
    )


def _labels_from_offer(offer: Offer) -> _SeriesLabels:
    store = offer.store
    return _SeriesLabels(
        store.retailer.slug,
        store.retailer.name,
        store.name,
        store.city,
        offer.retailer_product.retailer_sku,
    )


def _observation(row: Any, *, before_window: bool = False) -> PriceObservationOut:
    """One reading, from a history row or from the live offer -- an `Offer` carries exactly
    the same seven fields, which is why the right-hand end of a line needs no builder of its
    own. `None` in, `None` out: a series whose offer has been expired has no current price,
    and drawing its last known one forward to today would state a price that is not on sale.
    """
    return PriceObservationOut(
        scraped_at=row.scraped_at,
        price=row.price,
        regular_price=row.regular_price,
        loyalty_price=row.loyalty_price,
        unit_price=row.unit_price,
        price_basis=normalize_basis(row.price_basis) if row.price_basis else None,
        unit_price_unit=row.unit_price_unit,
        before_window=before_window,
    )


async def product_price_history(
    db: AsyncSession,
    product_id: int,
    days: int = DEFAULT_RANGE_DAYS,
    now: datetime | None = None,
) -> PriceHistoryResponse | None:
    """One product's price history, by retailer and store. `None` if no such product.

    Four queries whatever the number of series: the product, the window, the anchors, and
    the live offers. Nothing here loops over a query.
    """
    product = await db.get(CanonicalProduct, product_id)
    if product is None:
        return None
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)

    windowed = (
        _base(product_id)
        .add_columns(_newest_first().label("rank"))
        .where(PriceHistory.scraped_at >= since)
        .subquery()
    )
    # One over the cap, so a truncated series can be reported as truncated rather than
    # silently presented as the whole of what was collected.
    in_window = list(
        await db.execute(
            select(windowed)
            .where(windowed.c.rank <= MAX_POINTS_PER_SERIES + 1)
            # `_assemble` slices and reverses these, so the order is load-bearing. A
            # subquery does not inherit its window's ordering and SQL promises nothing
            # about row order without an ORDER BY -- today's plans happen to comply.
            .order_by(windowed.c.retailer_product_id, windowed.c.store_id, windowed.c.rank)
        )
    )

    anchored = (
        _base(product_id)
        .add_columns(_newest_first().label("rank"))
        .where(PriceHistory.scraped_at < since)
        .subquery()
    )
    anchors = list(await db.execute(select(anchored).where(anchored.c.rank == 1)))

    offers = list(
        await db.scalars(
            select(Offer)
            .join(Offer.retailer_product)
            .where(RetailerProduct.canonical_product_id == product_id)
            .options(
                selectinload(Offer.store).selectinload(Store.retailer),
                selectinload(Offer.retailer_product),
            )
        )
    )

    series = _assemble(in_window, anchors, offers)
    category = CATEGORIES.get(product.category)
    return PriceHistoryResponse(
        product_id=product.id,
        product_name=product.normalized_name,
        category=product.category,
        # What the y axis is titled with, and the fallback label for a history row written
        # before `unit_price_unit` existed and whose offer has since been expired.
        comparison_unit=category.comparison_label if category else None,
        currency=offers[0].currency if offers else "USD",
        days=days,
        since=since,
        now=now,
        series=series,
    )


def _assemble(
    in_window: list[Any], anchors: list[Any], offers: list[Offer]
) -> list[PriceSeriesOut]:
    """Fold rows and live offers into one series per (retailer product, store).

    A series can come from either side: history with no live offer is a delisted product,
    and a live offer with no history is a product whose first observation is its current
    one. Both are real and both are rendered; only a pair that is in neither is dropped.
    """
    key = _SeriesKey
    points: dict[_SeriesKey, list[Any]] = {}
    for row in in_window:
        points.setdefault(key(row.retailer_product_id, row.store_id), []).append(row)
    anchor_by_key = {key(r.retailer_product_id, r.store_id): r for r in anchors}
    offer_by_key = {key(o.retailer_product_id, o.store_id): o for o in offers}

    labels: dict[_SeriesKey, _SeriesLabels] = {}
    for row in [*in_window, *anchors]:
        labels.setdefault(key(row.retailer_product_id, row.store_id), _labels_from_row(row))
    for pair, offer in offer_by_key.items():
        labels.setdefault(pair, _labels_from_offer(offer))

    out: list[PriceSeriesOut] = []
    for pair, label in labels.items():
        rows = points.get(pair, [])
        truncated = len(rows) > MAX_POINTS_PER_SERIES
        # The query returned newest first, so the cap keeps the *recent* end; a chart reads
        # left to right, so the kept rows are then reversed.
        observations = [_observation(row) for row in reversed(rows[:MAX_POINTS_PER_SERIES])]
        # An anchor only means "the value the line starts at". A truncated series already
        # begins mid-history, so carrying one in would date the line wrongly.
        anchor = anchor_by_key.get(pair)
        if anchor is not None and not truncated:
            observations.insert(0, _observation(anchor, before_window=True))
        out.append(
            PriceSeriesOut(
                retailer_product_id=pair.retailer_product_id,
                store_id=pair.store_id,
                retailer_slug=label.retailer_slug,
                retailer_name=label.retailer_name,
                store_name=label.store_name,
                store_city=label.store_city,
                retailer_sku=label.retailer_sku,
                current=_observation(offer) if (offer := offer_by_key.get(pair)) else None,
                availability=as_availability(offer.availability) if offer else None,
                stock_reporting=stock_reporting(label.retailer_slug),
                points=observations,
                truncated=truncated,
            )
        )
    # Buyable first, then cheapest -- the same order `services/search.py` ranks offers in,
    # and for the same reason: the leading series is the one a client puts in its headline
    # slot, and an offer nobody has confirmed must not be presented as today's answer. A
    # series with no live offer has no current price to rank by and sorts last.
    out.sort(key=_rank)
    # A product carried in enough ZIPs accumulates stores without limit, and every one of
    # them is a series. The cap is on the answer, not on the collection: the dearest and the
    # delisted are the ones dropped, and the response says how many.
    return out[:MAX_SERIES]


def _rank(series: PriceSeriesOut) -> tuple[int, int, Decimal]:
    current = series.current
    if current is None or current.unit_price is None:
        return (len(AVAILABILITY_RANK), 1, Decimal(0))
    return (availability_rank(series.availability or UNKNOWN), 0, current.unit_price)
