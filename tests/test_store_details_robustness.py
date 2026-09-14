"""What a hostile or broken retailer payload must not be able to do.

Store details are read from a retailer's own HTML. That makes every value in them attacker-
adjacent: a wrong number or an over-long string arrives through the same door as a correct
one. None of it may reach a column, a distance calculation or a response in a form that
breaks a surface a shopper is using.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import Retailer, Store
from app.normalize.categories import CATEGORIES
from app.normalize.hours import DayHours, StoreHours
from app.retailers.base import StoreDetails, StoreLocation
from app.retailers.wholefoods.adapter import parse_store_details
from app.retailers.zipmatch import rank_within_radius, store_point
from app.services.scraper import apply_store_details, scrape_retailer
from app.services.stores import stores_near
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeAdapter

SF = StoreLocation("S1", "Some Store", city="San Francisco", zip_code="94105")


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 91.0, -91.0])
def test_a_coordinate_that_is_not_a_place_is_refused(bad: float) -> None:
    """`math.sin(inf)` raises, and `stores_near` measures every store on every request.

    One unusable latitude in one row would therefore have taken down search, baskets and
    freshness for every ZIP until the row was repaired by hand.
    """
    assert store_point(bad, -122.4, "94105") is not None  # falls back to the ZIP centroid
    point = store_point(bad, -122.4, "94105")
    assert point is not None and point.precision == "zip_centroid"

    assert store_point(bad, -122.4, None) is None  # nothing usable at all


def test_an_unusable_coordinate_cannot_break_the_ranking() -> None:
    stores = [
        StoreLocation("ok", "Fine", zip_code="94107", latitude=37.78, longitude=-122.39),
        StoreLocation("bad", "Broken", zip_code="94107", latitude=float("inf"), longitude=0.0),
    ]

    ranked = rank_within_radius(
        stores,
        "94105",
        point=lambda s: store_point(s.latitude, s.longitude, s.zip_code),
    )

    assert [store.external_id for _, store in ranked] == ["ok", "bad"]  # bad placed by its ZIP


async def test_an_unusable_coordinate_is_never_written(db: AsyncSession) -> None:
    retailer = Retailer(slug="r", name="R")
    db.add(retailer)
    await db.flush()
    store = Store(retailer_id=retailer.id, external_id="S1", name="Store", served_zip_codes=[])
    db.add(store)
    await db.flush()

    apply_store_details(
        store,
        StoreDetails(external_id="S1", latitude=float("nan"), longitude=200.0),
        datetime.now(UTC),
    )

    assert store.latitude is None and store.longitude is None
    assert await stores_near(db, "94105") is not None  # the request still answers


async def test_an_over_long_retailer_string_cannot_roll_back_the_ingest(db: AsyncSession) -> None:
    """PostgreSQL rejects an over-length value, and the rejection would take the whole
    retailer's transaction with it -- prices included, for a store name."""
    retailer = Retailer(slug="r", name="R")
    db.add(retailer)
    await db.flush()
    store = Store(retailer_id=retailer.id, external_id="S1", name="Store", served_zip_codes=[])
    db.add(store)
    await db.flush()

    apply_store_details(
        store,
        StoreDetails(
            external_id="S1",
            name="N" * 500,
            address_line1="A" * 500,
            city="C" * 500,
            state="S" * 90,
        ),
        datetime.now(UTC),
    )
    await db.flush()

    assert len(store.name) <= 200
    assert len(store.address_line1 or "") <= 200
    assert len(store.city or "") <= 100
    assert len(store.state or "") <= 50


async def test_a_details_fetch_that_fails_is_not_retried_on_every_scrape(db: AsyncSession) -> None:
    """Without a stamp, a store whose page 404s is re-fetched every five minutes forever."""
    adapter = FakeAdapter("nodetails", [SF], {"eggs": {"S1": []}}, store_details=None)
    adapter.fetch_store_details = adapter._fetch_store_details  # type: ignore[method-assign]

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert adapter.detail_calls == 1

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()

    store = await db.scalar(select(Store).where(Store.external_id == "S1"))
    assert store is not None and store.hours is None
    assert store.hours_updated_at is not None, "the attempt is recorded even when it found none"
    assert adapter.detail_calls == 1


async def test_details_are_read_again_once_the_cache_has_aged_out(db: AsyncSession) -> None:
    details = StoreDetails(
        external_id="S1",
        hours=StoreHours("America/Los_Angeles", {0: DayHours("08:00", "22:00")}, {}),
        source="fake",
    )
    adapter = FakeAdapter("detailed", [SF], {"eggs": {"S1": []}}, store_details=details)
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert adapter.detail_calls == 1

    store = await db.scalar(select(Store).where(Store.external_id == "S1"))
    assert store is not None
    store.hours_updated_at = datetime.now(UTC) - timedelta(days=30)
    await db.commit()

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()

    assert adapter.detail_calls == 2


async def test_a_failing_details_fetch_never_costs_the_retailer_its_prices(
    db: AsyncSession,
) -> None:
    """Hours are a garnish. A store page that times out must not take the scrape with it."""

    async def explode(store: StoreLocation) -> StoreDetails | None:
        raise TimeoutError("store page took too long")

    adapter = FakeAdapter(
        "boom",
        [SF],
        {"eggs": {"S1": [_egg()]}},
        store_details=StoreDetails(external_id="S1"),
    )
    adapter.fetch_store_details = explode  # type: ignore[method-assign]

    stats = await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()

    assert stats.written == 1, "the prices were collected anyway"


def _egg():
    from decimal import Decimal

    from app.retailers.base import ProductListing

    return ProductListing(
        retailer_sku="e1",
        title="Eggs, 12 CT",
        store_external_id="S1",
        price=Decimal("3.99"),
        regular_price=Decimal("3.99"),
        availability="in_stock",
        stock_status="available",
    )


def test_a_holiday_never_becomes_a_weekdays_standing_hours() -> None:
    """The published window is about a week long, so each weekday appears exactly once.

    Promoting every one of them to the weekly pattern makes a single holiday closure the
    store's standing hours for that weekday until the next scrape -- a Friday that reads
    "Closed" for a week after Christmas.
    """
    page = _store_page_with_daily_hours(
        [
            ("2026-12-21", "15:00", "06:00"),  # Mon, 07:00-22:00 local
            ("2026-12-22", "15:00", "06:00"),
            ("2026-12-23", "15:00", "06:00"),
            ("2026-12-24", "15:00", "06:00"),
            ("2026-12-25", None, None),  # Friday: closed for Christmas
            ("2026-12-26", "15:00", "06:00"),
            ("2026-12-27", "15:00", "06:00"),
        ]
    )

    details = parse_store_details(page)

    assert details is not None and details.hours is not None
    assert details.hours.dates["2026-12-25"] == DayHours(None, None), "the date is still right"
    assert 4 not in details.hours.weekly, "Friday keeps no standing hours from one closure"
    assert details.hours.weekly[0] == DayHours("07:00", "22:00")


def _store_page_with_daily_hours(days: list[tuple[str, str | None, str | None]]) -> str:
    import json

    daily = []
    for date, start, end in days:
        windows = []
        if start and end:
            next_day = f"{date[:8]}{int(date[8:]) + 1:02d}"
            windows = [{"startTime": f"{date}T{start}:00Z", "endTime": f"{next_day}T{end}:00Z"}]
        daily.append({"date": f"{date}T08:00:00Z", "operatingHours": windows})
    state = {
        "location": {
            "locationName": "Test",
            "storeCode": "10000",
            "geocode": {"latitude": 37.7, "longitude": -122.4},
            "address": {"addressLines": ["1 Test St"], "city": "SF", "state": "CA"},
            "locationFacets": [{"timeZone": "America/Los_Angeles"}],
            "operationalDailyHours": daily,
        }
    }
    return (
        '<script type="a-state" data-a-state="{&quot;key&quot;:&quot;detail-page-state&quot;}">'
        + json.dumps(state)
        + "</script>"
    )
