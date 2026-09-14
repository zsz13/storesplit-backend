"""Price history: what is recorded, what is deliberately not, and what the API returns.

Two halves. The first drives the real scrape path over fake adapters and asserts on the rows
it writes -- that is where deduplication lives and where a cached or repeated scrape has to
produce nothing. The second builds series directly and asserts on the endpoint's shape,
because the interesting cases (a price that last moved before the window, a delisted offer,
a series with one point) are about *time*, and a test that had to wait for them would not be
one.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.db.models import CanonicalProduct, Offer, PriceHistory, RetailerProduct
from app.normalize.categories import CATEGORIES
from app.normalize.units import Quantity, QuantityRange
from app.retailers.base import ProductListing
from app.services.price_history import MAX_POINTS_PER_SERIES, product_price_history
from app.services.scraper import scrape_retailer
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import STORE_A, STORE_B, FakeAdapter, listing
from tests.test_scrape_and_api import count_statements


def eggs(sku: str, store: str, price: str, **kwargs) -> ProductListing:
    return listing(sku, "Large Grade A Eggs, 12 CT", store, price, brand="Farm Co", **kwargs)


def chicken(sku: str, store: str, price: str) -> ProductListing:
    """A variable-weight tray: the retailer quotes a rate per pound, not a package total."""
    return listing(
        sku,
        "Boneless Skinless Chicken Breast Value Pack",
        store,
        price,
        price_basis="lb",
        weight_range=QuantityRange(Quantity(Decimal("2.5"), "lb"), Quantity(Decimal("5.25"), "lb")),
        max_total_price="12.95",
    )


async def run(db: AsyncSession, adapter: FakeAdapter, category: str = "eggs") -> None:
    await scrape_retailer(db, adapter, "94105", [CATEGORIES[category]], 2)
    await db.commit()


def catalog(category: str, listings_by_store: dict[str, list]) -> dict:
    return {category: listings_by_store}


# --------------------------------------------------------------- what a scrape records


async def test_a_fixed_price_product_records_one_observation(db: AsyncSession) -> None:
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    await run(db, adapter)
    row = await db.scalar(select(PriceHistory))
    assert row is not None
    assert (row.price, row.price_basis) == (Decimal("4.99"), "package")
    assert row.unit_price_unit == "egg"  # eggs are compared per egg, and the row says so


async def test_a_variable_weight_product_records_its_rate_and_its_basis(db: AsyncSession) -> None:
    """The regression the offers table already had: `6.99` is a rate, and a history row that
    does not say so is indistinguishable from a $6.99 package."""
    adapter = FakeAdapter(
        "alpha", [STORE_A], catalog("chicken breast", {"A1": [chicken("a-chx", "A1", "6.99")]})
    )
    await run(db, adapter, "chicken_breast")
    row = await db.scalar(select(PriceHistory))
    assert row is not None
    assert (row.price, row.price_basis) == (Decimal("6.99"), "lb")
    # A per-pound rate buys one pound, so the comparison price is the rate itself.
    assert (row.unit_price, row.unit_price_unit) == (Decimal("6.9900"), "lb")


async def test_an_unchanged_price_across_repeated_scrapes_records_nothing_new(
    db: AsyncSession,
) -> None:
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    for _ in range(4):
        await run(db, adapter)
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1


async def test_a_real_price_change_records_a_second_observation(db: AsyncSession) -> None:
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    await run(db, adapter)
    adapter._catalog = catalog("eggs", {"A1": [eggs("a", "A1", "5.49")]})
    await run(db, adapter)
    prices = list(await db.scalars(select(PriceHistory.price).order_by(PriceHistory.id)))
    assert prices == [Decimal("4.99"), Decimal("5.49")]


async def test_a_resized_pack_at_the_same_price_is_a_price_change(db: AsyncSession) -> None:
    """$3.99 for 16 oz and $3.99 for 12 oz are not the same price. The old three-column
    change test compared only the amounts, so shrinkflation recorded nothing at all."""
    adapter = FakeAdapter(
        "alpha",
        [STORE_A],
        catalog(
            "chicken breast",
            {"A1": [listing("a", "Chicken Breast", "A1", "3.99", size_text="16 oz")]},
        ),
    )
    await run(db, adapter, "chicken_breast")
    adapter._catalog = catalog(
        "chicken breast",
        {"A1": [listing("a", "Chicken Breast", "A1", "3.99", size_text="12 oz")]},
    )
    await run(db, adapter, "chicken_breast")
    rows = list(await db.scalars(select(PriceHistory).order_by(PriceHistory.id)))
    assert [r.price for r in rows] == [Decimal("3.99"), Decimal("3.99")]
    assert rows[0].unit_price != rows[1].unit_price


async def test_a_price_the_retailer_publishes_to_three_decimals_still_deduplicates(
    db: AsyncSession,
) -> None:
    """The round trip has to close. `price` is `Numeric(10, 2)`, so `4.999` is read back as
    `5.00`; comparing the stored value against an unrounded in-memory one never matched, and
    a byte-identical payload wrote a row on every scrape -- 288 a day at the five-minute
    refresh cadence, for a price that never moved. Kroger and Whole Foods both build prices
    straight from their payloads without rounding, so this is a live path.
    """
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.999")]}))
    for _ in range(3):
        await run(db, adapter)
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1
    row = await db.scalar(select(PriceHistory))
    assert row is not None and row.price == Decimal("5.00")


async def test_a_sale_ending_is_a_price_change_even_when_the_shelf_price_holds(
    db: AsyncSession,
) -> None:
    """`regular_price` is in the change test, and this is the only thing that proves it: a
    promotion ending moves the list price while what you hand over stays the same."""
    adapter = FakeAdapter(
        "alpha",
        [STORE_A],
        catalog("eggs", {"A1": [eggs("a", "A1", "4.99", regular_price="4.99")]}),
    )
    await run(db, adapter)
    adapter._catalog = catalog("eggs", {"A1": [eggs("a", "A1", "4.99", regular_price="6.49")]})
    await run(db, adapter)
    rows = list(await db.scalars(select(PriceHistory).order_by(PriceHistory.id)))
    assert [r.regular_price for r in rows] == [Decimal("4.99"), Decimal("6.49")]
    assert [r.price for r in rows] == [Decimal("4.99"), Decimal("4.99")]


async def test_a_loyalty_price_appearing_is_a_price_change(db: AsyncSession) -> None:
    """The card price is the third column of the change test, and a shopper with the card
    pays it. It appearing or vanishing is a change nothing else records."""
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    await run(db, adapter)
    adapter._catalog = catalog("eggs", {"A1": [eggs("a", "A1", "4.99", loyalty_price="3.99")]})
    await run(db, adapter)
    rows = list(await db.scalars(select(PriceHistory).order_by(PriceHistory.id)))
    assert [r.loyalty_price for r in rows] == [None, Decimal("3.99")]


async def test_a_product_listed_again_records_a_new_observation_at_the_same_price(
    db: AsyncSession,
) -> None:
    """History outlives the offer it described, so a product delisted at $4.99 and listed
    again at $4.99 matched the old row and recorded nothing -- and the chart then drew one
    unbroken line across the stretch it was not sold at all. The price is the same; that it
    is on sale again is the new fact."""
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    await run(db, adapter)
    adapter._catalog = catalog("eggs", {"A1": []})
    await run(db, adapter)  # delisted: the offer goes, the history stays
    adapter._catalog = catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]})
    await run(db, adapter)  # listed again at the same price
    prices = list(await db.scalars(select(PriceHistory.price).order_by(PriceHistory.id)))
    assert prices == [Decimal("4.99"), Decimal("4.99")]


async def test_a_cached_scrape_does_not_create_a_fake_observation(db: AsyncSession) -> None:
    """A reused capture replays the prices it was captured with. Deduplication is what makes
    that a no-op, so a run that collected nothing new writes nothing new -- even though it
    does rewrite `offers.scraped_at`, which is the column that legitimately means "confirmed
    again just now"."""
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    await run(db, adapter)
    first = await db.scalar(select(Offer.scraped_at))

    # The same payload again, as a reused capture would supply it.
    await run(db, adapter)
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1
    again = await db.scalar(select(Offer.scraped_at))
    assert first is not None and again is not None and again >= first


async def test_the_same_sku_at_two_stores_is_two_series(db: AsyncSession) -> None:
    """One retailer, one SKU, two branches, two prices. Merging them would draw a line
    through a price neither shop charged."""
    adapter = FakeAdapter(
        "alpha",
        [STORE_A, STORE_B],
        catalog(
            "eggs", {"A1": [eggs("shared", "A1", "4.99")], "B1": [eggs("shared", "B1", "5.99")]}
        ),
    )
    await run(db, adapter)
    rows = list(await db.scalars(select(PriceHistory).order_by(PriceHistory.id)))
    assert len({r.store_id for r in rows}) == 2
    assert {r.price for r in rows} == {Decimal("4.99"), Decimal("5.99")}
    assert len({r.retailer_product_id for r in rows}) == 1  # one SKU, two shops


async def test_history_survives_the_offer_it_described(db: AsyncSession) -> None:
    """A delisted product keeps its history; only the offer goes."""
    adapter = FakeAdapter("alpha", [STORE_A], catalog("eggs", {"A1": [eggs("a", "A1", "4.99")]}))
    await run(db, adapter)
    adapter._catalog = catalog("eggs", {"A1": []})
    await run(db, adapter)
    assert await db.scalar(select(func.count()).select_from(Offer)) == 0
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1


# ------------------------------------------------------------------- the API's shape


@pytest.fixture
async def series(db: AsyncSession):
    """One canonical product, two stores of one retailer, history placed in time by hand."""
    adapter = FakeAdapter(
        "alpha",
        [STORE_A, STORE_B],
        catalog(
            "eggs", {"A1": [eggs("shared", "A1", "4.99")], "B1": [eggs("shared", "B1", "5.99")]}
        ),
    )
    await run(db, adapter)
    product = await db.scalar(select(CanonicalProduct))
    assert product is not None
    return product


def history_row(rp_id: int, store_id: int, price: str, at: datetime, unit: str) -> PriceHistory:
    return PriceHistory(
        retailer_product_id=rp_id,
        store_id=store_id,
        price=Decimal(price),
        regular_price=Decimal(price),
        unit_price=Decimal(unit),
        price_basis="package",
        unit_price_unit="egg",
        scrape_source="fake:test",
        scraped_at=at,
    )


async def test_the_endpoint_separates_stores_and_labels_them(
    client: AsyncClient, db: AsyncSession, series
) -> None:
    body = (await client.get(f"/products/{series.id}/price-history")).json()
    assert body["comparison_unit"] == "egg"
    assert len(body["series"]) == 2
    assert {s["store_name"] for s in body["series"]} == {
        "Alpha Market Downtown",
        "Beta Foods SoMa",
    }
    assert {s["retailer_name"] for s in body["series"]} == {"Alpha"}
    assert {s["retailer_sku"] for s in body["series"]} == {"shared"}
    # Cheapest current price first, so the legend leads with the series a shopper wants.
    assert [s["current"]["price"] for s in body["series"]] == ["4.99", "5.99"]
    for one in body["series"]:
        assert one["current"]["price_basis"] == "package"
        assert one["current"]["unit_price_unit"] == "egg"


async def test_an_empty_history_is_an_empty_answer_not_an_error(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A product nobody has ever scraped twice still has a product page."""
    product = CanonicalProduct(
        category="eggs",
        normalized_name="ghost eggs",
        quantity=Decimal(12),
        quantity_unit="count",
        comparison_unit="count",
        comparison_quantity=Decimal(12),
    )
    db.add(product)
    await db.commit()
    body = (await client.get(f"/products/{product.id}/price-history")).json()
    assert body["series"] == [] and body["comparison_unit"] == "egg"


async def test_a_missing_product_is_a_404(client: AsyncClient) -> None:
    assert (await client.get("/products/999999/price-history")).status_code == 404


async def test_one_observation_is_returned_as_one_point(
    client: AsyncClient, db: AsyncSession, series
) -> None:
    """A single scrape gives a series exactly one history row and one current price. The
    client is what decides to say "not enough history yet"; the API must not pad it."""
    body = (await client.get(f"/products/{series.id}/price-history")).json()
    first = body["series"][0]
    assert len(first["points"]) == 1 and first["current"] is not None
    assert first["points"][0]["before_window"] is False


async def test_a_price_that_last_moved_before_the_window_still_starts_the_line(
    client: AsyncClient, db: AsyncSession, series
) -> None:
    """The defect a plain window query has: 45 days of a steady price renders as no history.
    The newest observation *before* the range comes back, flagged."""
    offer = await db.scalar(select(Offer).order_by(Offer.id))
    assert offer is not None
    await db.execute(PriceHistory.__table__.delete().where(PriceHistory.store_id == offer.store_id))
    db.add(
        history_row(
            offer.retailer_product_id,
            offer.store_id,
            "4.99",
            datetime.now(UTC) - timedelta(days=45),
            "0.4158",
        )
    )
    await db.commit()

    body = (await client.get(f"/products/{series.id}/price-history?days=30")).json()
    line = next(s for s in body["series"] if s["store_id"] == offer.store_id)
    assert [p["before_window"] for p in line["points"]] == [True]
    assert line["points"][0]["price"] == "4.99"
    assert line["current"] is not None  # and the right edge is the live offer


async def test_points_are_oldest_first_and_the_window_excludes_what_it_should(
    client: AsyncClient, db: AsyncSession, series
) -> None:
    offer = await db.scalar(select(Offer).order_by(Offer.id))
    assert offer is not None
    await db.execute(PriceHistory.__table__.delete().where(PriceHistory.store_id == offer.store_id))
    now = datetime.now(UTC)
    for days_ago, price in ((40, "3.99"), (20, "4.49"), (5, "4.99")):
        db.add(
            history_row(
                offer.retailer_product_id,
                offer.store_id,
                price,
                now - timedelta(days=days_ago),
                "0.4158",
            )
        )
    await db.commit()

    body = (await client.get(f"/products/{series.id}/price-history?days=30")).json()
    line = next(s for s in body["series"] if s["store_id"] == offer.store_id)
    assert [p["price"] for p in line["points"]] == ["3.99", "4.49", "4.99"]
    # The 40-day-old row is outside the window and is present only as the starting value.
    assert [p["before_window"] for p in line["points"]] == [True, False, False]

    wide = (await client.get(f"/products/{series.id}/price-history?days=90")).json()
    wide_line = next(s for s in wide["series"] if s["store_id"] == offer.store_id)
    assert [p["before_window"] for p in wide_line["points"]] == [False, False, False]


async def test_a_delisted_series_has_history_but_no_current_price(
    client: AsyncClient, db: AsyncSession, series
) -> None:
    """Its last known price must not be drawn forward to today."""
    offer = await db.scalar(select(Offer).order_by(Offer.id))
    assert offer is not None
    store_id = offer.store_id
    await db.delete(offer)
    await db.commit()

    body = (await client.get(f"/products/{series.id}/price-history")).json()
    line = next(s for s in body["series"] if s["store_id"] == store_id)
    assert line["current"] is None and len(line["points"]) == 1
    assert line["store_name"] and line["retailer_name"]  # still labelled, from its history


async def test_a_series_longer_than_the_cap_is_reported_as_truncated(
    client: AsyncClient, db: AsyncSession, series
) -> None:
    offer = await db.scalar(select(Offer).order_by(Offer.id))
    assert offer is not None
    now = datetime.now(UTC)
    for index in range(MAX_POINTS_PER_SERIES + 5):
        db.add(
            history_row(
                offer.retailer_product_id,
                offer.store_id,
                "4.99",
                now - timedelta(minutes=index + 1),
                "0.4158",
            )
        )
    await db.commit()

    body = (await client.get(f"/products/{series.id}/price-history")).json()
    line = next(s for s in body["series"] if s["store_id"] == offer.store_id)
    assert line["truncated"] is True
    assert len(line["points"]) == MAX_POINTS_PER_SERIES
    # A truncated series already starts mid-history; an anchor would date it wrongly.
    assert not any(p["before_window"] for p in line["points"])


async def test_the_query_count_does_not_grow_with_the_number_of_series(
    db: AsyncSession, engine, series
) -> None:
    """The N+1 this endpoint must not have: a store is not a query."""
    with count_statements(engine) as statements:
        await product_price_history(db, series.id)
    two_stores = sum(1 for s in statements if s.upper().startswith("SELECT"))

    offer = await db.scalar(select(Offer).order_by(Offer.id))
    assert offer is not None
    now = datetime.now(UTC)
    # Six more series, on stores that already exist, so only the fan-out changes.
    rp_ids = list(await db.scalars(select(RetailerProduct.id)))
    for index in range(6):
        db.add(
            history_row(
                rp_ids[index % len(rp_ids)],
                offer.store_id,
                f"{4 + index}.99",
                now - timedelta(hours=index + 1),
                "0.4158",
            )
        )
    await db.commit()

    with count_statements(engine) as statements:
        await product_price_history(db, series.id)
    assert sum(1 for s in statements if s.upper().startswith("SELECT")) == two_stores


async def test_the_range_is_bounded(client: AsyncClient, series) -> None:
    assert (await client.get(f"/products/{series.id}/price-history?days=0")).status_code == 422
    assert (await client.get(f"/products/{series.id}/price-history?days=99999")).status_code == 422
    assert (await client.get(f"/products/{series.id}/price-history?days=3650")).status_code == 200
