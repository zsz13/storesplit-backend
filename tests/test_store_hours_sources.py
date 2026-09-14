"""Where each retailer's opening hours come from, and what happens where none exist.

Five retailers publish hours on a surface StoreSplit is allowed to read, and each publishes
them in a different shape:

* **Whole Foods** -- absolute UTC windows per date on its store page (`test_store_details.py`)
* **Target** -- fourteen dated days of local wall clock on `/sl/<slug>/<id>`, plus the IANA
  zone and its own Google listing
* **Safeway** -- a real weekly pattern with dated holiday exceptions, in the Yext profile its
  store page hands to its own JavaScript, plus a Google place id and the coordinates the
  store resolver never gave
* **Smart & Final** -- one English sentence per store in the directory the scrape already
  downloads, beside an IANA zone
* **Sprouts** -- one `open_time`/`close_time` window and an IANA zone on its own
  WordPress site, keyed on the store number Instacart publishes as `location_code`

The rest publish nothing readable, and those stores keep "Hours not published". Every fixture
here is a real capture, trimmed to the record the parser reads.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.normalize.hours import hours_today
from app.retailers.base import StoreLocation
from app.retailers.safeway.adapter import parse_store_details as parse_safeway
from app.retailers.smartandfinal.adapter import (
    parse_opening_hours,
    store_details_from_record,
)
from app.retailers.sprouts.adapter import SproutsAdapter, parse_shops
from app.retailers.sprouts.adapter import parse_store_hours as parse_sprouts_hours
from app.retailers.sprouts.adapter import store_details_from_record as parse_sprouts
from app.retailers.sprouts.adapter import store_number as sprouts_store_number
from app.retailers.target.adapter import parse_store_details as parse_target
from app.retailers.target.adapter import store_page_url
from app.services.maps import place_from_retailer

FIXTURES = Path(__file__).parent / "fixtures"
PACIFIC_NOON = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)  # 12:00 in America/Los_Angeles


# --------------------------------------------------------------------------------- Target


def _target_page() -> str:
    return (FIXTURES / "target" / "store_page_3264.html").read_text()


def test_target_publishes_address_coordinates_and_a_zone_on_its_own_store_page() -> None:
    details = parse_target(_target_page())

    assert details is not None
    assert details.external_id == "3264"
    assert details.name == "Target San Francisco Stonestown"
    assert details.address_line1 == "285 Winston Dr"
    assert (details.city, details.state, details.zip_code) == ("San Francisco", "CA", "94132")
    assert (details.latitude, details.longitude) == (37.726614, -122.476464)
    assert details.source == "target:sl-page/store"


def test_target_hours_are_read_as_a_week_that_generalises_carefully() -> None:
    """Fourteen dated days, so every weekday appears twice and a one-off closure cannot
    become that weekday's standing hours."""
    hours = parse_target(_target_page()).hours

    assert hours is not None
    assert hours.timezone == "America/Los_Angeles"
    assert len(hours.dates) == 14, "every published day is kept against its own date"
    assert sorted(hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert {(w.opens, w.closes) for w in hours.weekly.values()} == {("08:00", "22:00")}
    assert hours_today(hours, PACIFIC_NOON).state == "open"
    assert hours_today(hours, PACIFIC_NOON).closes_at == "22:00"


def test_target_counter_hours_are_not_the_stores_hours() -> None:
    """The page publishes `capability_hours` for the Starbucks inside the shop. A shopper
    comparing grocery prices is asking when the shop is open, not when the cafe is."""
    record = json.loads(
        _target_page().split('{\\"store\\":', 1)[1].rsplit("}]", 1)[0].replace('\\"', '"')
    )
    capability = record["rolling_operating_hours"]["capability_hours"]
    assert capability, "the fixture keeps one counter, so this test can mean something"

    hours = parse_target(_target_page()).hours
    assert hours is not None
    assert {(w.opens, w.closes) for w in hours.weekly.values()} == {("08:00", "22:00")}


def test_target_hands_over_its_own_google_listing() -> None:
    """A place identified by the business itself -- no resolving, no guessing."""
    details = parse_target(_target_page())

    place = place_from_retailer(details.maps_place_url, source=details.source)
    assert place is not None
    assert place.url == "https://maps.google.com/maps?cid=10195751074682041949"
    assert place.place_id == "10195751074682041949"


def test_the_target_store_page_url_is_built_from_the_id_it_is_served_by() -> None:
    assert store_page_url("3264", "San Francisco Stonestown") == (
        "https://www.target.com/sl/san-francisco-stonestown/3264"
    )
    assert store_page_url("3264", None).endswith("/store/3264")


def test_a_page_that_is_not_a_target_store_page_yields_nothing() -> None:
    assert parse_target("<html><body>no store here</body></html>") is None
    assert parse_target("") is None


# -------------------------------------------------------------------------------- Safeway


def _safeway_page() -> str:
    return (FIXTURES / "safeway" / "store_page_4601.html").read_text()


def test_safeway_publishes_a_weekly_pattern_and_dated_holidays() -> None:
    details = parse_safeway(_safeway_page())

    assert details is not None and details.external_id == "4601"
    hours = details.hours
    assert hours is not None and hours.timezone == "America/Los_Angeles"
    assert sorted(hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert {(w.opens, w.closes) for w in hours.weekly.values()} == {("06:00", "23:00")}
    assert hours.dates == {"2026-09-07": hours.weekly[0]}, "a published holiday keeps its date"
    assert hours_today(hours, PACIFIC_NOON).state == "open"


def test_safeway_store_page_supplies_the_coordinates_its_locator_never_did() -> None:
    """Safeway's store resolver publishes no coordinates at all, which is why its stores were
    placed at their ZIP's centroid. The store page has them, so the store gets a real point."""
    details = parse_safeway(_safeway_page())

    assert details is not None
    assert details.latitude == pytest.approx(37.7969, abs=0.001)
    assert details.longitude == pytest.approx(-122.3986, abs=0.001)
    assert details.address_line1 == "145 Jackson St"


def test_safeway_hands_over_a_google_place_id() -> None:
    details = parse_safeway(_safeway_page())

    place = place_from_retailer(details.maps_place_url, source=details.source)
    assert place is not None
    assert place.place_id == "ChIJKfpyiGCAhYARNyu9cCeqEbk"
    assert place.url.startswith("https://www.google.com/maps/search/?api=1")


def test_a_page_without_a_yext_profile_yields_nothing() -> None:
    assert parse_safeway("<html><body>Safeway</body></html>") is None
    assert parse_safeway("<script>Yext.Profile = {not json</script>") is None


# -------------------------------------------------------------------------- Smart & Final


def _smartandfinal_records() -> list[dict]:
    return json.loads((FIXTURES / "smartandfinal" / "stores_hours.json").read_text())["items"]


def test_smartandfinal_hours_come_from_the_directory_the_scrape_already_read() -> None:
    for record in _smartandfinal_records():
        details = store_details_from_record(record)
        assert details is not None, record.get("retailerStoreId")
        assert details.hours is not None, record.get("openingHours")
        assert details.hours.timezone in {"America/Los_Angeles", "America/Phoenix"}
        assert sorted(details.hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
        assert details.source == "smartandfinal:api/stores"


@pytest.mark.parametrize(
    ("sentence", "opens", "closes"),
    [
        ("Sunday-Saturday: 6 AM - 10 PM", "06:00", "22:00"),
        ("Sunday-Saturday: 7 AM - 9 PM", "07:00", "21:00"),
        ("Monday-Sunday: 6:30 AM - 11:45 PM", "06:30", "23:45"),
        ("Sunday-Saturday: 12 AM - 12 PM", "00:00", "12:00"),
    ],
)
def test_a_recognised_sentence_becomes_the_whole_week(
    sentence: str, opens: str, closes: str
) -> None:
    hours = parse_opening_hours(sentence, "America/Los_Angeles")

    assert hours is not None
    assert sorted(hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert {(w.opens, w.closes) for w in hours.weekly.values()} == {(opens, closes)}


def test_a_sentence_with_different_days_keeps_them_different() -> None:
    hours = parse_opening_hours(
        "Mon-Fri: 7 AM - 9 PM, Sat: 8 AM - 8 PM, Sun: 9 AM - 7 PM", "America/Los_Angeles"
    )

    assert hours is not None
    assert (hours.weekly[0].opens, hours.weekly[0].closes) == ("07:00", "21:00")
    assert (hours.weekly[5].opens, hours.weekly[5].closes) == ("08:00", "20:00")
    assert (hours.weekly[6].opens, hours.weekly[6].closes) == ("09:00", "19:00")


@pytest.mark.parametrize(
    "sentence",
    [
        "Open 24 hours",
        "Sunday-Saturday: sunrise to sunset",
        "Call for hours",
        "Mon: 6 AM",
        "Fnord-Blursday: 6 AM - 10 PM",
        "",
        None,
    ],
)
def test_a_sentence_nobody_can_read_produces_no_hours_at_all(sentence: str | None) -> None:
    """Half a week presented as a whole one is worse than admitting the hours are unknown."""
    assert parse_opening_hours(sentence, "America/Los_Angeles") is None


def test_hours_without_a_timezone_are_not_hours() -> None:
    """A wall clock with no zone is not a fact about a store; it is eight hours of guesswork."""
    assert parse_opening_hours("Sunday-Saturday: 6 AM - 10 PM", None) is None
    assert parse_opening_hours("Sunday-Saturday: 6 AM - 10 PM", "") is None


def test_a_record_without_a_store_id_is_not_a_store() -> None:
    assert store_details_from_record({}) is None
    assert store_details_from_record({"openingHours": "Sunday-Saturday: 6 AM - 10 PM"}) is None


# --------------------------------------------------------------------------------- Sprouts


def _sprouts_record() -> dict:
    return json.loads((FIXTURES / "sprouts" / "store_276.json").read_text())


def test_sprouts_hours_come_from_its_own_site_not_the_storefront() -> None:
    """The Instacart storefront states no hours; `www.sprouts.com` states hours *and* a zone.

    This is the whole reason Sprouts has hours where Trader Joe's does not: both publish a
    wall clock, only one publishes the timezone it is kept in.
    """
    details = parse_sprouts(_sprouts_record(), "601")

    assert details is not None
    assert details.external_id == "601"  # the shop asked about, not Sprouts' own store id
    assert details.source == "sprouts:wp-json/store"
    assert details.hours is not None
    assert details.hours.timezone == "America/Los_Angeles"
    assert sorted(details.hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert {(w.opens, w.closes) for w in details.hours.weekly.values()} == {("07:00", "22:00")}


def test_the_daly_city_store_reads_open_until_ten_at_noon() -> None:
    """The sentence the shopper sees, decided in the store's own timezone."""
    details = parse_sprouts(_sprouts_record(), "601")
    assert details is not None

    today = hours_today(details.hours, PACIFIC_NOON)

    assert today.state == "open"
    assert today.closes_at == "22:00"


def test_sprouts_publishes_the_address_and_coordinates_the_storefront_withholds() -> None:
    """`parse_shops` can only set `latitude=None`; this record is where a Sprouts store
    stops being placed by the centroid of its ZIP."""
    details = parse_sprouts(_sprouts_record(), "601")

    assert details is not None
    assert details.address_line1 == "301 Gellert Blvd."
    assert (details.city, details.state, details.zip_code) == ("Daly City", "CA", "94015")
    assert details.latitude == pytest.approx(37.668844)
    assert details.longitude == pytest.approx(-122.466955)


def test_the_store_name_is_left_alone() -> None:
    """Sprouts calls this store "Daly City". The row says which retailer and which number,
    and overwriting it would lose both -- including the number this lookup is keyed on."""
    details = parse_sprouts(_sprouts_record(), "601")

    assert details is not None
    assert details.name is None


def test_a_store_number_nobody_serves_answers_200_with_nulls() -> None:
    """Sprouts does not 404 an unknown store; it returns `success: true` and an empty record,
    so the parser has to read the record rather than the status code."""
    assert parse_sprouts({"success": True, "data": {"store_id": None, "name": None}}, "601") is None
    assert parse_sprouts({"success": True, "data": {}}, "601") is None
    assert parse_sprouts({}, "601") is None
    assert parse_sprouts([], "601") is None


def test_the_store_number_is_read_from_the_name_instacart_published() -> None:
    """`external_id` is the Instacart shop id -- 601 and 357771 are the same Daly City store
    in two fulfilment modes -- and means nothing to Sprouts' own site. The store number does."""
    named = StoreLocation(external_id="601", name="Sprouts Farmers Market Daly City (Store #276)")
    assert sprouts_store_number(named) == "276"

    unnumbered = StoreLocation(external_id="601", name="Sprouts Farmers Market Daly City")
    assert sprouts_store_number(unnumbered) is None


@pytest.mark.parametrize(
    ("opens", "closes"),
    [("7:00AM", None), (None, "10:00PM"), ("", ""), ("sunrise", "sunset"), ("25:00AM", "10:00PM")],
)
def test_an_unreadable_window_produces_no_hours(opens: str | None, closes: str | None) -> None:
    assert parse_sprouts_hours(opens, closes, "America/Los_Angeles") is None


def test_sprouts_hours_without_a_timezone_are_not_hours() -> None:
    assert parse_sprouts_hours("7:00AM", "10:00PM", None) is None
    assert parse_sprouts_hours("7:00AM", "10:00PM", "") is None


def _sprouts_adapter(handler, clients) -> tuple[SproutsAdapter, list[httpx.Request]]:
    """A Sprouts adapter whose details host is a mock transport. The storefront client is
    left alone: nothing here may touch it, and a request that did would fail loudly."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    adapter = SproutsAdapter(clients)
    adapter._details_client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return adapter, seen


async def test_the_details_request_goes_to_the_store_number_on_sprouts_own_host(
    clients,
) -> None:
    """The whole join, over HTTP: the locator's `location_code` becomes the path segment."""
    payload = _sprouts_record()
    adapter, seen = _sprouts_adapter(lambda _r: httpx.Response(200, json=payload), clients)
    store = StoreLocation(external_id="601", name="Sprouts Daly City", store_number="276")

    details = await adapter.fetch_store_details(store)

    assert [str(r.url) for r in seen] == [
        "https://www.sprouts.com/wp-json/spr-wp-rest/v1/store/276"
    ]
    assert details is not None
    assert details.hours is not None and details.hours.timezone == "America/Los_Angeles"


async def test_a_store_with_no_number_costs_no_request_at_all(clients) -> None:
    """`external_id` is the Instacart shop id; asking Sprouts about it would be a wrong
    lookup, not a missing one, so the adapter does not ask."""
    adapter, seen = _sprouts_adapter(
        lambda _r: httpx.Response(200, json=_sprouts_record()), clients
    )
    store = StoreLocation(external_id="601", name="Sprouts Farmers Market Daly City")

    assert await adapter.fetch_store_details(store) is None
    assert seen == []


async def test_the_number_is_taken_from_the_locator_not_the_name(clients) -> None:
    """When both are present the carried value wins: a display label is not a key."""
    adapter, seen = _sprouts_adapter(
        lambda _r: httpx.Response(200, json=_sprouts_record()), clients
    )
    store = StoreLocation(
        external_id="601", name="Sprouts Farmers Market Daly City (Store #999)", store_number="276"
    )

    await adapter.fetch_store_details(store)

    assert seen[0].url.path.endswith("/276")


@pytest.mark.parametrize("status", [404, 500])
async def test_a_failing_store_page_raises_rather_than_inventing_hours(
    status: int, clients
) -> None:
    """The scrape's details phase catches this and the store keeps "Hours not published".
    What must never happen is a `StoreDetails` with no source behind it."""
    adapter, _seen = _sprouts_adapter(lambda _r: httpx.Response(status, text="nope"), clients)
    store = StoreLocation(external_id="601", name="Sprouts", store_number="276")

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.fetch_store_details(store)


async def test_a_store_number_nobody_serves_yields_nothing_from_a_200(clients) -> None:
    empty = {"success": True, "data": {"store_id": None, "name": None, "timezone": None}}
    adapter, _seen = _sprouts_adapter(lambda _r: httpx.Response(200, json=empty), clients)
    store = StoreLocation(external_id="601", name="Sprouts", store_number="30")

    assert await adapter.fetch_store_details(store) is None


def test_the_locator_carries_the_store_number_so_the_name_never_has_to() -> None:
    """`parse_shops` groups shops by `location_code` already; this is that key, kept."""
    shops = json.loads((FIXTURES / "sprouts" / "idp_shops_94110.json").read_text())

    stores = parse_shops(shops)

    assert stores, "the fixture should yield shops"
    assert all(s.store_number for s in stores), "every shop's store number is carried"
    daly = next(s for s in stores if "Daly City" in s.name)
    assert daly.store_number == "276"
    assert daly.external_id != daly.store_number, "the shop id is not the store number"
