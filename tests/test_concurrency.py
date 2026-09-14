"""What the async rewrite is supposed to buy: real concurrency, real bounds, real isolation.

Every test here is deterministic: the fakes sleep for a fixed time and count how many calls
overlap, so "these really ran at the same time" is an assertion, not a stopwatch guess.
"""

import asyncio
import time

import pytest
from app.concurrency import describe_exception, gather_bounded
from app.db.models import Offer, RetailerProduct, ScrapeRun, Store
from app.normalize.categories import CATEGORIES
from app.retailers.clients import RetailerClients
from app.retailers.raleys.adapter import RaleysAdapter
from app.retailers.smartandfinal.adapter import SmartAndFinalAdapter
from app.retailers.sprouts.adapter import SproutsAdapter
from app.retailers.wholefoods.adapter import WholeFoodsAdapter
from app.services import scraper
from app.services.scraper import fetch_retailer, run_scrape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import STORE_A, STORE_B, FakeAdapter, NestedFakeAdapter, Tracker, listing

DELAY = 0.05


def eggs_at(store: str) -> dict[str, dict[str, list]]:
    return {"eggs": {store: [listing(f"{store}-eggs", "Eggs, 12 CT", store, "4.99")]}}


def use_adapters(monkeypatch: pytest.MonkeyPatch, *adapters: FakeAdapter) -> None:
    registry = {adapter.slug: adapter for adapter in adapters}
    monkeypatch.setattr(scraper, "adapter_slugs", lambda: list(registry))
    monkeypatch.setattr(scraper, "get_adapter", lambda slug, clients: registry[slug])


# --------------------------------------------------------------------------- helpers


async def test_gather_bounded_keeps_order_and_never_exceeds_the_bound() -> None:
    tracker = Tracker()

    async def work(value: int) -> int:
        async with tracker.track():
            await asyncio.sleep(DELAY)
            return value

    results = await gather_bounded(3, [lambda v=v: work(v) for v in range(9)])
    assert results == list(range(9))
    assert tracker.peak == 3
    assert tracker.total == 9


async def test_gather_bounded_cancels_the_siblings_when_one_fails() -> None:
    completed = 0

    async def failing() -> int:
        raise ValueError("boom")

    async def slow() -> int:
        nonlocal completed
        await asyncio.sleep(30)
        completed += 1
        return 1

    with pytest.raises(BaseExceptionGroup) as caught:
        await gather_bounded(4, [failing, slow, slow])
    assert describe_exception(caught.value) == "ValueError: boom"
    assert completed == 0, "the siblings must be cancelled, not left running"


def test_describe_exception_unwraps_task_groups() -> None:
    inner = RuntimeError("upstream exploded")
    grouped = BaseExceptionGroup("unhandled", [BaseExceptionGroup("nested", [inner])])
    assert describe_exception(grouped) == "RuntimeError: upstream exploded"
    assert describe_exception(inner) == "RuntimeError: upstream exploded"


# --------------------------------------------------------------------------- clients


async def test_retailers_share_one_client_unless_they_need_their_own(clients) -> None:
    wholefoods = WholeFoodsAdapter(clients)
    smartandfinal = SmartAndFinalAdapter(clients)
    sprouts = SproutsAdapter(clients)
    raleys = RaleysAdapter(clients)
    assert wholefoods._client is clients.shared()
    assert smartandfinal._client is clients.shared()
    # Raley's sends its store cookie per request, which survives a shared jar, so it shares.
    assert raleys._client is clients.shared()
    # A guest session cookie jar is state of its own and must not be shared.
    assert sprouts._client is not clients.shared()
    # A second adapter instance reuses the same persistent client, it does not open another.
    assert SproutsAdapter(clients)._client is sprouts._client


async def test_pool_closes_every_client_once_and_refuses_reuse() -> None:
    pool = RetailerClients()
    shared = pool.shared()
    own = pool.own("sprouts")
    assert pool.shared() is shared  # the shared client is opened once
    await pool.aclose()
    assert shared.is_closed and own.is_closed
    with pytest.raises(RuntimeError):
        pool.shared()


# --------------------------------------------------------------------------- scrape run


async def test_retailers_really_run_concurrently(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = Tracker()
    delay = 0.1
    adapters = [
        FakeAdapter(f"r{index}", [STORE_A], eggs_at("A1"), delay=delay, tracker=tracker)
        for index in range(4)
    ]
    use_adapters(monkeypatch, *adapters)
    started = time.perf_counter()
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    elapsed = time.perf_counter() - started
    assert [run.status for run in runs] == ["succeeded"] * 4
    assert tracker.peak == 4, "all four retailers must be downloading at the same moment"
    # Sequential would cost 4 x delay; half of that still leaves generous slack for a busy box.
    assert elapsed < 2 * delay, "a sequential run would have cost the sum of the delays"


async def test_retailer_concurrency_is_bounded_by_settings(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    settings_override(scrape_max_concurrent_retailers=2)
    tracker = Tracker()
    adapters = [
        FakeAdapter(f"r{index}", [STORE_A], eggs_at("A1"), delay=DELAY, tracker=tracker)
        for index in range(6)
    ]
    use_adapters(monkeypatch, *adapters)
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert [run.status for run in runs] == ["succeeded"] * 6
    assert tracker.peak == 2, "the semaphore must cap concurrent retailers at the configured 2"
    assert tracker.total == 6


async def test_requests_inside_one_retailer_are_concurrent_and_bounded(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    settings_override(scrape_max_concurrent_requests_per_retailer=2, scrape_stores_per_retailer=2)
    catalog = {
        "eggs": {"A1": [], "B1": []},
        "milk": {"A1": [], "B1": []},
        "chicken breast": {"A1": [], "B1": []},
    }
    solo = FakeAdapter("solo", [STORE_A, STORE_B], catalog, delay=DELAY)
    use_adapters(monkeypatch, solo)
    runs = await run_scrape(
        sessionmaker, clients, "94105", None, ["eggs", "milk", "chicken_breast"]
    )
    assert runs[0].status == "succeeded"
    assert solo.own_tracker.total == 6, "2 stores x 3 categories"
    assert solo.own_tracker.peak == 2, "at most two of this retailer's requests at a time"


async def test_a_slow_retailer_times_out_without_taking_the_others_down(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    settings_override(scrape_retailer_timeout_seconds=0.05)
    slow = FakeAdapter("slow", [STORE_A], eggs_at("A1"), delay=5.0)
    fast = FakeAdapter("fast", [STORE_B], eggs_at("B1"))
    use_adapters(monkeypatch, slow, fast)
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert [run.status for run in runs] == ["failed", "succeeded"]
    assert "scrape deadline" in (runs[0].error or "")
    assert runs[1].offers_written == 1, "the healthy retailer's results must be kept"


async def test_overall_deadline_closes_out_unfinished_retailers(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    settings_override(scrape_deadline_seconds=0.05, scrape_retailer_timeout_seconds=30)
    adapters = [FakeAdapter(f"r{i}", [STORE_A], eggs_at("A1"), delay=5.0) for i in range(2)]
    use_adapters(monkeypatch, *adapters)
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert [run.status for run in runs] == ["failed", "failed"]
    assert all("scrape deadline of 0.05s exceeded" in (run.error or "") for run in runs)


# --------------------------------------------------------------------------- async DB


async def test_failed_ingest_rolls_back_its_transaction_and_keeps_earlier_ones(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside one (store, category) transaction must not undo a committed one."""
    catalog = {
        "eggs": {"A1": [listing("a-eggs", "Eggs, 12 CT", "A1", "4.99")]},
        "milk": {"A1": [listing("a-milk", "Whole Milk, 1 GL", "A1", "5.49")]},
    }
    adapter = FakeAdapter("alpha", [STORE_A], catalog)
    use_adapters(monkeypatch, adapter)
    real_ingest = scraper.ingest_listing

    async def fail_on_milk(session, batch, retailer, store, category, item, now, *args):
        if category.key == "milk":
            raise RuntimeError("db exploded")
        return await real_ingest(session, batch, retailer, store, category, item, now, *args)

    monkeypatch.setattr(scraper, "ingest_listing", fail_on_milk)
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs", "milk"])
    assert runs[0].status == "failed"
    assert "RuntimeError: db exploded" in (runs[0].error or "")
    # The eggs transaction committed before milk blew up, so its offer is still there.
    assert await db.scalar(select(func.count()).select_from(Offer)) == 1
    assert await db.scalar(select(func.count()).select_from(Store)) == 1


async def test_failure_in_the_first_transaction_leaves_no_rows_behind(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter(
        "alpha", [STORE_A], {"eggs": {"A1": [listing("a-eggs", "Eggs, 12 CT", "A1", "4.99")]}}
    )
    use_adapters(monkeypatch, adapter)

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(scraper, "ingest_listing", boom)
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert runs[0].status == "failed"
    # upsert_retailer/upsert_store only flushed, so the rollback must remove them.
    assert await db.scalar(select(func.count()).select_from(Store)) == 0
    assert await db.scalar(select(func.count()).select_from(Offer)) == 0
    # The run row itself was committed in its own transaction and survives to report failure.
    assert runs[0].finished_at is not None


# ------------------------------------------------- the budget nested fan-outs actually share


async def test_one_request_budget_covers_a_retailer_that_fans_out_inside_its_search() -> None:
    """Four adapters fan out again inside `search_products`.

    Sizing that inner fan-out from the same setting as the outer one gives limit x limit
    requests in flight, which is not what the setting says and not what an operator turning
    it down is asking for. The budget is per retailer, so it must hold across both levels.
    """
    tracker = Tracker()
    adapter = NestedFakeAdapter(tracker, pages=4)
    categories = [CATEGORIES[key] for key in ("eggs", "milk", "bread")]
    try:
        fetch = await fetch_retailer(
            adapter,  # type: ignore[arg-type]
            "94105",
            categories,
            max_stores=2,
            request_limit=2,
        )
    finally:
        await adapter.aclose()
    assert len(fetch.results) == 6, "2 stores x 3 categories"
    assert tracker.total == 24, "each of the 6 searches fetched 4 pages"
    assert tracker.peak == 2, "2 was the whole retailer's budget, not its allowance per level"


async def test_request_budget_is_per_retailer_not_per_process(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    """Two retailers each get their own budget, so one does not starve the other."""
    settings_override(scrape_max_concurrent_requests_per_retailer=2, scrape_stores_per_retailer=1)
    shared = Tracker()
    first, second = (
        FakeAdapter(f"r{i}", [STORE_A], eggs_at("A1"), delay=DELAY, tracker=shared)
        for i in range(2)
    )
    use_adapters(monkeypatch, first, second)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert shared.peak == 2, "one search each, running at the same time"


# --------------------------------------------------------- partial results and stuck rows


async def test_one_failed_search_does_not_discard_the_retailers_other_categories(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sequential scrape committed each category as it went, so a later failure kept them."""
    catalog = {
        "eggs": {"A1": [listing("a-eggs", "Eggs, 12 CT", "A1", "4.99")]},
        "milk": {"A1": [listing("a-milk", "Whole Milk, 1 GL", "A1", "5.49")]},
        "bread": {"A1": [listing("a-bread", "Wheat Bread, 24 OZ", "A1", "3.49")]},
    }
    adapter = FakeAdapter("alpha", [STORE_A], catalog)
    real_search = adapter.search_products

    async def fail_on_milk(query: str, store):
        if query == CATEGORIES["milk"].search_query:
            raise RuntimeError("upstream exploded")
        return await real_search(query, store)

    adapter.search_products = fail_on_milk  # type: ignore[method-assign]
    use_adapters(monkeypatch, adapter)
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs", "milk", "bread"])
    assert runs[0].status == "failed", "the run still reports the failed category"
    assert "RuntimeError: upstream exploded" in (runs[0].error or "")
    assert runs[0].offers_written == 2, "eggs and bread were still written"
    titles = set(await db.scalars(select(RetailerProduct.title)))
    assert titles == {"Eggs, 12 CT", "Wheat Bread, 24 OZ"}


async def test_a_failed_search_never_expires_the_offers_it_could_not_confirm(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty result from a broken search is not evidence that a store delisted anything."""
    catalog = {"eggs": {"A1": [listing("a-eggs", "Eggs, 12 CT", "A1", "4.99")]}}
    adapter = FakeAdapter("alpha", [STORE_A], catalog)
    use_adapters(monkeypatch, adapter)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert await db.scalar(select(func.count()).select_from(Offer)) == 1

    adapter.fail_with = RuntimeError("upstream exploded")
    runs = await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert runs[0].status == "failed"
    assert await db.scalar(select(func.count()).select_from(Offer)) == 1, (
        "the offer must survive a scrape that never saw the category"
    )


async def test_a_database_failure_never_leaves_runs_stuck_at_running(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run row is inserted up front, so an escape must still close them out."""
    adapters = [FakeAdapter(f"r{index}", [STORE_A], eggs_at("A1")) for index in range(3)]
    use_adapters(monkeypatch, *adapters)
    real_ingest = scraper._ingest_fetch
    calls = 0

    async def explode_on_second(sessionmaker_, run_id, fetch, zip_code):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("connection reset by peer")
        return await real_ingest(sessionmaker_, run_id, fetch, zip_code)

    monkeypatch.setattr(scraper, "_ingest_fetch", explode_on_second)
    with pytest.raises(BaseExceptionGroup) as caught:
        await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert describe_exception(caught.value) == "RuntimeError: connection reset by peer"
    statuses = list(await db.scalars(select(ScrapeRun.status).order_by(ScrapeRun.id)))
    assert statuses == ["succeeded", "failed", "failed"]
    assert "running" not in statuses
    finished = list(await db.scalars(select(ScrapeRun.finished_at)))
    assert all(value is not None for value in finished)


async def test_concurrent_runs_are_serialized_by_default(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs over one ZIP would each expire the offers the other had not confirmed yet."""
    tracker = Tracker()
    adapter = FakeAdapter("alpha", [STORE_A], eggs_at("A1"), delay=DELAY, tracker=tracker)
    use_adapters(monkeypatch, adapter)
    async with asyncio.TaskGroup() as group:
        group.create_task(run_scrape(sessionmaker, clients, "94105", None, ["eggs"]))
        group.create_task(run_scrape(sessionmaker, clients, "94105", None, ["eggs"]))
    assert tracker.total == 2
    assert tracker.peak == 1, "the run gate must keep the two scrapes from overlapping"


async def test_an_adapter_that_shares_one_page_searches_one_category_at_a_time(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    """A browser-backed retailer has one page, and that page is the session.

    Launching a task per (store, category) anyway does not make them concurrent -- they take
    turns on the browser's own lock -- it only has the ones at the back of the queue spend the
    retailer's deadline waiting for a page they never reach. So an adapter may say how many of
    its searches can really run together, and the scrape launches that many.
    """
    settings_override(scrape_max_concurrent_requests_per_retailer=8, scrape_stores_per_retailer=2)
    catalog = {
        "eggs": {"A1": [], "B1": []},
        "milk": {"A1": [], "B1": []},
        "chicken breast": {"A1": [], "B1": []},
    }
    serial = FakeAdapter("serial", [STORE_A, STORE_B], catalog, delay=DELAY)
    serial.max_concurrent_searches = 1  # type: ignore[attr-defined]
    parallel = FakeAdapter("parallel", [STORE_A, STORE_B], catalog, delay=DELAY)
    use_adapters(monkeypatch, serial, parallel)

    runs = await run_scrape(
        sessionmaker, clients, "94105", None, ["eggs", "milk", "chicken_breast"]
    )

    assert [run.status for run in runs] == ["succeeded", "succeeded"]
    assert serial.own_tracker.total == 6, "2 stores x 3 categories, all of them still done"
    assert serial.own_tracker.peak == 1, "and never two at once"
    assert parallel.own_tracker.peak > 1, "an adapter that says nothing is unchanged"


async def test_an_adapter_cannot_raise_its_search_concurrency_above_the_retailer_budget(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    """`max_concurrent_searches` narrows the fan-out; it is not a way to widen it.

    The per-retailer budget is the number this project measured and is fair to point at a
    grocer. An adapter asking for more must not get it.
    """
    settings_override(scrape_max_concurrent_requests_per_retailer=2, scrape_stores_per_retailer=2)
    catalog = {
        "eggs": {"A1": [], "B1": []},
        "milk": {"A1": [], "B1": []},
        "chicken breast": {"A1": [], "B1": []},
    }
    greedy = FakeAdapter("greedy", [STORE_A, STORE_B], catalog, delay=DELAY)
    greedy.max_concurrent_searches = 99  # type: ignore[attr-defined]
    use_adapters(monkeypatch, greedy)

    runs = await run_scrape(
        sessionmaker, clients, "94105", None, ["eggs", "milk", "chicken_breast"]
    )

    assert runs[0].status == "succeeded"
    assert greedy.own_tracker.total == 6
    assert greedy.own_tracker.peak <= 2, "the retailer's budget still decides"
