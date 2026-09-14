"""Comparing only the shops that are not shut, decided in each shop's own timezone.

The filter is deliberately narrow in two directions.

It **removes only a confirmed closure**. A store whose retailer publishes no hours is not
evidence of a locked door, and dropping it would quietly delete whole retailers from the
comparison -- every Raley's and every Kroger -- on the strength of a fact nobody has. Those
stores stay, ranked below the ones that really are open, so nothing unknown is *presented*
as open either.

And it **narrows which stores are compared, never how offers rank**. Every query is already
scoped by store, so the filter needs no clause of its own -- and cannot reorder anything.
The cheapest offer among the shops that are open is still the cheapest offer.
"""

from datetime import UTC, datetime

import pytest
from app.db.models import Store
from app.normalize.hours import DayHours, StoreHours, hours_today, store_hours_to_json
from app.services.scraper import run_scrape
from app.services.stores import open_now_stores, store_hours_state
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import STORE_A, STORE_B, FakeAdapter, listing
from tests.test_scrape_and_api import use_adapters

PACIFIC_NOON = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)  # 12:00 in America/Los_Angeles

# Both of these are true whatever the clock says, because the tests that drive the API
# cannot choose when they run: the service asks `utc_now()`. A window of "08:00 to 22:00"
# would make this suite pass in the afternoon and fail at midnight.
ALWAYS_OPEN = StoreHours(
    timezone="America/Los_Angeles",
    weekly={day: DayHours("00:00", "00:00") for day in range(7)},  # a day with no shut moment
    dates={},
)
ALWAYS_SHUT = StoreHours(
    timezone="America/Los_Angeles",
    weekly={day: DayHours(None, None) for day in range(7)},
    dates={},
)


def two_shops() -> tuple[FakeAdapter, FakeAdapter]:
    """One egg each, same brand and package, so both offers compete inside one product."""
    alpha = FakeAdapter(
        "alpha",
        [STORE_A],
        {"eggs": {"A1": [listing("a-eggs", "Eggs, 12 CT", "A1", "3.99", brand="Solid")]}},
    )
    beta = FakeAdapter(
        "beta",
        [STORE_B],
        {"eggs": {"B1": [listing("b-eggs", "Eggs, 12 CT", "B1", "4.99", brand="Solid")]}},
    )
    return alpha, beta


@pytest.fixture
async def shops(sessionmaker, clients, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha, beta = two_shops()
    use_adapters(monkeypatch, alpha, beta)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])


async def set_hours(db: AsyncSession, external_id: str, hours: StoreHours | None) -> None:
    store = (await db.scalars(select(Store).where(Store.external_id == external_id))).one()
    store.hours = store_hours_to_json(hours) if hours else None
    store.timezone = hours.timezone if hours else None
    await db.commit()


# ------------------------------------------------------------------ the store-level decision


def test_a_store_with_no_published_hours_is_unknown_and_not_closed() -> None:
    assert store_hours_state(Store(hours=None, timezone=None), PACIFIC_NOON) == "unknown"


def test_open_now_keeps_the_unknown_and_ranks_it_after_the_open() -> None:
    shut = Store(id=1, hours=store_hours_to_json(ALWAYS_SHUT), timezone=ALWAYS_SHUT.timezone)
    unknown = Store(id=2, hours=None, timezone=None)
    open_shop = Store(id=3, hours=store_hours_to_json(ALWAYS_OPEN), timezone=ALWAYS_OPEN.timezone)

    kept = open_now_stores([shut, unknown, open_shop], PACIFIC_NOON)

    assert [s.id for s in kept] == [3, 2], "the open one first, the unaccounted-for after it"


def test_open_now_preserves_distance_order_within_each_group() -> None:
    """`stores_near` ranked these by distance and that ranking still means something."""
    nearer = Store(id=1, hours=store_hours_to_json(ALWAYS_OPEN), timezone="America/Los_Angeles")
    further = Store(id=2, hours=store_hours_to_json(ALWAYS_OPEN), timezone="America/Los_Angeles")

    assert [s.id for s in open_now_stores([nearer, further], PACIFIC_NOON)] == [1, 2]
    assert [s.id for s in open_now_stores([further, nearer], PACIFIC_NOON)] == [2, 1]


def test_a_store_is_judged_in_its_own_timezone_not_the_servers() -> None:
    """22:00 Pacific is 01:00 the next day in New York, where this shop has been shut for
    three hours. Reading the schedule in the server's zone would call it open."""
    eastern = Store(
        hours=store_hours_to_json(
            StoreHours("America/New_York", {d: DayHours("08:00", "22:00") for d in range(7)}, {})
        ),
        timezone="America/New_York",
    )
    pacific_evening = datetime(2026, 9, 11, 5, 0, tzinfo=UTC)  # 22:00 Pacific, 01:00 Eastern

    assert store_hours_state(eastern, pacific_evening) == "closed"


def test_a_shop_open_past_midnight_is_open_after_midnight() -> None:
    """The window that decides this began *yesterday*. `_open_window` looks back a day for
    exactly this, and without that lookback a bar-hours grocer reads shut from midnight on."""
    overnight = Store(
        hours=store_hours_to_json(
            StoreHours("America/Los_Angeles", {d: DayHours("20:00", "02:00") for d in range(7)}, {})
        ),
        timezone="America/Los_Angeles",
    )

    # 01:00 Pacific on the 11th: inside the window that opened at 20:00 on the 10th.
    assert store_hours_state(overnight, datetime(2026, 9, 11, 8, 0, tzinfo=UTC)) == "open"
    # 03:00 Pacific, an hour after it shut and seventeen before it opens again.
    assert store_hours_state(overnight, datetime(2026, 9, 11, 10, 0, tzinfo=UTC)) == "closed"


def test_a_shop_that_never_closes_is_open_at_every_hour() -> None:
    """Midnight to midnight. The other half of "handle overnight hours and Open 24 hours"."""
    for hour in (0, 3, 12, 23):
        moment = datetime(2026, 9, 11, hour, 0, tzinfo=UTC)
        store = Store(hours=store_hours_to_json(ALWAYS_OPEN), timezone=ALWAYS_OPEN.timezone)
        assert store_hours_state(store, moment) == "open", hour


def test_next_open_at_crosses_more_than_one_shut_day() -> None:
    """A shop shut Saturday and Sunday opens on Monday, and the dialog has to say Monday --
    `_next_opening` scans forward, and the label stops being "tomorrow" past one day."""
    weekdays_only = StoreHours(
        "America/Los_Angeles",
        {
            **{d: DayHours("08:00", "18:00") for d in range(5)},
            5: DayHours(None, None),
            6: DayHours(None, None),
        },
        {},
    )

    # Saturday 2026-09-12, 12:00 Pacific.
    today = hours_today(weekdays_only, datetime(2026, 9, 12, 19, 0, tzinfo=UTC))

    assert today.state == "closed"
    assert today.closed_all_day is True
    assert today.opens_at == "08:00"
    assert today.opens_day == "Monday", "not 'tomorrow': Sunday is shut too"
    assert today.next_open_at == datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


# ------------------------------------------------------------------------ through the API


async def test_search_without_the_filter_compares_every_store(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    await set_hours(db, "A1", ALWAYS_SHUT)
    await set_hours(db, "B1", ALWAYS_OPEN)

    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()

    assert body["open_now"] is False
    assert {o["retailer_sku"] for p in body["products"] for o in p["offers"]} == {
        "a-eggs",
        "b-eggs",
    }


async def test_open_now_drops_the_offers_of_a_shop_that_is_shut(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    await set_hours(db, "A1", ALWAYS_SHUT)
    await set_hours(db, "B1", ALWAYS_OPEN)

    body = (
        await client.get(
            "/products/search", params={"q": "eggs", "zip_code": "94105", "open_now": "true"}
        )
    ).json()

    assert body["open_now"] is True
    assert {o["retailer_sku"] for p in body["products"] for o in p["offers"]} == {"b-eggs"}


async def test_open_now_keeps_a_shop_whose_hours_nobody_publishes(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    """Raley's and Kroger, today. Unknown hours are not evidence of a locked door."""
    await set_hours(db, "A1", None)
    await set_hours(db, "B1", ALWAYS_OPEN)

    body = (
        await client.get(
            "/products/search", params={"q": "eggs", "zip_code": "94105", "open_now": "true"}
        )
    ).json()

    assert {o["retailer_sku"] for p in body["products"] for o in p["offers"]} == {
        "a-eggs",
        "b-eggs",
    }


async def test_the_store_list_is_never_narrowed_by_the_filter(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    """A client that filtered down to nothing still has to be able to say *which* shops are
    shut and when they open, and that answer is in these rows."""
    await set_hours(db, "A1", ALWAYS_SHUT)
    await set_hours(db, "B1", ALWAYS_SHUT)

    body = (
        await client.get(
            "/products/search", params={"q": "eggs", "zip_code": "94105", "open_now": "true"}
        )
    ).json()

    assert body["products"] == []
    assert len(body["stores"]) == 2
    assert {s["hours_today"]["state"] for s in body["stores"]} == {"closed"}


async def test_a_store_shut_every_day_has_no_opening_to_wait_for(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    await set_hours(db, "A1", ALWAYS_OPEN)
    await set_hours(db, "B1", ALWAYS_SHUT)

    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    by_state = {s["hours_today"]["state"]: s["hours_today"] for s in body["stores"]}

    assert by_state["open"]["next_open_at"] is None, "it is open; there is nothing to wait for"
    assert by_state["closed"]["closed_all_day"] is True
    assert by_state["closed"]["next_open_at"] is None, "shut every day, so nothing to open towards"


def test_next_open_at_is_the_instant_a_shut_shop_opens() -> None:
    """The instant, not the wall clock. The "everything near you is closed" dialog orders
    shops by it, and two shops in different zones print the same "8:00 AM" without meaning
    the same moment -- so the dialog would sort them by coincidence of spelling."""
    pacific = hours_today(
        StoreHours("America/Los_Angeles", {d: DayHours("08:00", "22:00") for d in range(7)}, {}),
        datetime(2026, 9, 10, 13, 0, tzinfo=UTC),  # 06:00 Pacific, two hours before it opens
    )
    eastern = hours_today(
        StoreHours("America/New_York", {d: DayHours("08:00", "22:00") for d in range(7)}, {}),
        datetime(2026, 9, 10, 10, 0, tzinfo=UTC),  # 06:00 Eastern, two hours before it opens
    )

    assert pacific.opens_at == eastern.opens_at == "08:00", "the same string"
    assert pacific.next_open_at == datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    assert eastern.next_open_at == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert pacific.next_open_at > eastern.next_open_at, "and not the same moment"


async def test_open_now_narrows_the_basket_to_shops_worth_a_trip(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    await set_hours(db, "A1", ALWAYS_SHUT)
    await set_hours(db, "B1", ALWAYS_OPEN)
    request = {"zip_code": "94105", "items": [{"query": "eggs", "quantity": 1, "unit": "dozen"}]}

    everything = (await client.post("/basket/compare", json=request)).json()
    only_open = (await client.post("/basket/compare", json={**request, "open_now": True})).json()

    assert {o["store"]["name"] for o in everything["single_store_options"]} == {
        STORE_A.name,
        STORE_B.name,
    }
    assert {o["store"]["name"] for o in only_open["single_store_options"]} == {STORE_B.name}
    assert only_open["open_now"] is True
    assert len(only_open["stores"]) == 2, "the shut shop is still named, so a client can say so"


async def test_the_cheapest_badge_still_means_cheapest_among_what_was_compared(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    """The filter narrows the shops; it must never reorder the offers inside a product, or
    the badge would sit on something that is not the lowest unit price on screen."""
    await set_hours(db, "A1", ALWAYS_SHUT)  # A1 is the cheaper of the two
    await set_hours(db, "B1", ALWAYS_OPEN)

    body = (
        await client.get(
            "/products/search", params={"q": "eggs", "zip_code": "94105", "open_now": "true"}
        )
    ).json()
    offers = [o for p in body["products"] for o in p["offers"]]

    assert body["cheapest_offer_id"] == offers[0]["id"]
    assert offers[0]["retailer_sku"] == "b-eggs", "the cheapest one still standing"


async def test_the_drill_down_agrees_with_the_list_it_was_opened_from(
    client: AsyncClient, db: AsyncSession, shops
) -> None:
    """A product page that contradicted the search above it about which shops are shut would
    be worse than either answer alone."""
    await set_hours(db, "A1", ALWAYS_SHUT)
    await set_hours(db, "B1", ALWAYS_OPEN)
    listed = (
        await client.get(
            "/products/search", params={"q": "eggs", "zip_code": "94105", "availability": "all"}
        )
    ).json()
    product_id = listed["products"][0]["id"]

    everything = (
        await client.get(f"/products/{product_id}/offers", params={"availability": "all"})
    ).json()
    only_open = (
        await client.get(
            f"/products/{product_id}/offers", params={"availability": "all", "open_now": "true"}
        )
    ).json()

    assert {o["retailer_sku"] for o in everything["product"]["offers"]} == {"a-eggs", "b-eggs"}
    assert {o["retailer_sku"] for o in only_open["product"]["offers"]} == {"b-eggs"}
