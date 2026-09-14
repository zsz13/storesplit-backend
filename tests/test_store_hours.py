"""Today's hours, in the store's own timezone.

The shopper is told "Open until 10:00 PM" or "Closed - Opens 8:00 AM", so the only question
this module answers is what is true at the store right now: never at the server, never in UTC,
and never a guess when the retailer published nothing.
"""

from datetime import UTC, datetime

from app.normalize.hours import DayHours, StoreHours, hours_today

WEEKLY = StoreHours(
    timezone="America/Los_Angeles",
    weekly={day: DayHours("08:00", "22:00") for day in range(7)},
    dates={},
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_open_now_reports_when_it_closes() -> None:
    # 2026-09-10 19:00Z is 12:00 in Los Angeles.
    today = hours_today(WEEKLY, _utc("2026-09-10T19:00"))

    assert today.state == "open"
    assert today.closes_at == "22:00"


def test_before_opening_reports_todays_opening() -> None:
    # 14:00Z is 07:00 local, an hour before the doors open.
    today = hours_today(WEEKLY, _utc("2026-09-10T14:00"))

    assert today.state == "closed"
    assert today.opens_at == "08:00" and today.opens_day == "today"


def test_after_closing_reports_tomorrows_opening() -> None:
    # 06:00Z is 23:00 the previous local day, an hour after closing.
    today = hours_today(WEEKLY, _utc("2026-09-11T06:00"))

    assert today.state == "closed"
    assert today.opens_at == "08:00" and today.opens_day == "tomorrow"


def test_a_window_running_past_midnight_is_still_open_after_midnight() -> None:
    late = StoreHours(
        timezone="America/Los_Angeles",
        weekly={day: DayHours("08:00", "01:00") for day in range(7)},
        dates={},
    )

    # 07:30Z is 00:30 local: yesterday's window has not closed yet.
    assert hours_today(late, _utc("2026-09-11T07:30")).state == "open"
    # 09:30Z is 02:30 local: it has.
    assert hours_today(late, _utc("2026-09-11T09:30")).state == "closed"


def test_a_closed_day_reports_the_next_day_that_opens() -> None:
    # Thursday 2026-09-10 is closed; Friday opens at 09:00.
    schedule = StoreHours(
        timezone="America/Los_Angeles",
        weekly={
            0: DayHours("08:00", "22:00"),
            1: DayHours("08:00", "22:00"),
            2: DayHours("08:00", "22:00"),
            3: DayHours(None, None),
            4: DayHours("09:00", "22:00"),
            5: DayHours("08:00", "22:00"),
            6: DayHours("08:00", "22:00"),
        },
        dates={},
    )

    today = hours_today(schedule, _utc("2026-09-10T19:00"))

    assert today.state == "closed"
    assert today.opens_at == "09:00" and today.opens_day == "tomorrow"


def test_a_dated_window_overrides_the_weekly_one() -> None:
    holiday = StoreHours(
        timezone="America/Los_Angeles",
        weekly={day: DayHours("08:00", "22:00") for day in range(7)},
        dates={"2026-09-10": DayHours("08:00", "15:00")},
    )

    today = hours_today(holiday, _utc("2026-09-10T19:00"))

    assert today.state == "open" and today.closes_at == "15:00"


def test_hours_nobody_published_are_unknown_rather_than_guessed() -> None:
    assert hours_today(None, _utc("2026-09-10T19:00")).state == "unknown"

    empty = StoreHours(timezone="America/Los_Angeles", weekly={}, dates={})
    assert hours_today(empty, _utc("2026-09-10T19:00")).state == "unknown"

    unusable_timezone = StoreHours(
        timezone="Mars/Olympus", weekly={0: DayHours("08:00", "22:00")}, dates={}
    )
    assert hours_today(unusable_timezone, _utc("2026-09-10T19:00")).state == "unknown"


def test_the_store_timezone_decides_the_day_not_the_server() -> None:
    """03:00Z on the 11th is still Thursday the 10th in Los Angeles."""
    thursday_only = StoreHours(
        timezone="America/Los_Angeles",
        weekly={3: DayHours("08:00", "22:00")},  # Thursday
        dates={},
    )

    assert hours_today(thursday_only, _utc("2026-09-11T03:00")).state == "open"
