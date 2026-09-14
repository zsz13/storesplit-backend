"""Store opening hours: how they are held, and what is true at the store right now.

Pure functions over a retailer-agnostic schedule. Adapters translate whatever their retailer
publishes into `StoreHours`; the database keeps it as JSON; the API turns it into the one
sentence a shopper reads. A retailer that publishes nothing produces `unknown` -- there is no
default open time, and a store that is shut is worse to be sent to than one whose hours are
admitted to be unknown.

Times are local wall clock, "HH:MM", because that is what a store publishes and what a
shopper reads. A `closes` at or before its `opens` runs past midnight (08:00 -> 01:00).
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HoursState = Literal["open", "closed", "unknown"]
DAYS_AHEAD = 7  # far enough to find the next opening, short enough to give up on a dead store


@dataclass(frozen=True)
class DayHours:
    """One day's window. Both fields `None` means the store is closed that day."""

    opens: str | None
    closes: str | None

    def is_closed_all_day(self) -> bool:
        return self.opens is None or self.closes is None


@dataclass(frozen=True)
class StoreHours:
    timezone: str
    # 0 = Monday, matching `date.weekday()`. Missing weekdays are simply unknown.
    weekly: dict[int, DayHours]
    # ISO date -> that date's window, for the days a retailer publishes individually. A dated
    # window always beats the weekly one: it is how a holiday is published.
    dates: dict[str, DayHours]


@dataclass(frozen=True)
class UnzonedHours:
    """Weekday windows a retailer published without naming the zone they are kept in.

    Trader Joe's locator states a full week per store -- `monday_open` "09:00",
    `sunday_close` "21:00" -- and states no timezone anywhere in the record. A wall clock
    with no zone is not a fact about a store, so these windows must not be storable as a
    schedule by themselves, and this type is what makes that true by construction rather
    than by an author remembering the rule: the only way out of it is
    `hours_from_unzoned`, which needs a zone and returns None without one.

    The zone comes from somewhere else entirely -- a Google place already verified for this
    store, the zone a previous pass wrote on the row, or the one the store's own coordinates
    stand in (`normalize/timezones.py`) -- and never from the clock itself. The last of those
    is what took Trader Joe's off "Hours not published": the zone was never truly missing
    from its locator, only unread, because the same record carries the store's coordinates
    and a point lies in exactly one timezone.
    """

    weekly: dict[int, DayHours]


@dataclass(frozen=True)
class HoursToday:
    state: HoursState
    opens_at: str | None = None
    closes_at: str | None = None
    # "today", "tomorrow", or a weekday name when the next opening is further out.
    opens_day: str | None = None
    # True only when the retailer published *this* date as a day the store does not open at
    # all. It is a different sentence from "closed right now": "Closed today" tells a shopper
    # to stop planning around this shop, where "Closed - opens 8:00 AM" tells them to wait.
    # A weekday the retailer simply did not publish is neither; it stays plain `closed`.
    closed_all_day: bool = False
    # The next opening as an absolute instant, where there is one and the store is shut now.
    # `opens_at` is the wall clock a shopper reads and is the right thing to *print*; it is
    # the wrong thing to *sort* by, because "8:00 AM" in Reno and "8:00 AM" in San Francisco
    # are the same string and, one week in eight, not the same moment. The "everything
    # nearby is closed" dialog orders stores by their next opening, so it needs the instant.
    # None while the store is open, and None when nothing is published to open towards.
    next_open_at: datetime | None = None


def hours_from_published_days(
    days: dict[date, DayHours], timezone: str | None
) -> StoreHours | None:
    """A retailer's published calendar as a schedule: dated exactly, generalised carefully.

    Several retailers publish *the next week or two* rather than a weekly pattern -- Whole
    Foods about seven days, Target fourteen. Every weekday therefore appears at least once,
    and promoting each of them into the weekly pattern would make a single holiday closure
    that weekday's standing hours: read on 21 December, Christmas Day turns "Friday" into
    "closed", and a shopper is told that every Friday until the next scrape.

    So a day joins the weekly pattern only when its window is the one the store usually
    keeps -- the most common window across the days published. A day that differs stays a
    dated exception, where it is exactly right and generalises to nothing.

    Returns None without a timezone: a wall clock with no zone is not a fact about a store.
    """
    if not days or not timezone:
        return None
    dated: dict[str, DayHours] = {}
    seen: dict[int, list[DayHours]] = {}
    for day in sorted(days):
        window = days[day]
        dated[day.isoformat()] = window
        seen.setdefault(day.weekday(), []).append(window)
    usual = Counter(w for windows in seen.values() for w in windows if not w.is_closed_all_day())
    standing = usual.most_common(1)[0][0] if usual else None
    weekly: dict[int, DayHours] = {}
    for weekday, windows in seen.items():
        # A weekday the retailer published more than once, agreeing with itself, is that
        # weekday's rule however unusual it looks -- a shop that shuts early every Sunday, or
        # shuts entirely. Target publishes fourteen days, so its weekdays corroborate in
        # pairs; Whole Foods publishes about seven and each weekday appears once, where the
        # only safe reading is still "the window this store usually keeps".
        if len(windows) > 1 and len(set(windows)) == 1:
            weekly[weekday] = windows[0]
        elif standing is not None and windows[-1] == standing:
            weekly[weekday] = standing
    return StoreHours(timezone=timezone, weekly=weekly, dates=dated)


def hours_from_weekly(
    weekly: dict[int, DayHours], dates: dict[str, DayHours], timezone: str | None
) -> StoreHours | None:
    """A schedule from a retailer that publishes a real weekly pattern plus dated exceptions.

    Safeway's Yext profile is this shape (`normalHours` by weekday, `holidayHours` by date),
    and so is any retailer that states "Mon-Sun 6am-11pm". Nothing is generalised here
    because nothing needs to be: the retailer already said which rule is the standing one.
    """
    if not timezone or (not weekly and not dates):
        return None
    return StoreHours(timezone=timezone, weekly=dict(weekly), dates=dict(dates))


def hours_from_unzoned(unzoned: UnzonedHours | None, timezone: str | None) -> StoreHours | None:
    """Windows a retailer published, paired with a zone that came from somewhere else.

    The one exit from `UnzonedHours`, and it is deliberately narrow. Trader Joe's is the
    case it exists for: a complete week per store and no timezone on any surface, so its
    clock becomes a schedule only once a zone has been established for that store by some
    other means -- and stays unknown when none ever is, which is still the honest answer for
    a store nobody has coordinates for.
    """
    if unzoned is None:
        return None
    return hours_from_weekly(unzoned.weekly, {}, timezone)


def hours_today(hours: StoreHours | None, now: datetime) -> HoursToday:
    """What is true at this store at this instant, decided in the store's own timezone."""
    if hours is None or (not hours.weekly and not hours.dates):
        return HoursToday("unknown")
    try:
        zone = ZoneInfo(hours.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        # An unusable timezone cannot be reasoned about, and reasoning in UTC instead would
        # put a California store's closing time eight hours out.
        return HoursToday("unknown")

    local = now.astimezone(zone)
    open_window = _open_window(hours, local)
    if open_window is not None:
        return HoursToday("open", opens_at=open_window.opens, closes_at=open_window.closes)

    today = _for_day(hours, local.date())
    shut_all_day = today is not None and today.is_closed_all_day()
    upcoming = _next_opening(hours, local)
    if upcoming is None:
        # Nothing published to open towards. A stated closure is still a stated fact, so it
        # is reported; anything else is genuinely unknown rather than quietly "closed".
        return HoursToday("closed", closed_all_day=True) if shut_all_day else HoursToday("unknown")
    day, window = upcoming
    return HoursToday(
        "closed",
        opens_at=window.opens,
        closes_at=window.closes,
        opens_day=_day_label(local, day),
        closed_all_day=shut_all_day,
        next_open_at=_at(day, window.opens, local.tzinfo),
    )


def _for_day(hours: StoreHours, day: date) -> DayHours | None:
    dated = hours.dates.get(day.isoformat())
    if dated is not None:
        return dated
    return hours.weekly.get(day.weekday())


def _open_window(hours: StoreHours, local: datetime) -> DayHours | None:
    """The window containing this instant, whether it began today or before midnight."""
    for day in (local.date(), local.date() - timedelta(days=1)):
        window = _for_day(hours, day)
        if window is None or window.is_closed_all_day():
            continue
        opens = _at(day, window.opens, local.tzinfo)
        closes = _at(day, window.closes, local.tzinfo)
        if opens is None or closes is None:
            continue
        if closes <= opens:  # runs past midnight into the next day
            closes += timedelta(days=1)
        if opens <= local < closes:
            return window
    return None


def _next_opening(hours: StoreHours, local: datetime) -> tuple[date, DayHours] | None:
    for offset in range(DAYS_AHEAD):
        day = local.date() + timedelta(days=offset)
        window = _for_day(hours, day)
        if window is None or window.is_closed_all_day():
            continue
        opens = _at(day, window.opens, local.tzinfo)
        if opens is not None and opens > local:
            return day, window
    return None


def _at(day: date, wall_clock: str | None, zone: Any) -> datetime | None:
    parsed = parse_wall_clock(wall_clock)
    if parsed is None:
        return None
    return datetime.combine(day, parsed, tzinfo=zone)


def _day_label(local: datetime, day: date) -> str:
    delta = (day - local.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return day.strftime("%A")


def parse_wall_clock(value: str | None) -> time | None:
    """ "08:00" or "0800" -> a time; anything else -> None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) == 4 and text.isdigit():
        text = f"{text[:2]}:{text[2:]}"
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


_CLOCK_12H = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?")


def parse_clock_12h(raw: Any) -> str | None:
    """ "6 AM" / "10:30 PM" / "7:00AM" -> "06:00" / "22:30" / "07:00".

    Retailers that write opening times for people rather than for machines write them like
    this, with or without the minutes, the space and the full stops -- Smart & Final's
    "Sunday-Saturday: 6 AM - 10 PM" and Sprouts' `"7:00AM"` are the same clock in two
    dialects. It lives here rather than in either adapter because reading a clock is
    normalization, not retailer knowledge, and two copies of it would be two places for a
    midnight bug to hide.
    """
    if not isinstance(raw, str):
        return None
    match = _CLOCK_12H.fullmatch(raw.strip())
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + 12 if match.group(3).lower() == "p" else hour % 12
    text = f"{hour:02d}:{minute:02d}"
    return text if parse_wall_clock(text) else None


_CLOCK_24H = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_clock_24h(raw: Any) -> str | None:
    """ "06:00:00" / "22:00" -> "06:00" / "22:00", and "24:00:00" -> "00:00".

    The machine-readable dialect, beside `parse_clock_12h`'s human one: a retailer whose
    hours come out of a scheduling system publishes a full `HH:MM:SS`. Seconds are dropped
    rather than kept, because `DayHours` holds a wall clock a shopper reads and "22:00:00"
    is the same closing time spelled longer.

    Midnight at the end of a day is written both ways in the wild -- the Save Mart Companies
    banners publish `"00:00:00"`, Safeway's Yext profile publishes 2400 -- and `24:00` is not
    a time `time.fromisoformat` will parse. Both mean the same instant, so both become
    "00:00", which `hours_today` already reads as running past midnight when it closes a day.
    """
    if not isinstance(raw, str):
        return None
    match = _CLOCK_24H.fullmatch(raw.strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if (hour, minute) == (24, 0):
        return "00:00"
    if hour > 23 or minute > 59:
        return None
    text = f"{hour:02d}:{minute:02d}"
    return text if parse_wall_clock(text) else None


WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_DAY_RANGE_RE = re.compile(r"(?P<start>[a-z]+)\s*-\s*(?P<end>[a-z]+)")
# An en dash, an em dash and the words a retailer writes a range with, all reduced to the
# one separator the pattern above knows. Written as escapes rather than as themselves: a
# bare en dash in source is a character nobody can tell from a hyphen at a glance.
_RANGE_SEPARATORS = {"\u2013": "-", "\u2014": "-", " to ": "-", " through ": "-", " thru ": "-"}


def parse_weekday(raw: Any) -> int | None:
    """A weekday name as `date.weekday()` -- "Friday" / "FRI" / "friday" -> 4.

    Whole names and their first three letters, because retailers write both and neither is
    ambiguous in English: no two weekday names share a three-letter prefix. Anything else is
    None, so a wording nobody anticipated loses that day rather than silently landing on
    Monday.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text in WEEKDAY_INDEX:
        return WEEKDAY_INDEX[text]
    if len(text) == 3:
        return next((i for name, i in WEEKDAY_INDEX.items() if name.startswith(text)), None)
    return None


def parse_day_range(raw: Any) -> tuple[int, ...]:
    """ "Friday - Sunday" -> (4, 5, 6); "Monday" -> (0,); anything unreadable -> ().

    99 Ranch states a store's week as a handful of inclusive ranges
    (`"Monday - Thursday"` 08:00-21:00, `"Friday - Sunday"` 08:00-22:00) rather than as
    seven days, so a range has to become the days it covers before it can become a schedule.

    A range that runs past Sunday wraps: "Saturday - Monday" is Saturday, Sunday, Monday,
    walked forward from the start rather than sorted, because sorting would silently turn a
    three-day weekend into the five weekdays between its ends. An empty tuple is the answer
    for anything unreadable, and the caller drops those days rather than guessing at them --
    half a week shown as a whole one is worse than admitting the hours are unknown.
    """
    if not isinstance(raw, str):
        return ()
    text = raw.strip().lower()
    for separator, plain in _RANGE_SEPARATORS.items():
        text = text.replace(separator, plain)
    match = _DAY_RANGE_RE.fullmatch(text.strip())
    if match is None:
        single = parse_weekday(text)
        return (single,) if single is not None else ()
    start, end = parse_weekday(match.group("start")), parse_weekday(match.group("end"))
    if start is None or end is None:
        return ()
    span = (end - start) % 7
    return tuple((start + step) % 7 for step in range(span + 1))


def wall_clock(moment: datetime, timezone: str) -> str | None:
    """A UTC instant as "HH:MM" at the store, for retailers that publish absolute times."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return moment.astimezone(zone).strftime("%H:%M")


def store_hours_from_json(raw: Any, timezone: str | None) -> StoreHours | None:
    """Rebuild a schedule from the JSON column, tolerating anything a hand edit produced."""
    if not isinstance(raw, dict) or not timezone:
        return None
    weekly: dict[int, DayHours] = {}
    for key, value in (raw.get("weekly") or {}).items():
        try:
            weekday = int(key)
        except (TypeError, ValueError):
            continue
        window = _day_hours_from_json(value)
        if window is not None and 0 <= weekday <= 6:
            weekly[weekday] = window
    dates: dict[str, DayHours] = {}
    for key, value in (raw.get("dates") or {}).items():
        window = _day_hours_from_json(value)
        if window is not None and isinstance(key, str):
            dates[key] = window
    if not weekly and not dates:
        return None
    return StoreHours(timezone=timezone, weekly=weekly, dates=dates)


def store_hours_to_json(hours: StoreHours) -> dict[str, Any]:
    return {
        "weekly": {str(day): _day_hours_to_json(window) for day, window in hours.weekly.items()},
        "dates": {day: _day_hours_to_json(window) for day, window in hours.dates.items()},
    }


def _day_hours_from_json(value: Any) -> DayHours | None:
    if not isinstance(value, dict):
        return None
    opens = value.get("opens")
    closes = value.get("closes")
    if opens is None and closes is None:
        return DayHours(None, None)  # published as closed all day
    if parse_wall_clock(opens) is None or parse_wall_clock(closes) is None:
        return None
    return DayHours(str(opens), str(closes))


def _day_hours_to_json(window: DayHours) -> dict[str, str | None]:
    return {"opens": window.opens, "closes": window.closes}


def utc_now() -> datetime:
    return datetime.now(UTC)
