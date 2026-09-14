"""Reading a store's timezone off its own coordinates.

The gap this fills: several retailers publish a complete weekly clock and never name the
zone it is kept in -- Trader Joe's states `monday_open` "09:00" for every store in the
country and no timezone on any surface it has. A wall clock with no zone is not a fact about
a store, so before this those weeks were unusable and the only way to a zone was Google
Places, billed, for a fact that is already implied by the address.

A store stands at a point, and a point is in exactly one zone. `timezone_at` is that lookup
and nothing more: offline, deterministic, no network, no key, no third party asked.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.normalize.hours import DayHours, UnzonedHours, hours_from_unzoned, hours_today
from app.normalize.timezones import US_TIMEZONES, timezone_at


def test_a_store_gets_the_zone_it_stands_in() -> None:
    """Trader Joe's San Francisco - 9th St (78), at the coordinates its locator publishes."""
    assert timezone_at(37.77111, -122.40738) == "America/Los_Angeles"


def test_the_zone_follows_the_point_and_not_the_state() -> None:
    """The whole reason coordinates are read rather than the state the retailer published.

    Indiana keeps two zones: the Chicago commuter counties in its north-west corner are
    Central, and the rest of the state is Eastern. A state-level table would put Trader
    Joe's Schererville store an hour wrong every day of the year.
    """
    schererville = timezone_at(41.4789, -87.4545)
    indianapolis = timezone_at(39.7684, -86.1581)

    assert schererville == "America/Chicago"
    assert indianapolis == "America/Indiana/Indianapolis"
    assert schererville != indianapolis


def test_a_zone_that_keeps_no_daylight_saving_is_kept_as_itself() -> None:
    """Phoenix is not Denver for eight months of the year.

    `America/Phoenix` does not observe DST, so folding Arizona into the Mountain zone would
    be right in winter and an hour wrong all summer -- the kind of error that reads as
    correct on the day it ships.
    """
    assert timezone_at(33.4484, -112.0740) == "America/Phoenix"


def test_a_pair_that_is_not_a_point_on_earth_yields_nothing() -> None:
    """A retailer's payload is the source of these numbers, so a bad one has to be survivable."""
    assert timezone_at(91.0, -122.4) is None
    assert timezone_at(37.77, 181.0) is None
    assert timezone_at(None, -122.4) is None
    assert timezone_at(37.77, None) is None
    assert timezone_at(None, None) is None


def test_a_point_outside_the_united_states_yields_nothing() -> None:
    """StoreSplit is US-only in every other dimension -- ZIP codes, Census centroids, `e164_us`
    -- so a store resolving abroad is a broken coordinate, not a distant shop.

    The case this is really for is a dropped minus sign on the longitude, the commonest
    coordinate defect there is: San Francisco's -122.4 read as +122.4 lands in China. Without
    this the answer is wrong by fifteen hours instead of by one, and confident with it --
    a store shown open at four in the morning and hidden while its doors are really open.
    """
    assert timezone_at(37.77111, 122.40738) is None, "sign-flipped longitude: Asia, not a store"
    assert timezone_at(51.5074, -0.1278) is None, "London"
    assert timezone_at(43.6532, -79.3832) is None, "Toronto -- America/*, and still not the US"


def test_a_point_in_open_water_yields_nothing_rather_than_a_nautical_zone() -> None:
    """Mid-Pacific resolves to `Etc/GMT+9`, a real zone and the wrong one to keep.

    No shop stands in open water, so this is what wrong coordinates look like -- and a
    fixed `Etc/` offset keeps no daylight saving, so a US store read in one would be right
    in winter and an hour wrong all summer.
    """
    assert timezone_at(0.0, -140.0) is None


def test_every_zone_returned_is_one_the_schedule_reader_can_load() -> None:
    """`hours_today` answers "unknown" for a zone `ZoneInfo` cannot load, so a zone that does
    not round-trip here would silently cost a store its hours rather than raise."""
    points = [
        (37.77111, -122.40738),  # San Francisco
        (41.4789, -87.4545),  # Schererville, IN
        (38.2527, -85.7585),  # Louisville, KY
        (33.4484, -112.0740),  # Phoenix, AZ
        (31.7619, -106.4850),  # El Paso, TX
        (30.4213, -87.2169),  # Pensacola, FL
        (42.9634, -85.6681),  # Grand Rapids, MI
        (21.3069, -157.8583),  # Honolulu, HI
    ]
    for latitude, longitude in points:
        zone = timezone_at(latitude, longitude)
        assert zone is not None, (latitude, longitude)
        assert ZoneInfo(zone) is not None


def test_a_derived_zone_turns_an_unzoned_week_into_a_readable_schedule() -> None:
    """The end this exists for: Trader Joe's week, read in the zone its store stands in."""
    week = UnzonedHours({day: DayHours("09:00", "21:00") for day in range(7)})

    hours = hours_from_unzoned(week, timezone_at(37.77111, -122.40738))

    assert hours is not None
    assert hours.timezone == "America/Los_Angeles"
    # 12:00 in San Francisco, inside the 09:00-21:00 window.
    assert hours_today(hours, datetime(2026, 9, 10, 19, 0, tzinfo=UTC)).state == "open"


def test_the_united_states_zone_set_is_every_zone_the_country_keeps() -> None:
    """A codomain check is only as good as its list: a zone wrongly missing here silently
    costs every store in it its hours. Pinned by count and by the entries most easily lost --
    the ones that exist because a state is split, which is exactly why the polygons are read
    in the first place."""
    assert len(US_TIMEZONES) == 29
    for zone in (
        "America/Indiana/Indianapolis",
        "America/Indiana/Knox",
        "America/Kentucky/Louisville",
        "America/North_Dakota/Beulah",
        "America/Menominee",
        "America/Boise",
        "America/Phoenix",
        "America/Detroit",
        "Pacific/Honolulu",
        "America/Adak",
    ):
        assert zone in US_TIMEZONES, zone


def test_every_zone_in_the_united_states_set_is_one_zoneinfo_can_load() -> None:
    """A typo in the set would not fail here by itself -- it would quietly reject the real
    zone and cost those stores their hours -- so each entry is loaded rather than trusted."""
    for zone in US_TIMEZONES:
        assert ZoneInfo(zone) is not None, zone
