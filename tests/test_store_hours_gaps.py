"""The three retailers that used to say "Hours not published", and why each one did.

They were one symptom with three causes, and the fixes are correspondingly different:

* **99 Ranch** published everything all along -- an IANA `timeZone` and the shop's own week
  -- in the very payload `find_stores` reads. Nothing was missing but a parser.
* **Trader Joe's** publishes a complete week per store and **no timezone anywhere**. A wall
  clock with no zone is not a fact about a store, so its week is carried as `UnzonedHours`
  and becomes a schedule only once a zone is established some other way.
* **Raley's** publishes nothing on any surface `robots.txt` allows: its store page carries
  interface strings and no store record, and its details live behind the disallowed `/api`.
  A Google place that has already been *verified* as this store is the only source left.

Every fixture here is a real capture, trimmed to the record the parser reads.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.db.models import Store
from app.normalize.categories import CATEGORIES
from app.normalize.hours import (
    DayHours,
    StoreHours,
    UnzonedHours,
    hours_from_unzoned,
    hours_today,
    store_hours_to_json,
)
from app.normalize.timezones import timezone_at
from app.retailers.base import StoreDetails, StoreLocation
from app.retailers.ranch99.adapter import parse_business_hours, store_details_from_record
from app.retailers.traderjoes.adapter import parse_locator_hours, parse_locator_results
from app.services.maps import (
    PLACE_HOURS_MASK,
    PLACE_ZONE_MASK,
    PlaceSchedule,
    schedule_from_place,
)
from app.services.scraper import (
    _derivable_zone,
    apply_store_details,
    fetch_retailer,
    ingest_retailer,
)
from sqlalchemy import select

from tests.fakes import FakeAdapter

FIXTURES = Path(__file__).parent / "fixtures"
PACIFIC_NOON = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)  # 12:00 in America/Los_Angeles
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _ranch99_records() -> list[dict]:
    payload = json.loads((FIXTURES / "ranch99" / "stores_94110.json").read_text())
    return payload["data"]["records"]


def _traderjoes_payload() -> dict:
    return json.loads((FIXTURES / "traderjoes" / "locator_94110.json").read_text())


def _traderjoes_entries() -> list[dict]:
    return _traderjoes_payload()["response"]["collection"]


# -------------------------------------------------------------------------------- 99 Ranch


def test_ranch99_publishes_a_zone_and_a_week_in_the_store_lookup() -> None:
    """The record `find_stores` already read carries both, so hours cost no extra request."""
    details = store_details_from_record(_ranch99_records()[0], "1769")

    assert details is not None
    assert details.hours is not None
    assert details.hours.timezone == "America/Los_Angeles"
    assert sorted(details.hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert details.source == "ranch99:be-api/nearby-stores"


def test_ranch99_day_ranges_become_the_days_they_cover() -> None:
    """ "Monday - Thursday" and "Friday - Sunday" are one store's whole week, in two rows."""
    hours = parse_business_hours(
        [
            {"dayOfWeeks": "Friday - Sunday", "startTime": "08:00", "endTime": "22:00"},
            {"dayOfWeeks": "Monday - Thursday", "startTime": "08:00", "endTime": "21:00"},
        ],
        "America/Los_Angeles",
    )

    assert hours is not None
    assert [hours.weekly[d].closes for d in range(7)] == [
        "21:00",  # Monday
        "21:00",
        "21:00",
        "21:00",  # Thursday
        "22:00",  # Friday
        "22:00",
        "22:00",  # Sunday
    ]


def test_ranch99_uses_the_shops_hours_and_not_its_delivery_window() -> None:
    """`onlineBusinessTimes` is when it delivers. A shopper at the door is asking about the
    door, and the two genuinely differ: Richmond delivers until 22:00 all week and shuts its
    doors at 21:00 from Monday to Thursday."""
    record = next(r for r in _ranch99_records() if str(r["storeNumber"]) == "1769")
    details = store_details_from_record(record, "1769")

    assert record["onlineBusinessTimes"] != record["offlineBusinessTimes"], "fixture premise"
    assert details is not None and details.hours is not None
    monday = details.hours.weekly[0]
    assert (monday.opens, monday.closes) == ("08:00", "21:00")


def test_ranch99_unreadable_entries_lose_their_days_rather_than_guess() -> None:
    hours = parse_business_hours(
        [
            {"dayOfWeeks": "Monday - Tuesday", "startTime": "08:00", "endTime": "22:00"},
            {"dayOfWeeks": "whenever we feel like it", "startTime": "08:00", "endTime": "22:00"},
            {"dayOfWeeks": "Friday", "startTime": "early", "endTime": "late"},
        ],
        "America/Los_Angeles",
    )

    assert hours is not None
    assert sorted(hours.weekly) == [0, 1], "only the days that could be read"


# ---------------------------------------------------------------------------- Trader Joe's


def test_traderjoes_publishes_a_full_week_and_no_zone_to_read_it_in() -> None:
    entry = _traderjoes_entries()[0]

    assert entry["monday_open"] == "09:00", "fixture premise: the week is published"
    assert not any("zone" in key.lower() for key in entry), "and no timezone is, anywhere"


def test_traderjoes_week_is_unzoned_and_is_not_a_schedule_on_its_own() -> None:
    unzoned = parse_locator_hours(_traderjoes_entries()[0])

    assert unzoned is not None
    assert sorted(unzoned.weekly) == [0, 1, 2, 3, 4, 5, 6]
    assert unzoned.weekly[0] == DayHours("09:00", "21:00")
    # The whole point of the type: without a zone there is no schedule and no answer.
    assert hours_from_unzoned(unzoned, None) is None
    assert hours_today(hours_from_unzoned(unzoned, None), PACIFIC_NOON).state == "unknown"


def test_traderjoes_week_becomes_a_schedule_once_a_zone_is_known() -> None:
    hours = hours_from_unzoned(parse_locator_hours(_traderjoes_entries()[0]), "America/New_York")

    assert hours is not None
    assert hours.timezone == "America/New_York"
    # 19:00Z is 15:00 in New York: inside 09:00-21:00, so open there.
    assert hours_today(hours, PACIFIC_NOON).state == "open"


def test_traderjoes_week_becomes_a_schedule_from_its_own_locator_record() -> None:
    """Both of the San Francisco stores, end to end from the captured payload.

    The week and the coordinates that date it come out of the *same* locator record, so the
    thing that used to be missing is now read from the thing that was never missing. Nobody
    is asked about either: no Google key, no network, no row in a database.
    """
    entries = {entry["clientkey"]: entry for entry in _traderjoes_entries()}
    located = {store.external_id: store for store in parse_locator_results(_traderjoes_payload())}

    for store_number in ("78", "225"):
        store = located[store_number]
        hours = hours_from_unzoned(
            parse_locator_hours(entries[store_number]),
            timezone_at(store.latitude, store.longitude),
        )

        assert hours is not None, f"store {store_number} still has no schedule"
        assert hours.timezone == "America/Los_Angeles"
        assert sorted(hours.weekly) == [0, 1, 2, 3, 4, 5, 6], "the whole week, not just today"
        assert all(window == DayHours("09:00", "21:00") for window in hours.weekly.values())
        # 12:00 in San Francisco on a Thursday, inside 09:00-21:00.
        assert hours_today(hours, PACIFIC_NOON).state == "open"


def test_traderjoes_each_weekday_lands_on_its_own_day() -> None:
    """Every real Trader Joe's store keeps the same hours all seven days, so the captured
    fixture cannot tell a correct weekday mapping from a scrambled one: swap two entries in
    `WEEKDAY_INDEX` and every other assertion in this file still passes. This is the only
    test that would notice, so it states a week no two days of which are alike.

    The requirement is "the full weekly schedule, not just today's text" -- a week read into
    the wrong days is exactly as wrong as no week, and reads as correct six days out of seven.
    """
    entry = {
        "monday_open": "08:00",
        "monday_close": "20:00",
        "tuesday_open": "08:15",
        "tuesday_close": "20:15",
        "wednesday_open": "08:30",
        "wednesday_close": "20:30",
        "thursday_open": "08:45",
        "thursday_close": "20:45",
        "friday_open": "09:00",
        "friday_close": "21:00",
        "saturday_open": "09:15",
        "saturday_close": "21:15",
        "sunday_open": "09:30",
        "sunday_close": "21:30",
    }

    unzoned = parse_locator_hours(entry)

    assert unzoned is not None
    # 0 = Monday, matching `date.weekday()`, through to 6 = Sunday.
    assert unzoned.weekly == {
        0: DayHours("08:00", "20:00"),
        1: DayHours("08:15", "20:15"),
        2: DayHours("08:30", "20:30"),
        3: DayHours("08:45", "20:45"),
        4: DayHours("09:00", "21:00"),
        5: DayHours("09:15", "21:15"),
        6: DayHours("09:30", "21:30"),
    }


def test_traderjoes_sunday_is_read_on_a_sunday_and_not_on_monday() -> None:
    """The same mapping proven through the reader a shopper's answer comes from, so an
    off-by-one between `WEEKDAY_INDEX` and `date.weekday()` cannot hide behind the parser."""
    hours = hours_from_unzoned(
        parse_locator_hours(
            {
                "sunday_open": "10:00",
                "sunday_close": "18:00",
                "monday_open": "07:00",
                "monday_close": "23:00",
            }
        ),
        "America/Los_Angeles",
    )

    # 2026-09-13 is a Sunday; 19:00Z is 12:00 in California on both days.
    sunday = hours_today(hours, datetime(2026, 9, 13, 19, 0, tzinfo=UTC))
    monday = hours_today(hours, datetime(2026, 9, 14, 19, 0, tzinfo=UTC))

    assert (sunday.state, sunday.closes_at) == ("open", "18:00")
    assert (monday.state, monday.closes_at) == ("open", "23:00")


def test_traderjoes_free_text_holiday_notes_are_not_parsed() -> None:
    """`holidayhours` and `Temp Hours Note` are prose written for people. Half a week read
    wrongly is worse than a week nobody claimed."""
    unzoned = parse_locator_hours(
        {"monday_open": "09:00", "monday_close": "21:00", "holidayhours": "Closing at 5pm 12/24"}
    )
    hours = hours_from_unzoned(unzoned, "America/Los_Angeles")

    assert hours is not None
    assert hours.dates == {}, "a dated exception is never guessed from prose"


def test_traderjoes_store_with_no_published_clock_yields_nothing() -> None:
    coming_soon = next(e for e in _traderjoes_entries() if e.get("Coming Soon") == "Yes")

    assert parse_locator_hours(coming_soon) is None


# ------------------------------------------------------------------- a verified Google place


def test_google_periods_become_a_week_with_sunday_renumbered() -> None:
    """Google numbers weekdays from Sunday; `date.weekday()` numbers them from Monday."""
    schedule = schedule_from_place(
        {
            "timeZone": {"id": "America/Los_Angeles"},
            "regularOpeningHours": {
                "periods": [
                    {
                        "open": {"day": 0, "hour": 7, "minute": 0},
                        "close": {"day": 0, "hour": 22, "minute": 0},
                    },
                    {
                        "open": {"day": 1, "hour": 6, "minute": 0},
                        "close": {"day": 1, "hour": 23, "minute": 0},
                    },
                ]
            },
        }
    )

    assert schedule is not None
    assert schedule.timezone == "America/Los_Angeles"
    assert schedule.hours is not None
    assert schedule.hours.weekly[6] == DayHours("07:00", "22:00"), "Google's day 0 is Sunday"
    assert schedule.hours.weekly[0] == DayHours("06:00", "23:00"), "Google's day 1 is Monday"


def test_google_period_with_no_close_is_open_all_day() -> None:
    """A place that never shuts is published as an `open` with no `close` at all."""
    schedule = schedule_from_place(
        {
            "timeZone": {"id": "America/Los_Angeles"},
            "regularOpeningHours": {"periods": [{"open": {"day": 3, "hour": 0, "minute": 0}}]},
        }
    )

    assert schedule is not None and schedule.hours is not None
    assert schedule.hours.weekly[2] == DayHours("00:00", "00:00")
    # Midnight to midnight is a day with no shut moment in it, wherever the clock stands.
    hours = hours_from_unzoned(schedule.hours, schedule.timezone)
    assert hours_today(hours, datetime(2026, 9, 9, 23, 30, tzinfo=UTC)).state == "open"


def test_google_overnight_period_stays_attached_to_the_evening_it_opens_on() -> None:
    schedule = schedule_from_place(
        {
            "timeZone": {"id": "America/Los_Angeles"},
            "regularOpeningHours": {
                "periods": [
                    {
                        "open": {"day": 5, "hour": 20, "minute": 0},
                        "close": {"day": 6, "hour": 2, "minute": 0},
                    }
                ]
            },
        }
    )

    assert schedule is not None and schedule.hours is not None
    assert sorted(schedule.hours.weekly) == [4], "Friday, the day it opens, not Saturday too"
    assert schedule.hours.weekly[4] == DayHours("20:00", "02:00")


def test_a_place_that_never_closes_is_open_every_day_not_just_sunday() -> None:
    """Google publishes an always-open place as **one** period: day 0, 00:00, no close. It
    is a statement about the week, and filing it under Sunday is how a 24-hour shop came out
    as "Closed - Opens 12:00 AM Sunday" for the other six days."""
    schedule = schedule_from_place(
        {
            "timeZone": {"id": "America/Los_Angeles"},
            "regularOpeningHours": {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]},
        }
    )

    assert schedule is not None and schedule.hours is not None
    assert sorted(schedule.hours.weekly) == [0, 1, 2, 3, 4, 5, 6]
    hours = hours_from_unzoned(schedule.hours, schedule.timezone)
    # Wednesday lunchtime, four days from the Sunday the payload names.
    assert hours_today(hours, datetime(2026, 9, 9, 19, 0, tzinfo=UTC)).state == "open"


def test_google_split_hours_drop_the_day_rather_than_merge_it() -> None:
    """Two periods in one day is a lunchtime closure, and `DayHours` holds one window.
    Merging them would claim the shop is open across the gap, which is worse than unknown."""
    schedule = schedule_from_place(
        {
            "timeZone": {"id": "America/Los_Angeles"},
            "regularOpeningHours": {
                "periods": [
                    {
                        "open": {"day": 1, "hour": 8, "minute": 0},
                        "close": {"day": 1, "hour": 12, "minute": 0},
                    },
                    {
                        "open": {"day": 1, "hour": 14, "minute": 0},
                        "close": {"day": 1, "hour": 18, "minute": 0},
                    },
                    {
                        "open": {"day": 2, "hour": 8, "minute": 0},
                        "close": {"day": 2, "hour": 18, "minute": 0},
                    },
                ]
            },
        }
    )

    assert schedule is not None and schedule.hours is not None
    assert sorted(schedule.hours.weekly) == [1], "Monday is dropped; Tuesday survives"


def test_google_zone_alone_is_a_usable_answer() -> None:
    """What Trader Joe's needs, and the cheaper of the two Places SKUs."""
    schedule = schedule_from_place({"timeZone": {"id": "America/Los_Angeles"}})

    assert schedule is not None
    assert schedule.timezone == "America/Los_Angeles"
    assert schedule.hours is None


def test_google_record_stating_neither_is_no_answer() -> None:
    assert schedule_from_place({}) is None
    assert schedule_from_place("not a record") is None


def test_hours_are_the_dearer_field_mask_and_a_zone_is_the_cheaper_one() -> None:
    """`regularOpeningHours` is a Places Details Enterprise field and `timeZone` a Pro one, so
    a store that already published its own week must never be billed for hours again."""
    assert PLACE_ZONE_MASK == "timeZone"
    assert "regularOpeningHours" in PLACE_HOURS_MASK
    assert "regularOpeningHours" not in PLACE_ZONE_MASK


def test_unzoned_hours_carry_no_zone_of_their_own() -> None:
    """The type exists to make "a wall clock with no zone" unstorable as a schedule."""
    assert not hasattr(UnzonedHours(weekly={}), "timezone")


# ------------------------------------------------------ the ladder, when the row is written


def _store() -> Store:
    return Store(id=1, external_id="1", name="Somewhere", hours=None, timezone=None)


def test_a_retailers_own_zoned_week_wins_outright() -> None:
    """Google is never consulted about a store whose retailer stated its own hours, so a
    schedule offered alongside one must not displace it."""
    store = _store()
    published = StoreHours("America/Los_Angeles", {0: DayHours("08:00", "22:00")}, {})

    apply_store_details(
        store,
        StoreDetails(external_id="1", hours=published, source="ranch99:be-api/nearby-stores"),
        NOW,
        schedule=PlaceSchedule("America/New_York", UnzonedHours({0: DayHours("06:00", "23:00")})),
    )

    assert store.hours == store_hours_to_json(published)
    assert store.timezone == "America/Los_Angeles"
    assert store.hours_source == "ranch99:be-api/nearby-stores"


def test_a_retailers_unzoned_week_is_read_in_a_zone_google_supplies() -> None:
    """Trader Joe's, exactly. The clock stays the retailer's; only the frame is borrowed,
    so the source recorded is still Trader Joe's own."""
    store = _store()

    apply_store_details(
        store,
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
        schedule=PlaceSchedule("America/Los_Angeles", None),
    )

    assert store.timezone == "America/Los_Angeles"
    assert store.hours == {"weekly": {"0": {"opens": "09:00", "closes": "21:00"}}, "dates": {}}
    assert store.hours_source == "traderjoes:locator"


def test_a_zone_already_on_the_row_is_enough_to_read_an_unzoned_week() -> None:
    """A shop does not move between timezones, so a zone established once keeps working and
    costs no further lookup."""
    store = _store()
    store.timezone = "America/Los_Angeles"

    apply_store_details(
        store,
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
    )

    assert store.hours is not None
    assert store.hours_source == "traderjoes:locator"


def test_an_unzoned_week_is_read_in_the_zone_its_own_coordinates_name() -> None:
    """Trader Joe's San Francisco - 9th St (78), with nobody asked about anything.

    The locator states the week and the coordinates in the same record, and a point lies in
    exactly one timezone -- so the zone this week needs is already implied by the address
    Trader Joe's published. No Google schedule is passed here at all, which is the point:
    before this the same call produced "Hours not published".
    """
    store = _store()

    apply_store_details(
        store,
        StoreDetails(
            external_id="78",
            latitude=37.77111,
            longitude=-122.40738,
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
        schedule=None,
    )

    assert store.timezone == "America/Los_Angeles"
    assert store.hours == {"weekly": {"0": {"opens": "09:00", "closes": "21:00"}}, "dates": {}}
    assert store.hours_source == "traderjoes:locator", "the clock is still Trader Joe's own"


def test_a_derived_zone_never_displaces_one_the_retailer_published() -> None:
    """Coordinates are the bottom of the zone ladder, not the top. A retailer that names the
    zone its own clock is kept in has said something the polygon lookup cannot improve on."""
    store = _store()
    published = StoreHours("America/New_York", {0: DayHours("08:00", "22:00")}, {})

    apply_store_details(
        store,
        # Coordinates in California, hours the retailer itself stamped Eastern -- a store
        # near a zone boundary, or a retailer that keeps one chain-wide clock.
        StoreDetails(
            external_id="1",
            latitude=37.77111,
            longitude=-122.40738,
            hours=published,
            source="safeway:yext/localPage",
        ),
        NOW,
    )

    assert store.timezone == "America/New_York"
    assert store.hours_source == "safeway:yext/localPage"


def test_an_unzoned_week_with_no_coordinates_anywhere_still_says_nothing() -> None:
    """The honest floor is unchanged: no zone from any source means no schedule, not a guess
    read in whatever zone the server happens to keep."""
    store = _store()

    apply_store_details(
        store,
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
    )

    assert store.hours is None
    assert store.timezone is None


def test_coordinates_that_are_not_a_point_on_earth_yield_no_zone() -> None:
    """A retailer's payload is where these numbers come from, so a broken pair must cost the
    hours and nothing else -- never a schedule read in a zone nobody stands in."""
    store = _store()

    apply_store_details(
        store,
        StoreDetails(
            external_id="1",
            latitude=999.0,
            longitude=-122.40738,
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
    )

    assert store.hours is None
    assert store.timezone is None


def test_a_derived_zone_corrects_a_stale_one_already_on_the_row() -> None:
    """The rung order that keeps a bad coordinate from being permanent.

    `apply_store_details` writes every resolved zone back to the row, so a derived zone
    becomes a *cached* derived zone next week. Were the cache consulted first it would
    outrank the source it came from and nothing could ever correct it: one payload with a
    dropped minus sign writes a zone half a world away and the store keeps it for good. The
    coordinates are re-read every pass, so a bad one costs a single scrape.
    """
    store = _store()
    store.timezone = "America/New_York"  # what a previous pass concluded, wrongly

    apply_store_details(
        store,
        StoreDetails(
            external_id="78",
            latitude=37.77111,
            longitude=-122.40738,
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
    )

    assert store.timezone == "America/Los_Angeles", "the coordinates win over the stale cache"


def test_a_zone_google_stated_still_beats_the_coordinates() -> None:
    """Google's zone is an observation about a verified place; the polygon answer is a
    derivation from a number a retailer typed. The observation stays on top."""
    store = _store()

    apply_store_details(
        store,
        StoreDetails(
            external_id="1",
            latitude=37.77111,
            longitude=-122.40738,
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        NOW,
        schedule=PlaceSchedule("America/New_York", None),
    )

    assert store.timezone == "America/New_York"


def test_googles_week_is_never_read_in_a_zone_derived_from_coordinates() -> None:
    """The derived zone exists to stop *first-party* hours being discarded, and is not a
    general-purpose filler. Google is asked for `timeZone` in the same request as
    `regularOpeningHours`, so a reply carrying hours and no zone is a malformed answer rather
    than a gap worth filling -- and inventing a frame for a third party's clock is a wider
    claim than this change is entitled to make."""
    store = _store()

    apply_store_details(
        store,
        StoreDetails(
            external_id="1",
            latitude=37.77111,
            longitude=-122.40738,
            source="raleys:sitemap",
        ),
        NOW,
        schedule=PlaceSchedule(None, UnzonedHours({0: DayHours("06:00", "23:00")})),
    )

    assert store.hours is None, "hours with no stated zone stay unstored"
    assert store.timezone is None


def test_google_fills_the_gap_only_where_the_retailer_published_nothing() -> None:
    """Raley's, exactly: no hours on any surface `robots.txt` allows."""
    store = _store()

    apply_store_details(
        store,
        StoreDetails(external_id="1", source="raleys:sitemap"),
        NOW,
        schedule=PlaceSchedule(
            "America/Los_Angeles", UnzonedHours({0: DayHours("06:00", "23:00")})
        ),
    )

    assert store.timezone == "America/Los_Angeles"
    assert store.hours == {"weekly": {"0": {"opens": "06:00", "closes": "23:00"}}, "dates": {}}
    assert store.hours_source == "google:places/details"


def test_a_store_nobody_can_state_hours_for_keeps_none() -> None:
    store = _store()

    apply_store_details(store, StoreDetails(external_id="1", source="raleys:sitemap"), NOW)

    assert store.hours is None
    assert store.timezone is None
    assert store.hours_updated_at == NOW, "the attempt is still stamped, or it repeats forever"


# ----------------------------------------------- the Google rung, wired through a scrape


def _adapter(
    details: StoreDetails | None,
    place: str | None = None,
    coordinates: tuple[float, float] | None = None,
):
    """A retailer with one store, publishing whatever `details` says and nothing else."""
    latitude, longitude = coordinates or (None, None)
    location = StoreLocation(
        "1",
        "Somewhere Downtown",
        address_line1="1 Market St",
        zip_code="94105",
        latitude=latitude,
        longitude=longitude,
    )
    published = details
    if published is not None and place is not None:
        published = replace(published, maps_place_url=place)
    return location, FakeAdapter("g", [location], {"eggs": {"1": []}}, store_details=published)


async def _scrape(db, adapter, location, **resolvers) -> Store:
    fetch = await fetch_retailer(
        adapter, "94105", [CATEGORIES["eggs"]], max_stores=2, request_limit=4, **resolvers
    )
    await ingest_retailer(db, fetch, "94105")
    await db.commit()
    store = await db.scalar(select(Store).where(Store.external_id == location.external_id))
    assert store is not None
    return store


async def test_google_is_never_asked_without_a_key(db) -> None:
    """The requester's decision: the rung is built and left dark. No key, no request, and
    every store that needed it keeps saying "Hours not published"."""
    location, adapter = _adapter(StoreDetails(external_id="1", source="raleys:sitemap"))

    store = await _scrape(db, adapter, location, schedule_resolver=None)

    assert store.hours is None
    assert store.timezone is None


async def test_google_is_asked_only_about_a_verified_place(db) -> None:
    """A place found by a bare address search never reaches the hours lookup: the hours of
    the business next door are worse than no hours at all."""
    location, adapter = _adapter(StoreDetails(external_id="1", source="raleys:sitemap"))
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return None

    # No place published and no resolver, so nothing was ever verified for this store.
    await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert asked == [], "no verified place, so no lookup"


async def test_google_fills_a_gap_for_a_place_the_retailer_published(db) -> None:
    location, adapter = _adapter(
        StoreDetails(external_id="1", source="raleys:sitemap"),
        place="https://maps.google.com/maps?cid=10195751074682041949",
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/Los_Angeles", UnzonedHours({0: DayHours("06:00", "23:00")}))

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert [want_hours for _, want_hours in asked] == [True], "no week published, so ask for one"
    assert store.hours_source == "google:places/details"
    assert store.timezone == "America/Los_Angeles"


async def test_a_store_that_published_a_week_is_asked_only_for_its_zone(db) -> None:
    """Trader Joe's, every store. `regularOpeningHours` is the dearer Places SKU, and a
    retailer that already stated its own week must never be billed for it."""
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/Los_Angeles", None)

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert [want_hours for _, want_hours in asked] == [False], "the cheaper mask"
    assert store.timezone == "America/Los_Angeles"
    assert store.hours_source == "traderjoes:locator", "the clock is still the retailer's"


async def test_a_store_with_its_own_zoned_week_is_never_asked_at_all(db) -> None:
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            hours=StoreHours("America/Los_Angeles", {0: DayHours("06:00", "22:00")}, {}),
            source="safeway:local-page/yext-profile",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
    )
    asked: list[str] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append(place_id)
        return PlaceSchedule("America/New_York", UnzonedHours({0: DayHours("00:00", "00:00")}))

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert asked == []
    assert store.hours_source == "safeway:local-page/yext-profile"
    assert store.timezone == "America/Los_Angeles"


async def test_a_transient_first_party_failure_does_not_let_google_take_the_row(db) -> None:
    """ "First-party wins" has to be a fact about the row, not about one call. A store page
    that 500s once leaves this run with no published hours; without the row-level check,
    that one bad afternoon replaces Safeway's week and dated holidays with Google's."""
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            hours=StoreHours("America/Los_Angeles", {0: DayHours("06:00", "22:00")}, {}),
            source="safeway:local-page/yext-profile",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
    )

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        return PlaceSchedule("America/New_York", UnzonedHours({0: DayHours("00:00", "00:00")}))

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)
    assert store.hours_source == "safeway:local-page/yext-profile", "premise: first party wrote it"

    # Now the retailer's own page stops answering, and only Google has anything to say.
    adapter._store_details = None  # the fetch attempt finds nothing
    store.hours_updated_at = None  # and the weekly gate lets it be read again
    await db.commit()
    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert store.hours_source == "safeway:local-page/yext-profile"
    assert store.timezone == "America/Los_Angeles", "Safeway's week survives a bad afternoon"


async def test_google_is_not_asked_for_a_zone_the_stores_own_coordinates_already_name(db) -> None:
    """Trader Joe's, and the billed request this removes.

    Its locator publishes the week and the coordinates in one record, so the zone was the
    only thing missing and the only thing Google was ever asked for -- one Places Details Pro
    lookup per store per week, for a fact the published address already implies. A point lies
    in exactly one timezone, so there is nothing left to buy.

    The decision is still made from the *payload* and never from the row: `_store_schedules`
    holds no `Store`, and the coordinates it reads are the ones the locator put on the
    `StoreLocation` it returned.
    """
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({day: DayHours("09:00", "21:00") for day in range(7)}),
            source="traderjoes:locator",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
        coordinates=(37.77111, -122.40738),
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/New_York", None)

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert asked == [], "the zone was readable offline, so nothing was bought"
    assert store.timezone == "America/Los_Angeles"
    assert store.hours_source == "traderjoes:locator"
    assert store.hours is not None and len(store.hours["weekly"]) == 7


async def test_google_is_still_asked_for_a_zone_a_store_with_no_coordinates_needs(db) -> None:
    """The rung is narrowed, not removed. A store placed only by the centroid of its ZIP has
    no point to look a zone up at, and for it the paid lookup is still the only answer."""
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/Los_Angeles", None)

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert [want_hours for _, want_hours in asked] == [False], "the zone only, the cheaper SKU"
    assert store.timezone == "America/Los_Angeles"
    assert store.hours_source == "traderjoes:locator"


async def test_a_store_with_no_week_of_its_own_is_still_asked_for_both(db) -> None:
    """Raley's. Coordinates can supply a zone and never a clock, so a retailer that publishes
    no hours anywhere still needs Google's week -- the dearer of the two SKUs."""
    location, adapter = _adapter(
        StoreDetails(external_id="1", source="raleys:sitemap"),
        place="https://maps.google.com/maps?cid=10195751074682041949",
        coordinates=(37.77111, -122.40738),
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/Los_Angeles", UnzonedHours({0: DayHours("06:00", "23:00")}))

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert [want_hours for _, want_hours in asked] == [True], "the week as well as the zone"
    assert store.hours_source == "google:places/details"


async def test_google_is_still_asked_when_the_coordinates_are_not_a_point_on_earth(db) -> None:
    """The fetch-phase skip must never be more permissive than the write it decides against.

    Skipping the paid lookup and then failing to derive at ingest is the one outcome worse
    than paying: the store keeps no hours *and* the fallback was declined, and since every
    attempt is stamped it stays that way for a week with nothing in the logs.
    """
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({0: DayHours("09:00", "21:00")}),
            source="traderjoes:locator",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
        coordinates=(999.0, -122.40738),  # a latitude that is not on Earth
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/Los_Angeles", None)

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert [want_hours for _, want_hours in asked] == [False], "garbage coordinates buy nothing"
    assert store.timezone == "America/Los_Angeles", "and Google's answer still lands"


async def test_google_is_still_asked_for_a_week_the_retailer_published_empty(db) -> None:
    """`UnzonedHours` with no days in it satisfies `is not None` and yields no schedule, so
    testing the wrong one of those would decline the lookup for a store that ends up with no
    hours at all."""
    location, adapter = _adapter(
        StoreDetails(
            external_id="1",
            unzoned_hours=UnzonedHours({}),
            source="traderjoes:locator",
        ),
        place="https://maps.google.com/maps?cid=10195751074682041949",
        coordinates=(37.77111, -122.40738),
    )
    asked: list[tuple[str, bool]] = []

    async def schedule(place_id: str, want_hours: bool) -> PlaceSchedule | None:
        asked.append((place_id, want_hours))
        return PlaceSchedule("America/Los_Angeles", UnzonedHours({0: DayHours("06:00", "23:00")}))

    store = await _scrape(db, adapter, location, schedule_resolver=schedule)

    assert asked, "an empty week is not a week; the lookup is still worth making"
    assert store.hours is not None


def test_a_half_stated_coordinate_pair_derives_nothing() -> None:
    """The fetch skip and the row read coordinates the same way: as a pair, published over
    locator, which is how `upsert_store` and `apply_store_details` write them.

    A field-wise coalesce would mix one source's latitude with another's longitude and derive
    a zone for a point neither payload describes -- then the skip declines the paid lookup
    while the row, written as an atomic pair, still has no coordinates to derive from. No
    registered adapter can produce this today (Trader Joe's `fetch_store_details` states no
    coordinates at all); it is pinned because the failure is silent and lasts a week.
    """
    located = StoreLocation("1", "Somewhere", latitude=None, longitude=-122.40738)
    details = StoreDetails(external_id="1", latitude=37.77111, longitude=None)

    assert _derivable_zone(located, details) is None
    # Either source stating a whole pair is enough, and the published record wins.
    assert _derivable_zone(located, replace(details, longitude=-122.40738)) == "America/Los_Angeles"
    assert (
        _derivable_zone(
            StoreLocation("1", "Somewhere", latitude=37.77111, longitude=-122.40738), None
        )
        == "America/Los_Angeles"
    )
