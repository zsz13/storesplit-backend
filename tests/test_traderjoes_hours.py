"""Trader Joe's hours, end to end, with nobody asked about anything.

Trader Joe's was the one retailer whose hours were missing for a reason that was not a
missing parser and not a closed door. Its locator has always published a complete week per
store -- `monday_open` "09:00" .. `sunday_close` "21:00" -- and names a timezone on no
surface it has: not the locator record, not the store page, not the GraphQL API. A wall
clock with no zone is not a fact about a store, so the week was carried as `UnzonedHours`
and every store said "Hours not published".

The zone was never actually missing, only unread: the same locator record carries the
store's coordinates, and a point lies in exactly one timezone. `normalize/timezones.py` is
that lookup, and these tests drive the **real** adapter over the **real** captured payload
to prove the week now survives all the way to what a shopper reads -- no Google key, no
network, and no third party asked for a fact Trader Joe's address already implies.

The two stores named throughout are the two San Francisco branches the locator returns for
94103: `San Francisco - 9th St (78)` at 555 9th St, and `San Francisco - Pacific Place (225)`
at 10 4th Street.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.db.models import Store
from app.normalize.hours import hours_today, store_hours_from_json
from app.retailers.traderjoes import adapter as traderjoes
from app.services.scraper import run_scrape
from app.services.stores import open_now_stores, store_hours_state
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.test_scrape_and_api import use_adapters

FIXTURES = Path(__file__).parent / "fixtures" / "traderjoes"

# 12:00 on Thursday 10 September 2026 in America/Los_Angeles, inside Trader Joe's 09:00-21:00.
PACIFIC_NOON = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
# 06:00 the same morning in San Francisco: before the doors open, after midnight.
PACIFIC_DAWN = datetime(2026, 9, 10, 13, 0, tzinfo=UTC)

NINTH_ST = "78"
PACIFIC_PLACE = "225"


class _Response:
    """Just enough of `httpx.Response` for the adapter: a status, a body, and no raising."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


async def _offline_request(_client: Any, _method: str, url: str, **_kwargs: Any) -> _Response:
    """The adapter's one HTTP call, answered from the captured payloads.

    Only the transport is replaced. `find_stores`, `fetch_store_details`,
    `parse_locator_results` and `parse_locator_hours` all run as they ship, so a regression
    in any of them fails here rather than passing against a hand-written stand-in.
    """
    if url == traderjoes.LOCATOR_URL:
        return _Response(_fixture("locator_94110.json"))
    if url == traderjoes.GRAPHQL_URL:
        return _Response(_fixture("search_eggs.json"))
    raise AssertionError(f"the adapter reached an unexpected URL: {url}")


@pytest.fixture
async def scraped_traderjoes(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    """One real scrape of Trader Joe's, with no Google key and so no schedule resolver.

    The per-retailer cap is lifted from its default of two so the run covers both of the
    branches this change was checked against -- 94103's ranking picks 9th St and Hayes
    Valley, and Pacific Place is third. The cap is a fact about how a ZIP is compared, not
    about whether hours can be read, and every store the locator returns is exercised here.
    """
    settings_override(scrape_stores_per_retailer=5)
    monkeypatch.setattr(traderjoes, "request_with_retry", _offline_request)
    use_adapters(monkeypatch, traderjoes.TraderJoesAdapter(clients))  # type: ignore[arg-type]
    await run_scrape(sessionmaker, clients, "94103", None, ["eggs"])


async def _stores(db: AsyncSession) -> dict[str, Store]:
    rows = await db.scalars(select(Store).options(selectinload(Store.retailer)))
    return {store.external_id: store for store in rows}


# -------------------------------------------------------------- what the scrape writes down


async def test_both_san_francisco_stores_keep_a_full_week(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """The whole schedule, not the one line the store page prints as "Today's Hours"."""
    stores = await _stores(db)

    for external_id in (NINTH_ST, PACIFIC_PLACE):
        store = stores[external_id]
        assert store.hours is not None, f"store {external_id} still publishes nothing"
        assert sorted(store.hours["weekly"]) == ["0", "1", "2", "3", "4", "5", "6"]
        assert store.hours["weekly"]["6"] == {"opens": "09:00", "closes": "21:00"}, "Sunday too"


async def test_the_zone_is_the_one_the_stores_stand_in(
    db: AsyncSession, scraped_traderjoes
) -> None:
    stores = await _stores(db)

    assert stores[NINTH_ST].timezone == "America/Los_Angeles"
    assert stores[PACIFIC_PLACE].timezone == "America/Los_Angeles"


async def test_trader_joes_stays_the_source_of_its_own_hours(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """Only the frame the clock is read in came from anywhere else, and that is not Google
    either -- it is the coordinates Trader Joe's published in the very same record."""
    stores = await _stores(db)

    assert stores[NINTH_ST].hours_source == traderjoes.DETAILS_SOURCE
    assert stores[NINTH_ST].hours_source == "traderjoes:locator"


async def test_no_store_is_left_saying_hours_not_published(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """Every store the locator published a clock for, which is every store that is open."""
    stores = await _stores(db)
    unknown = [
        external_id
        for external_id, store in stores.items()
        if store_hours_state(store, PACIFIC_NOON) == "unknown"
    ]

    assert unknown == [], "these stores publish a week and stand somewhere"


# ------------------------------------------------------------------ what a shopper is told


async def test_a_store_open_at_noon_reports_when_it_shuts(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """The data behind "Open until 9:00 PM". `lib/format.ts` needs `closes_at` to say it."""
    store = (await _stores(db))[NINTH_ST]

    today = hours_today(store_hours_from_json(store.hours, store.timezone), PACIFIC_NOON)

    assert today.state == "open"
    assert today.closes_at == "21:00"


async def test_a_store_shut_at_dawn_reports_when_it_opens(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """The data behind "Closed - Opens 9:00 AM", and `next_open_at` is the instant the
    "everything nearby is closed" dialog orders shut stores by."""
    store = (await _stores(db))[PACIFIC_PLACE]

    today = hours_today(store_hours_from_json(store.hours, store.timezone), PACIFIC_DAWN)

    assert today.state == "closed"
    assert today.opens_at == "09:00"
    assert today.opens_day == "today"
    assert today.next_open_at == datetime(2026, 9, 10, 16, 0, tzinfo=UTC), "09:00 in California"
    assert today.closed_all_day is False, "shut for the night is not shut for the day"


# ---------------------------------------------------------------------- the "Open now" filter


async def test_open_now_keeps_trader_joes_while_it_is_open(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """It used to be kept only because its hours were unknown -- the concession the filter
    makes for a store nobody can vouch for. Now it is kept because it is open."""
    stores = list((await _stores(db)).values())

    kept = open_now_stores(stores, PACIFIC_NOON)

    assert {store.external_id for store in kept} >= {NINTH_ST, PACIFIC_PLACE}
    assert all(store_hours_state(store, PACIFIC_NOON) == "open" for store in kept)


async def test_open_now_drops_trader_joes_once_it_has_shut(
    db: AsyncSession, scraped_traderjoes
) -> None:
    """The half that was impossible before. A store with no zone can never be *known* shut,
    so "Open now" could only ever wave Trader Joe's through at four in the morning."""
    stores = list((await _stores(db)).values())

    kept = open_now_stores(stores, PACIFIC_DAWN)

    assert [store.external_id for store in kept] == [], "at 06:00 every one of them is shut"


async def test_the_api_reports_the_state_and_the_client_needs_no_clock(
    client: AsyncClient, db: AsyncSession, scraped_traderjoes
) -> None:
    """`StoreOut.hours_today` is decided on the server in the store's own timezone, which is
    what lets the browser render a sentence without ever re-deciding it."""
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94103"})).json()
    stores = {store["name"]: store for store in body["stores"]}

    assert stores, "premise: the scrape resolved Trader Joe's stores for this ZIP"
    for store in stores.values():
        assert store["timezone"] == "America/Los_Angeles"
        assert store["hours_today"]["state"] in {"open", "closed"}, "never unknown any more"
