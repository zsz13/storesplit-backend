"""What a Lucky or Save Mart store is, and where each thing said about it comes from.

These two banners are the case the store layer was weakest at: the Instacart storefront that
prices them names a shop and nothing else -- no street, no name, no zone, no hours -- so a
Lucky row used to read "Lucky Supermarkets 23130" with an empty address and "Hours not
published", beside Safeway rows that had all four.

Three surfaces answer between them, and the tests below are grouped by which:

* `DefaultShop` -- the shop serving a ZIP, and the `retailerLocationId` it sells from. **A
  shop is one fulfilment mode of a store**, so the location is the identity and the shop is
  only an address for price queries.
* `/v3/retailers/<id>/pickup_locations` -- the storefront's own pickup picker, the one
  anonymous surface that says what a location id *is*: a street address and the banner's own
  store number.
* `<banner site>/stores/<store number>` -- luckysupermarkets.com and savemart.com, a
  different host with its own permissive robots.txt, where the store's name, exact address,
  point, timezone, phone and week are published.

Every fixture is a real capture, trimmed to the record the parser reads.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.normalize.hours import DayHours, hours_today, parse_clock_24h
from app.retailers.base import StoreLocation
from app.retailers.clients import RetailerClients
from app.retailers.savemartco.adapter import LuckyAdapter, SaveMartAdapter
from app.retailers.savemartco.storefront import (
    LUCKY_BANNER,
    OPEN_ALL_DAY,
    SAVEMART_BANNER,
    PickupLocation,
    Shop,
    parse_default_shop,
    parse_pickup_locations,
    parse_special_days,
    parse_store_hours,
    store_details_from_record,
    store_display_name,
    store_from_shop,
)

FIXTURES = Path(__file__).parent / "fixtures"
PACIFIC_NOON = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)  # 12:00 in America/Los_Angeles
PACIFIC_3AM = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)  # 03:00 in America/Los_Angeles
SOURCE = "lucky:stores/storeDetailsV2"


def load(*parts: str) -> dict:
    return json.loads((FIXTURES.joinpath(*parts)).read_text())


def lucky_record(store_number: str) -> dict:
    return load("lucky", f"store_{store_number}.json")["storeDetailsV2"]


# ------------------------------------------------------- DefaultShop: shop vs physical store


def test_the_shop_and_the_store_it_sells_from_are_different_ids() -> None:
    """The whole reason a Lucky row is no longer keyed on a shop id."""
    shop = parse_default_shop(load("lucky", "default_shop_94538.json"))

    assert shop is not None
    assert shop.shop_id == "24854"
    assert shop.location_id == "34737"
    assert shop.retailer_id == "542", "the banner's own Instacart id, read rather than pinned"
    assert shop.store_id == "34737", "a store row is the location, not one of its shops"


def test_a_shop_that_names_no_location_still_identifies_itself() -> None:
    """Degrading to the old behaviour, not to nothing: prices keep flowing, and the store is
    simply not merged with anything."""
    shop = parse_default_shop({"data": {"defaultShop": {"id": "6575"}}})

    assert shop is not None and shop.store_id == "6575"
    assert shop.location_id is None and shop.retailer_id is None


def test_no_shop_where_the_banner_has_no_store_there() -> None:
    assert parse_default_shop({"data": {"defaultShop": None}}) is None
    assert parse_default_shop({"data": {}}) is None
    assert parse_default_shop({}) is None


# ----------------------------------------------- the pickup picker: what a location id means


def test_the_pickup_picker_turns_a_location_id_into_an_address_and_a_store_number() -> None:
    locations = parse_pickup_locations(load("lucky", "pickup_locations_94538.json"))

    mowry = next(location for location in locations if location.location_id == "34737")
    assert mowry.store_number == "714", "the banner's own number -- what its website is keyed on"
    assert mowry.address_line1 == "5000 Mowry Ave"
    assert (mowry.city, mowry.state, mowry.zip_code) == ("Fremont", "CA", "94538")
    assert (mowry.latitude, mowry.longitude) == (37.53469, -121.997896)


def test_the_picker_is_ordered_nearest_first_and_holds_each_store_once() -> None:
    locations = parse_pickup_locations(load("lucky", "pickup_locations_94538.json"))

    assert [location.store_number for location in locations] == ["714", "713", "780"]
    assert len({location.location_id for location in locations}) == len(locations)


def test_two_records_for_one_location_are_one_store() -> None:
    """The duplicate this whole change exists to prevent, refused at the parser."""
    payload = {
        "pickup_locations": [
            {"id": "34737", "location_code": "714", "address": {"address_line_1": "5000 Mowry"}},
            {"id": "34737", "location_code": "714", "address": {"address_line_1": "5000 Mowry"}},
        ]
    }

    assert [location.location_id for location in parse_pickup_locations(payload)] == ["34737"]


@pytest.mark.parametrize("code", ["", None, "../../admin", "714a", "7" * 11])
def test_a_store_number_that_is_not_one_is_refused(code: object) -> None:
    """It is pasted into a URL path, so it is checked before it becomes one."""
    payload = {"pickup_locations": [{"id": "34737", "location_code": code, "address": {}}]}

    assert parse_pickup_locations(payload)[0].store_number is None


def test_a_picker_payload_that_says_nothing_yields_nothing() -> None:
    assert parse_pickup_locations({}) == []
    assert parse_pickup_locations({"pickup_locations": None}) == []
    assert parse_pickup_locations({"pickup_locations": ["not a record"]}) == []
    assert parse_pickup_locations([]) == []


# ------------------------------------------------ the banner's own site: name, place, hours


def test_the_banner_publishes_everything_the_storefront_withholds() -> None:
    details = store_details_from_record(lucky_record("714"), "34737", SOURCE, banner=LUCKY_BANNER)

    assert details is not None
    assert details.external_id == "34737", "the store asked about, not the banner's number"
    assert details.name == "Lucky Supermarkets - Mowry"
    assert details.address_line1 == "5000 Mowry Ave"
    assert (details.city, details.state, details.zip_code) == ("Fremont", "CA", "94538")
    assert (round(details.latitude or 0, 3), round(details.longitude or 0, 3)) == (37.535, -122.0)
    assert details.phone == "+15107441660"
    assert details.source == SOURCE
    assert details.hours is not None and details.hours.timezone == "America/Los_Angeles"


def test_the_week_is_the_one_the_store_keeps_including_its_late_nights() -> None:
    """A real weekly pattern, so nothing is generalised from a published fortnight."""
    details = store_details_from_record(lucky_record("714"), "34737", SOURCE, banner=LUCKY_BANNER)

    assert details is not None and details.hours is not None
    weekly = details.hours.weekly
    assert sorted(weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert weekly[0] == DayHours("06:00", "22:00"), "Monday"
    assert weekly[4] == DayHours("06:00", "23:00"), "Friday is an hour later"
    assert details.hours.dates == {}, "this banner publishes no dated exceptions"


def test_a_store_open_now_closes_at_the_hour_the_banner_published() -> None:
    details = store_details_from_record(lucky_record("714"), "34737", SOURCE, banner=LUCKY_BANNER)
    assert details is not None

    today = hours_today(details.hours, PACIFIC_NOON)

    assert (today.state, today.closes_at) == ("open", "22:00")


def test_a_store_shut_at_three_in_the_morning_is_told_when_it_opens() -> None:
    details = store_details_from_record(lucky_record("714"), "34737", SOURCE, banner=LUCKY_BANNER)
    assert details is not None

    today = hours_today(details.hours, PACIFIC_3AM)

    assert (today.state, today.opens_at, today.opens_day) == ("closed", "06:00", "today")
    assert not today.closed_all_day


def test_a_store_whose_hours_the_banner_does_not_publish_keeps_none() -> None:
    """Lucky 212 states an address, a point and a zone, and an empty week. The store is real;
    its hours are not published, and "Hours not published" is the honest row."""
    details = store_details_from_record(lucky_record("212"), "34691", SOURCE, banner=LUCKY_BANNER)

    assert details is not None
    assert details.name == "Lucky Supermarkets - Contra Loma"
    assert details.address_line1 == "3190 Contra Loma Blvd"
    assert details.phone == "+19257548824"
    assert details.hours is None
    assert hours_today(details.hours, PACIFIC_NOON).state == "unknown"


def test_a_store_open_round_the_clock_says_so_with_no_clock_at_all() -> None:
    """`OPEN_24_HOURS` carries no times. Midnight to midnight is the same statement, and it
    is what `HoursToday` reports with equal `opens_at` and `closes_at`."""
    record = load("savemart", "store_655.json")["storeDetailsV2"]

    details = store_details_from_record(record, "34xxx", "savemart:x", banner=SAVEMART_BANNER)

    assert details is not None and details.hours is not None
    assert details.name == "Save Mart - West Lodi"
    assert set(details.hours.weekly.values()) == {OPEN_ALL_DAY}
    today = hours_today(details.hours, PACIFIC_3AM)
    assert (today.state, today.opens_at, today.closes_at) == ("open", "00:00", "00:00")


def test_a_record_with_no_store_in_it_is_not_a_store() -> None:
    """A retired number answers 404 on these banners, but a null record is the other shape
    this platform uses and the status code is not what is trusted."""
    assert store_details_from_record({}, "34737", SOURCE, banner=LUCKY_BANNER) is None
    assert store_details_from_record({"storeId": None}, "1", SOURCE, banner=LUCKY_BANNER) is None


# --------------------------------------------------------------------------- reading a week


def test_hours_without_the_zone_they_are_kept_in_are_not_hours() -> None:
    """The rule that keeps Trader Joe's out of the product, applied here."""
    week = {
        "weekly": [
            {
                "day": "MONDAY",
                "daily": {"open": {"open": "06:00:00", "close": "22:00:00"}, "type": "OPEN"},
            }
        ]
    }

    assert parse_store_hours(week, None) is None
    assert parse_store_hours(week, "") is None
    assert parse_store_hours(week, "America/Los_Angeles") is not None


def test_a_stated_closure_is_a_stated_fact() -> None:
    week = {"weekly": [{"day": "SUNDAY", "daily": {"type": "CLOSED"}}]}

    hours = parse_store_hours(week, "America/Los_Angeles")

    assert hours is not None and hours.weekly == {6: DayHours(None, None)}


def test_a_day_nobody_has_read_is_left_out_of_the_week_rather_than_guessed() -> None:
    """An unknown `type` must not become "closed": absent reads as unknown, closed reads as
    a promise that the store is shut."""
    week = {
        "weekly": [
            {"day": "MONDAY", "daily": {"type": "BY_APPOINTMENT"}},
            {
                "day": "TUESDAY",
                "daily": {"open": {"open": "06:00:00", "close": "22:00:00"}, "type": "OPEN"},
            },
        ]
    }

    hours = parse_store_hours(week, "America/Los_Angeles")

    assert hours is not None and sorted(hours.weekly) == [1]


@pytest.mark.parametrize(
    "daily",
    [
        {"type": "OPEN"},
        {"type": "OPEN", "open": {}},
        {"type": "OPEN", "open": {"open": "06:00:00"}},
        {"type": "OPEN", "open": {"open": "sunrise", "close": "sunset"}},
        {"type": "OPEN", "open": "06:00-22:00"},
        "not a block",
    ],
)
def test_a_window_that_cannot_be_read_yields_no_day(daily: object) -> None:
    assert parse_store_hours({"weekly": [{"day": "MONDAY", "daily": daily}]}, "UTC") is None


def test_a_weekday_nobody_names_is_not_a_weekday() -> None:
    week = {"weekly": [{"day": "CANDLEDAY", "daily": {"type": "OPEN_24_HOURS"}}]}

    assert parse_store_hours(week, "America/Los_Angeles") is None


def test_a_dated_exception_is_read_when_it_carries_a_date_and_a_window() -> None:
    """`hours.special` is empty on all 189 stores of the three banners, so this is the shape
    that is already known -- a `daily` block -- beside a date that parses as one."""
    special = [{"date": "2026-12-25", "daily": {"type": "CLOSED"}}]

    assert parse_special_days(special) == {"2026-12-25": DayHours(None, None)}


@pytest.mark.parametrize(
    "entry",
    [
        {"daily": {"type": "CLOSED"}},
        {"date": "Christmas", "daily": {"type": "CLOSED"}},
        {"date": "2026-12-25"},
        {"date": "2026-12-25", "daily": {"type": "SOMETHING_NEW"}},
        "not a record",
    ],
)
def test_an_exception_nobody_can_read_is_skipped_rather_than_guessed_at(entry: object) -> None:
    """The day then falls back to the store's standing hours, which is what the banner's own
    site shows today. Guessing could tell a shopper a shut store is open."""
    assert parse_special_days([entry]) == {}


def test_a_dated_exception_beats_the_weekly_window_for_that_day() -> None:
    week = {
        "weekly": [
            {
                "day": "THURSDAY",
                "daily": {"open": {"open": "06:00:00", "close": "22:00:00"}, "type": "OPEN"},
            }
        ],
        "special": [{"date": "2026-09-10", "daily": {"type": "CLOSED"}}],
    }

    hours = parse_store_hours(week, "America/Los_Angeles")

    assert hours is not None
    today = hours_today(hours, PACIFIC_NOON)  # a Thursday
    assert (today.state, today.closed_all_day) == ("closed", True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("06:00:00", "06:00"),
        ("22:00", "22:00"),
        ("00:00:00", "00:00"),
        ("24:00:00", "00:00"),
        ("6:05:00", "06:05"),
        ("25:00:00", None),
        ("06:60:00", None),
        ("6pm", None),
        ("", None),
        (None, None),
        (600, None),
    ],
)
def test_the_machine_readable_clock_is_read_or_refused(raw: object, expected: str | None) -> None:
    assert parse_clock_24h(raw) == expected


# ------------------------------------------------------------------ the name and the number


def test_the_branch_is_named_with_its_retailer_in_front_of_it() -> None:
    """A shout in capitals is a heading on the banner's own page and noise in a list of nine
    retailers; and `services/maps.py` searches with this string, where "Mowry" alone names no
    supermarket and Google resolves the street."""
    assert store_display_name(LUCKY_BANNER, "MOWRY") == "Lucky Supermarkets - Mowry"
    assert store_display_name(SAVEMART_BANNER, "WEST LODI") == "Save Mart - West Lodi"
    assert store_display_name(LUCKY_BANNER, "") is None
    assert store_display_name(LUCKY_BANNER, None) is None


@pytest.mark.parametrize(
    ("numbers", "expected"),
    [
        ([{"value": "15107441660", "description": "Main"}], "+15107441660"),
        ([{"value": "(510) 744-1660", "description": "Main"}], "+15107441660"),
        (
            [
                {"value": "15105550000", "description": "Pharmacy"},
                {"value": "15107441660", "description": "Main"},
            ],
            "+15107441660",
        ),
        ([{"value": "744-1660", "description": "Main"}], None),
        ([{"value": "", "description": "Main"}], None),
        ([], None),
        (None, None),
    ],
)
def test_a_number_is_kept_only_when_it_really_is_one(numbers: object, expected: str | None) -> None:
    record = {"storeId": "714", "phoneNumbers": numbers}

    details = store_details_from_record(record, "34737", SOURCE, banner=LUCKY_BANNER)

    assert details is not None and details.phone == expected


# ----------------------------------------------------- assembling one store from the three


def test_a_store_is_the_location_named_placed_and_linked_to_its_own_page() -> None:
    shop = Shop(shop_id="24854", retailer_id="542", location_id="34737")
    location = parse_pickup_locations(load("lucky", "pickup_locations_94538.json"))[0]
    details = store_details_from_record(lucky_record("714"), "34737", SOURCE, banner=LUCKY_BANNER)

    store = store_from_shop(shop, location, details, LUCKY_BANNER, "94538")

    assert store.external_id == "34737", "the physical store, not the shop"
    assert store.name == "Lucky Supermarkets - Mowry"
    assert store.address_line1 == "5000 Mowry Ave"
    assert store.store_number == "714"
    assert store.details_url == "https://luckysupermarkets.com/stores/714"
    assert store.latitude is not None and store.longitude is not None


def test_the_pickers_address_stands_in_when_the_banners_site_says_nothing() -> None:
    """A week when luckysupermarkets.com is unreachable still has a street and a point, so
    the store still ranks by distance and still gets a map link."""
    shop = Shop(shop_id="24854", retailer_id="542", location_id="34737")
    location = parse_pickup_locations(load("lucky", "pickup_locations_94538.json"))[0]

    store = store_from_shop(shop, location, None, LUCKY_BANNER, "94538")

    assert store.external_id == "34737"
    assert store.name == "Lucky Supermarkets - 714"
    assert (store.address_line1, store.city) == ("5000 Mowry Ave", "Fremont")
    assert store.store_number == "714"


def test_a_store_neither_surface_could_describe_is_still_a_store() -> None:
    """Exactly what this returned before any of this existed: a shop and the ZIP that found
    it. Prices are what a scrape is for; a street is what it is not."""
    store = store_from_shop(Shop(shop_id="6575"), None, None, LUCKY_BANNER, "94509")

    assert store.external_id == "6575"
    assert store.name == "Lucky Supermarkets 6575"
    assert (store.address_line1, store.store_number, store.details_url) == (None, None, None)
    assert store.zip_code == "94509"


# --------------------------------------------------------------- the adapter, over HTTP


class FakeBanner:
    """The three surfaces a Save Mart Companies store is assembled from, as one transport.

    Routed by host and path rather than by call order, so a test fails on *which* request the
    adapter made rather than on how many.
    """

    def __init__(self, *, picker: object = None, store: object = None, store_status: int = 200):
        self.requests: list[httpx.Request] = []
        self._picker = picker
        self._store = store
        self._store_status = store_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/storefront"):
            return httpx.Response(
                200, text="<html></html>", headers={"set-cookie": "__Host-instacart_sid=x; Path=/"}
            )
        if path == "/graphql":
            return httpx.Response(200, json=load("lucky", "default_shop_94538.json"))
        if path.endswith("/pickup_locations"):
            if self._picker is None:
                return httpx.Response(500, text="picker down")
            return httpx.Response(200, json=self._picker)
        if path.startswith("/stores/"):
            if self._store is None:
                return httpx.Response(self._store_status, text="store page down")
            return httpx.Response(self._store_status, json=self._store)
        return httpx.Response(404, text=f"unexpected {request.url}")

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def lucky_adapter(clients: RetailerClients, site: FakeBanner) -> LuckyAdapter:
    adapter = LuckyAdapter(clients)
    transport = httpx.MockTransport(site.handler)
    adapter._storefront._client = httpx.AsyncClient(transport=transport)
    adapter._details_client = httpx.AsyncClient(transport=transport)
    return adapter


def whole_site(**kwargs: object) -> FakeBanner:
    return FakeBanner(
        picker=kwargs.get("picker", load("lucky", "pickup_locations_94538.json")),
        store=kwargs.get("store", load("lucky", "store_714.json")),
    )


async def test_find_stores_joins_the_three_surfaces_into_one_store(clients) -> None:
    site = whole_site()
    adapter = lucky_adapter(clients, site)

    stores = await adapter.find_stores("94538")

    assert len(stores) == 1
    store = stores[0]
    assert store.external_id == "34737"
    assert store.name == "Lucky Supermarkets - Mowry"
    assert store.address_line1 == "5000 Mowry Ave"
    assert store.store_number == "714"
    assert "/v3/retailers/542/pickup_locations" in site.paths()
    assert "/stores/714" in site.paths()


async def test_the_price_queries_are_addressed_to_the_shop_not_to_the_store(clients) -> None:
    """The two ids are not interchangeable: `34737` is a supermarket and `24854` is the way
    this ZIP buys from it. Asking the storefront about the supermarket would find nothing."""
    site = whole_site()
    adapter = lucky_adapter(clients, site)
    store = (await adapter.find_stores("94538"))[0]
    site.requests.clear()

    await adapter.search_products("eggs", store)

    variables = [
        r.url.params.get("variables", "") for r in site.requests if r.url.path == "/graphql"
    ]
    assert variables, "the search must have reached the storefront"
    assert all('"shopId":"24854"' in v for v in variables)
    assert not any('"shopId":"34737"' in v for v in variables)


async def test_the_store_page_is_read_once_a_run_not_once_a_caller(clients) -> None:
    """`find_stores` needs the name and `fetch_store_details` needs the hours; both are on
    the same record, so the details phase costs no request of its own."""
    site = whole_site()
    adapter = lucky_adapter(clients, site)
    store = (await adapter.find_stores("94538"))[0]
    assert site.paths().count("/stores/714") == 1

    details = await adapter.fetch_store_details(store)

    assert site.paths().count("/stores/714") == 1, "the record was already read this run"
    assert details is not None and details.hours is not None
    assert details.external_id == "34737"
    assert details.phone == "+15107441660"


async def test_a_picker_that_fails_costs_the_address_and_not_the_prices(clients) -> None:
    site = FakeBanner(picker=None, store=load("lucky", "store_714.json"))
    adapter = lucky_adapter(clients, site)

    stores = await adapter.find_stores("94538")

    assert len(stores) == 1, "the ZIP still has a store to price"
    assert stores[0].external_id == "34737", "the storefront named the location itself"
    assert stores[0].store_number is None
    assert "/stores/714" not in site.paths(), "with no number there is nothing to ask for"


async def test_a_banner_site_that_fails_leaves_the_pickers_answer_standing(clients) -> None:
    site = FakeBanner(picker=load("lucky", "pickup_locations_94538.json"), store=None)
    adapter = lucky_adapter(clients, site)

    stores = await adapter.find_stores("94538")

    assert stores[0].address_line1 == "5000 Mowry Ave"
    assert stores[0].name == "Lucky Supermarkets - 714"
    assert stores[0].store_number == "714"


async def test_a_store_the_banner_no_longer_serves_yields_no_details(clients) -> None:
    site = FakeBanner(
        picker=load("lucky", "pickup_locations_94538.json"), store=None, store_status=404
    )
    adapter = lucky_adapter(clients, site)

    store = StoreLocation(external_id="34737", name="Lucky Supermarkets - 714", store_number="714")

    assert await adapter.fetch_store_details(store) is None


async def test_a_store_with_no_number_is_never_asked_about(clients) -> None:
    site = whole_site()
    adapter = lucky_adapter(clients, site)

    store = StoreLocation(external_id="34737", name="Lucky Supermarkets 34737")

    assert await adapter.fetch_store_details(store) is None
    assert site.requests == []


async def test_no_shop_means_no_store_and_no_further_requests(clients) -> None:
    site = FakeBanner(picker=None, store=None)
    site.handler = lambda request: (  # type: ignore[method-assign]
        site.requests.append(request)
        or (
            httpx.Response(
                200,
                text="<html></html>",
                headers={"set-cookie": "__Host-instacart_sid=x; Path=/"},
            )
            if request.url.path.endswith("/storefront")
            else httpx.Response(200, json={"data": {"defaultShop": None}})
        )
    )
    adapter = lucky_adapter(clients, site)

    assert await adapter.find_stores("99999") == []
    assert not any(p.endswith("/pickup_locations") for p in site.paths())


def test_both_banners_point_at_their_own_website(clients) -> None:
    """The prices and the store information are on two different sites, per banner."""
    lucky, savemart = LuckyAdapter(clients), SaveMartAdapter(clients)

    assert lucky.banner.store_site == "https://luckysupermarkets.com"
    assert lucky.banner.site_url == "https://shop.luckysupermarkets.com"
    assert savemart.banner.store_site == "https://savemart.com"
    assert savemart.banner.site_url == "https://shop.savemart.com"
    assert savemart.banner.store_page_url("781") == "https://savemart.com/stores/781"


def test_a_pickup_location_stands_alone_as_a_record() -> None:
    """It is the fallback `store_from_shop` reaches for, so it must be constructible empty."""
    assert PickupLocation(location_id="34737").store_number is None
