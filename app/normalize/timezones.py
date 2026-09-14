"""The timezone a store stands in, read from its own coordinates.

A store's week is a wall clock, and a wall clock is only a fact about a store once the zone
it is kept in is known. Most retailers that publish hours publish the zone beside them --
Safeway's Yext `timezone`, Target's `iso_time_zone_code`, Sprouts' `timezone`. Some publish
a complete week and no zone at all: Trader Joe's states `monday_open` .. `sunday_close` for
every store in the country and names a timezone on no surface it has.

For those, the zone was previously bought from Google Places, which is a billed request for
something the address already implies. A store stands at a point; a point lies in exactly one
timezone; the boundaries between them are published data. So this reads the zone off the
coordinates the retailer already gave, offline and with nobody asked.

`tzfpy` is the lookup, chosen over the alternatives because it carries the
timezone-boundary polygons inside one small wheel and needs no runtime dependency of its
own -- the zone is decided by the real boundary rather than by a state-shaped approximation
of it, which matters exactly where it is hardest: Trader Joe's Schererville store is in
Indiana and on Chicago time, and Phoenix keeps no daylight saving while the rest of the
Mountain zone does.

This is a fallback for a zone, never for a clock. Nothing here invents opening hours.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzfpy import get_tz

from app.normalize.geo import is_on_earth

# The 29 zones the United States actually keeps, from the `US` rows of tzdata's `zone.tab`.
#
# This is a **codomain check, not a resolver**: the polygons decide the answer, and this only
# refuses one that cannot belong to a store in this system. StoreSplit is US-only in every
# other dimension already -- ZIP codes, Census ZCTA centroids, `normalize/phone.py::e164_us`
# -- so a US store resolving to `Asia/Shanghai` is not a store in Shanghai, it is a broken
# coordinate, and the commonest coordinate defect is a dropped minus sign on the longitude.
# Without this the wrong answer is wrong by fifteen hours rather than by one, and it is
# confident: a store shown open at four in the morning, and dropped from "Open now" while its
# doors are really open.
#
# Frozen as a literal rather than read from `zone.tab` at import: the file is not guaranteed
# to exist beside every `zoneinfo` installation (the `tzdata` wheel ships no `zone.tab`), and
# a check that silently degrades to "allow everything" on such a host is worse than no check.
# The set changes about once a decade; `tests/test_timezones.py` pins every entry as loadable.
US_TIMEZONES: frozenset[str] = frozenset(
    {
        "America/Adak",
        "America/Anchorage",
        "America/Boise",
        "America/Chicago",
        "America/Denver",
        "America/Detroit",
        "America/Indiana/Indianapolis",
        "America/Indiana/Knox",
        "America/Indiana/Marengo",
        "America/Indiana/Petersburg",
        "America/Indiana/Tell_City",
        "America/Indiana/Vevay",
        "America/Indiana/Vincennes",
        "America/Indiana/Winamac",
        "America/Juneau",
        "America/Kentucky/Louisville",
        "America/Kentucky/Monticello",
        "America/Los_Angeles",
        "America/Menominee",
        "America/Metlakatla",
        "America/New_York",
        "America/Nome",
        "America/North_Dakota/Beulah",
        "America/North_Dakota/Center",
        "America/North_Dakota/New_Salem",
        "America/Phoenix",
        "America/Sitka",
        "America/Yakutat",
        "Pacific/Honolulu",
    }
)


def timezone_at(latitude: float | None, longitude: float | None) -> str | None:
    """The IANA zone covering this point, or None where there is no usable answer.

    None for coordinates that are absent or not a point on Earth -- these numbers come out
    of a retailer's payload -- and None for a point that lands in open water, where the
    answer is a nautical `Etc/GMT±N`. That is a real zone and the wrong one to keep: no shop
    stands in it, so getting one back means the coordinates do not name a place, and its
    fixed offset observes no daylight saving -- a US store read in one would be right in
    winter and an hour wrong all summer, which is worse than admitting the hours are unknown.

    None, too, for any zone outside `US_TIMEZONES` -- see that constant for why a zone this
    system cannot use is treated as a broken coordinate rather than as a distant store.

    A zone is also dropped unless `ZoneInfo` can load it: `hours_today` reads the schedule
    through `ZoneInfo` and answers "unknown" for a name it cannot resolve, so a zone that
    does not round-trip here would cost the store its hours quietly, one layer further down,
    instead of simply never being written.
    """
    # `is_on_earth` owns what makes a pair a real point -- NaN, infinity, out of range -- and
    # rejects a missing one too. The explicit `is None` in front of it is there because it
    # returns a plain `bool` rather than a `TypeGuard`, so it narrows nothing for the reader
    # or the type checker, and `get_tz` takes two floats.
    if latitude is None or longitude is None or not is_on_earth(latitude, longitude):
        return None
    zone = get_tz(longitude, latitude)
    # `Etc/GMT±N` is what open water resolves to and is excluded by `US_TIMEZONES` along with
    # everything else; the membership test is the whole check.
    if zone not in US_TIMEZONES:
        return None
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return zone
