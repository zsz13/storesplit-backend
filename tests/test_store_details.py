"""Reading a store's own details -- name, address, timezone and opening hours.

An optional adapter capability: Whole Foods publishes all of it on its store page, most
retailers publish none of it anywhere StoreSplit is allowed to read, and the offer row says
"Hours not published" for those rather than inventing a schedule.
"""

from pathlib import Path

from app.db.models import Store
from app.normalize.categories import CATEGORIES
from app.normalize.hours import DayHours, StoreHours
from app.retailers.base import StoreDetails, StoreLocation
from app.retailers.wholefoods.adapter import parse_store_details
from app.services.scraper import scrape_retailer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _ocean() -> str:
    return (FIXTURES / "wholefoods" / "store_page_ocean.html").read_text()


def test_store_details_are_read_from_the_store_page() -> None:
    details = parse_store_details(_ocean())

    assert details is not None
    assert details.external_id == "10432"
    assert details.name == "Whole Foods Ocean"
    assert details.address_line1 == "1150 Ocean Avenue"
    assert details.city == "San Francisco" and details.state == "CA"
    assert details.zip_code == "94112"  # the payload's "94112-1843", trimmed to five
    assert (details.latitude, details.longitude) == (37.72387, -122.454877)


def test_published_hours_become_a_weekly_schedule_in_the_store_timezone() -> None:
    """The retailer publishes absolute UTC windows per date; a shopper reads a wall clock."""
    details = parse_store_details(_ocean())

    assert details is not None and details.hours is not None
    hours = details.hours
    assert hours.timezone == "America/Los_Angeles"
    # 2026-09-10T15:00Z -> 08:00 local, 2026-09-11T05:00Z -> 22:00 local the same day.
    assert hours.dates["2026-09-10"].opens == "08:00"
    assert hours.dates["2026-09-10"].closes == "22:00"
    # Every published day carries the same window, so the weekly pattern is complete.
    assert set(hours.weekly) == set(range(7))
    assert all(day.opens == "08:00" and day.closes == "22:00" for day in hours.weekly.values())


def test_a_page_that_is_not_a_store_page_yields_nothing() -> None:
    assert parse_store_details("<html><body>no state here</body></html>") is None
    assert parse_store_details("") is None


async def test_a_scrape_records_the_store_details_its_adapter_publishes(db: AsyncSession) -> None:
    location = StoreLocation("S1", "Some Store", city="San Francisco", zip_code="94105")
    details = StoreDetails(
        external_id="S1",
        name="Some Store, Market Street",
        address_line1="1 Market St",
        city="San Francisco",
        state="CA",
        zip_code="94105",
        latitude=37.7936,
        longitude=-122.3965,
        hours=StoreHours(
            timezone="America/Los_Angeles",
            weekly={day: DayHours("08:00", "22:00") for day in range(7)},
            dates={},
        ),
        source="fake:store-page",
    )
    adapter = FakeAdapter("detailed", [location], {"eggs": {"S1": []}}, store_details=details)

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()

    store = await db.scalar(select(Store).where(Store.external_id == "S1"))
    assert store is not None
    assert store.name == "Some Store, Market Street"  # the store's own page beats the locator
    assert store.address_line1 == "1 Market St"
    assert store.timezone == "America/Los_Angeles"
    assert store.hours_source == "fake:store-page"
    assert store.hours_updated_at is not None
    assert store.hours == {
        "weekly": {str(day): {"opens": "08:00", "closes": "22:00"} for day in range(7)},
        "dates": {},
    }


async def test_a_retailer_that_publishes_no_details_leaves_the_hours_empty(
    db: AsyncSession,
) -> None:
    """Raley's serves its store details from a robots-disallowed path, so it has none."""
    location = StoreLocation("R1", "Nob Hill Alameda", city="Alameda", zip_code="94501")
    adapter = FakeAdapter("plain", [location], {"eggs": {"R1": []}})
    assert not hasattr(adapter, "fetch_store_details")

    await scrape_retailer(db, adapter, "94501", [CATEGORIES["eggs"]], 2)
    await db.commit()

    store = await db.scalar(select(Store).where(Store.external_id == "R1"))
    assert store is not None
    assert store.hours is None and store.timezone is None, "nothing was published, so nothing"
    # The *attempt* is stamped even though there was nothing to attempt, because that stamp
    # is the weekly gate: it covers the Google place lookup as well, and a store that is
    # never stamped is a store re-sent to a billed API on every scrape, for ever.
    assert store.hours_updated_at is not None


async def test_details_are_not_refetched_while_they_are_still_fresh(db: AsyncSession) -> None:
    location = StoreLocation("S1", "Some Store", city="San Francisco", zip_code="94105")
    details = StoreDetails(external_id="S1", name="Some Store", source="fake:store-page")
    adapter = FakeAdapter("detailed", [location], {"eggs": {"S1": []}}, store_details=details)

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert adapter.detail_calls == 1

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()

    assert adapter.detail_calls == 1, "a week's cache, not a fetch per scrape"
