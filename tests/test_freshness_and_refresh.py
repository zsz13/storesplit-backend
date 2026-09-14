"""Stale-while-revalidate, single-flight refreshes, and the cooldown the API enforces.

The scrape itself is replaced throughout: what is under test is when a refresh is started,
when it is refused and what the API says about it -- never whether a retailer answers.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import Offer, ScrapeRun
from app.normalize.categories import CATEGORIES
from app.schemas import ScrapeRunOut
from app.services import refresh as refresh_module
from app.services import scraper
from app.services.refresh import ALL_CATEGORIES, refresh_key, registry
from app.services.scraper import run_scrape
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeAdapter, two_retailers

# The fake stores live at 94105/94107, so this is the ZIP with data behind it.
ZIP = "94105"
# A ZIP no fake store serves: nothing has ever been collected here.
EMPTY_ZIP = "10001"


@pytest.fixture
async def scraped(sessionmaker, clients, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real offers in the database, collected the way the rest of the suite collects them."""
    adapters: dict[str, FakeAdapter] = {a.slug: a for a in two_retailers()}
    monkeypatch.setattr(scraper, "adapter_slugs", lambda: list(adapters))
    monkeypatch.setattr(scraper, "get_adapter", lambda slug, clients: adapters[slug])
    await run_scrape(sessionmaker, clients, ZIP, None, ["eggs", "milk"])


@pytest.fixture
def scrape_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str] | None]]:
    """Record what a refresh would have collected, without collecting anything."""
    calls: list[tuple[str, list[str] | None]] = []

    async def fake_run_scrape(sessionmaker, clients, zip_code, retailers, categories):
        calls.append((zip_code, categories))
        return []

    monkeypatch.setattr(refresh_module, "run_scrape", fake_run_scrape)
    return calls


@pytest.fixture
def blocking_scrape(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """A refresh that stays in flight until the test releases it."""
    release = asyncio.Event()

    async def fake_run_scrape(sessionmaker, clients, zip_code, retailers, categories):
        await release.wait()
        return []

    monkeypatch.setattr(refresh_module, "run_scrape", fake_run_scrape)
    return release


async def _age_data(db: AsyncSession, minutes: int) -> None:
    """Make the collected data genuinely old -- offers *and* the runs that wrote them.

    Ageing only the offers would build a state that cannot occur: prices from 45 minutes
    ago next to a scrape that finished seconds ago. The cooldown reads `scrape_runs`, and
    would rightly refuse to collect again so soon after a run that had just happened.
    """
    when = datetime.now(UTC) - timedelta(minutes=minutes)
    await db.execute(update(Offer).values(scraped_at=when))
    await db.execute(update(ScrapeRun).values(started_at=when, finished_at=when))
    await db.commit()
    registry.reset()  # and forget the in-process half of the same floor


async def _search(client: AsyncClient, zip_code: str = ZIP, query: str = "eggs") -> dict:
    response = await client.get("/products/search", params={"q": query, "zip_code": zip_code})
    assert response.status_code == 200
    return response.json()


async def _refresh(client: AsyncClient, zip_code: str = ZIP, query: str | None = "eggs") -> dict:
    payload: dict[str, str] = {"zip_code": zip_code}
    if query is not None:
        payload["query"] = query
    response = await client.post("/products/refresh", json=payload)
    assert response.status_code == 200
    return response.json()


async def _drain() -> None:
    """Let a background refresh finish before asserting on what it recorded."""
    for _ in range(200):
        await asyncio.sleep(0)
        if not any(entry.running for entry in registry._entries.values()):
            return


class TestFreshnessReporting:
    async def test_fresh_data_is_not_stale_and_starts_nothing(
        self, client: AsyncClient, scraped, settings_override, scrape_calls
    ) -> None:
        settings_override(search_auto_refresh=True, search_freshness_ttl_seconds=1800)

        freshness = (await _search(client))["freshness"]

        assert freshness["is_stale"] is False
        assert freshness["refreshing"] is False
        assert freshness["ttl_seconds"] == 1800
        assert freshness["age_seconds"] < 60
        assert scrape_calls == []

    async def test_the_ttl_is_configurable(
        self, client: AsyncClient, db, scraped, settings_override, scrape_calls
    ) -> None:
        settings_override(search_auto_refresh=False, search_freshness_ttl_seconds=60)
        await _age_data(db, minutes=5)

        freshness = (await _search(client))["freshness"]

        assert freshness["ttl_seconds"] == 60
        assert freshness["is_stale"] is True

    async def test_a_zip_with_nothing_collected_is_stale(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        freshness = (await _search(client, EMPTY_ZIP))["freshness"]

        assert freshness["last_updated_at"] is None
        assert freshness["is_stale"] is True

    async def test_freshness_ignores_the_availability_filter(
        self, client: AsyncClient, scraped, settings_override, scrape_calls
    ) -> None:
        """How recently a scrape ran is a fact about the scrape, not about what is shown."""
        in_stock = await _search(client)
        every = (
            await client.get(
                "/products/search", params={"q": "eggs", "zip_code": ZIP, "availability": "all"}
            )
        ).json()

        assert in_stock["freshness"]["last_updated_at"] == every["freshness"]["last_updated_at"]


class TestStaleWhileRevalidate:
    async def test_stale_data_is_returned_and_a_refresh_starts_behind_it(
        self, client: AsyncClient, db, scraped, settings_override, scrape_calls
    ) -> None:
        settings_override(search_auto_refresh=True, search_freshness_ttl_seconds=1800)
        await _age_data(db, minutes=45)

        body = await _search(client)

        # The whole point: the answer arrives complete, out of the data already collected.
        assert body["products"], "stale results must still be returned, not withheld"
        assert body["freshness"]["is_stale"] is True
        assert body["freshness"]["refreshing"] is True
        await _drain()
        assert scrape_calls == [(ZIP, ["eggs"])], "only the category on screen is collected"

    async def test_a_second_search_joins_the_refresh_already_running(
        self, client: AsyncClient, db, scraped, settings_override, blocking_scrape
    ) -> None:
        settings_override(search_auto_refresh=True, search_freshness_ttl_seconds=1800)
        await _age_data(db, minutes=45)

        first = await _search(client)
        second = await _search(client)

        assert first["freshness"]["refreshing"] is True
        assert second["freshness"]["refreshing"] is True
        blocking_scrape.set()

    async def test_auto_refresh_can_be_switched_off(
        self, client: AsyncClient, db, scraped, settings_override, scrape_calls
    ) -> None:
        settings_override(search_auto_refresh=False, search_freshness_ttl_seconds=1800)
        await _age_data(db, minutes=45)

        freshness = (await _search(client))["freshness"]

        assert freshness["is_stale"] is True
        assert freshness["refreshing"] is False
        assert scrape_calls == []

    async def test_a_query_matching_no_staple_never_auto_refreshes(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        """There is no category to scope a scrape to, and all seven is not what was asked."""
        settings_override(search_auto_refresh=True)

        body = await _search(client, query="kumquat")

        assert body["category"] is None
        assert body["freshness"]["refreshing"] is False
        assert scrape_calls == []


class TestManualRefresh:
    async def test_it_collects_the_category_on_screen(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        body = await _refresh(client)

        assert body["state"] == "started"
        assert body["category"] == "eggs"
        assert body["freshness"]["refreshing"] is True
        await _drain()
        assert scrape_calls == [(ZIP, ["eggs"])]

    async def test_without_a_query_it_collects_every_category(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        """What the "nothing collected for this ZIP yet" state needs."""
        body = await _refresh(client, query=None)

        assert body["state"] == "started"
        assert body["category"] is None
        await _drain()
        assert scrape_calls == [(ZIP, None)]

    async def test_a_second_press_while_running_starts_nothing(
        self, client: AsyncClient, settings_override, blocking_scrape
    ) -> None:
        first = await _refresh(client)
        second = await _refresh(client)

        assert first["state"] == "started"
        assert second["state"] == "already_running"
        assert second["freshness"]["refreshing"] is True
        assert second["freshness"]["can_refresh"] is False
        blocking_scrape.set()

    async def test_the_cooldown_is_enforced_by_the_api(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)

        first = await _refresh(client)
        await _drain()
        second = await _refresh(client)

        assert first["state"] == "started"
        assert second["state"] == "cooling_down"
        assert second["freshness"]["can_refresh"] is False
        assert 0 < second["freshness"]["refresh_available_in_seconds"] <= 300
        # The enforcement: no second scrape was started, whatever the client believed.
        assert len(scrape_calls) == 1

    async def test_the_cooldown_survives_a_restart_of_the_process(
        self, client: AsyncClient, db: AsyncSession, settings_override, scrape_calls
    ) -> None:
        """A reload or a second tab cannot re-arm the button, and neither can a redeploy.

        `scrape_runs` is the durable floor: everything this process remembered is thrown
        away here, the way a restart would throw it away, and the cooldown still holds.
        """
        settings_override(search_refresh_cooldown_seconds=300)
        db.add(
            ScrapeRun(
                retailer_slug="alpha",
                zip_code=ZIP,
                categories=["eggs", "milk"],
                status="succeeded",
                started_at=datetime.now(UTC) - timedelta(seconds=30),
            )
        )
        await db.commit()
        registry.reset()

        body = await _refresh(client)

        assert body["state"] == "cooling_down"
        assert 0 < body["freshness"]["refresh_available_in_seconds"] <= 300
        assert scrape_calls == []

    async def test_an_older_run_does_not_hold_the_cooldown_open(
        self, client: AsyncClient, db: AsyncSession, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)
        db.add(
            ScrapeRun(
                retailer_slug="alpha",
                zip_code=ZIP,
                categories=["eggs"],
                status="succeeded",
                started_at=datetime.now(UTC) - timedelta(seconds=600),
            )
        )
        await db.commit()
        registry.reset()

        assert (await _refresh(client))["state"] == "started"

    async def test_a_run_for_another_category_leaves_this_key_free(
        self, client: AsyncClient, db: AsyncSession, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)
        db.add(
            ScrapeRun(
                retailer_slug="alpha",
                zip_code=ZIP,
                categories=["milk"],
                status="succeeded",
                started_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
        await db.commit()
        registry.reset()

        assert (await _refresh(client))["state"] == "started"

    async def test_another_zip_has_its_own_cooldown(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)

        first = await _refresh(client, ZIP)
        await _drain()
        other = await _refresh(client, EMPTY_ZIP)

        assert first["state"] == "started"
        assert other["state"] == "started"

    async def test_a_manual_refresh_also_cools_down_the_automatic_one(
        self, client: AsyncClient, db, scraped, settings_override, scrape_calls
    ) -> None:
        """One cooldown, shared. Otherwise the automatic path would collect again at once."""
        settings_override(
            search_auto_refresh=True,
            search_freshness_ttl_seconds=1800,
            search_refresh_cooldown_seconds=300,
        )
        await _age_data(db, minutes=45)

        await _refresh(client)
        await _drain()
        body = await _search(client)

        assert body["freshness"]["is_stale"] is True
        assert len(scrape_calls) == 1, "the search must not start a second scrape"


class TestSingleFlight:
    """The property the whole mechanism is named for, tested the only way that can fail.

    Sequential calls prove nothing here: the second one always sees a finished first. These
    dispatch concurrently, so the second reaches `ensure_refresh` while the first is inside
    the `await` that reads the durable cooldown floor -- exactly the window the lock exists
    to close. Remove `async with self._lock` from `ensure_refresh` and these fail.
    """

    async def test_two_concurrent_manual_refreshes_start_one_scrape(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)
        payload = {"zip_code": ZIP, "query": "eggs"}

        first, second = await asyncio.gather(
            client.post("/products/refresh", json=payload),
            client.post("/products/refresh", json=payload),
        )

        states = [first.json()["state"], second.json()["state"]]
        assert states.count("started") == 1, f"exactly one caller may start a scrape, got {states}"
        assert set(states) <= {"started", "already_running", "cooling_down"}, states
        await _drain()
        assert len(scrape_calls) == 1, (
            f"two overlapping runs over one ZIP would each expire what the other had not "
            f"confirmed; {len(scrape_calls)} were started"
        )

    async def test_many_concurrent_refreshes_still_start_one_scrape(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)
        payload = {"zip_code": ZIP, "query": "eggs"}

        responses = await asyncio.gather(
            *(client.post("/products/refresh", json=payload) for _ in range(8))
        )

        assert [r.json()["state"] for r in responses].count("started") == 1
        await _drain()
        assert len(scrape_calls) == 1

    async def test_concurrent_searches_over_stale_data_start_one_scrape(
        self, client: AsyncClient, db, scraped, settings_override, scrape_calls
    ) -> None:
        """The automatic path races the same way: every stale search wants to revalidate."""
        settings_override(
            search_auto_refresh=True,
            search_freshness_ttl_seconds=1800,
            search_refresh_cooldown_seconds=300,
        )
        await _age_data(db, minutes=45)
        params = {"q": "eggs", "zip_code": ZIP}

        await asyncio.gather(*(client.get("/products/search", params=params) for _ in range(6)))

        await _drain()
        assert len(scrape_calls) == 1

    async def test_concurrent_refreshes_for_different_keys_are_independent(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        """The lock must not serialize unrelated keys into one refresh."""
        settings_override(search_refresh_cooldown_seconds=300)

        responses = await asyncio.gather(
            client.post("/products/refresh", json={"zip_code": ZIP, "query": "eggs"}),
            client.post("/products/refresh", json={"zip_code": ZIP, "query": "milk"}),
        )

        assert [r.json()["state"] for r in responses] == ["started", "started"]
        await _drain()
        assert sorted(categories for _, categories in scrape_calls) == [["eggs"], ["milk"]]


class TestShutdown:
    async def test_aclose_stops_a_refresh_that_is_still_running(
        self, client: AsyncClient, settings_override, blocking_scrape
    ) -> None:
        """What the application lifespan calls on the way out.

        The tasks have to be *finished* before their HTTP clients and database engine are
        closed under them, so this cancels and then awaits -- and must not hang on a scrape
        that would otherwise never return.
        """
        assert (await _refresh(client))["state"] == "started"
        assert any(entry.running for entry in registry._entries.values())

        await asyncio.wait_for(registry.aclose(), timeout=5)

        assert not any(entry.running for entry in registry._entries.values())
        blocking_scrape.set()


class TestRefreshFailures:
    async def test_a_failed_refresh_keeps_the_last_results_and_reports_the_error(
        self, client: AsyncClient, db, scraped, settings_override, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def exploding_scrape(sessionmaker, clients, zip_code, retailers, categories):
            raise RuntimeError("retailer unreachable")

        monkeypatch.setattr(refresh_module, "run_scrape", exploding_scrape)
        await _age_data(db, minutes=45)

        await _refresh(client)
        await _drain()
        body = await _search(client)

        assert body["products"], "a failed refresh must not take the last valid data away"
        assert body["freshness"]["refreshing"] is False
        assert "retailer unreachable" in body["freshness"]["last_error"]

    async def test_a_partly_failed_run_names_the_retailers_that_failed(
        self, client: AsyncClient, settings_override, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def partial_scrape(sessionmaker, clients, zip_code, retailers, categories):
            now = datetime.now(UTC)
            common = {"zip_code": zip_code, "categories": ["eggs"], "started_at": now}
            return [
                ScrapeRunOut(
                    id=1,
                    retailer_slug="alpha",
                    status="succeeded",
                    products_seen=2,
                    offers_written=2,
                    **common,
                ),
                ScrapeRunOut(
                    id=2,
                    retailer_slug="beta",
                    status="failed",
                    products_seen=0,
                    offers_written=0,
                    error="timeout",
                    **common,
                ),
            ]

        monkeypatch.setattr(refresh_module, "run_scrape", partial_scrape)
        settings_override(search_refresh_cooldown_seconds=300)

        await _refresh(client)
        await _drain()
        body = await _refresh(client)

        assert body["state"] == "cooling_down"
        assert "beta" in body["freshness"]["last_error"]


class TestGlobalBudget:
    """The per-key cooldown is not a budget: the key is chosen by the caller.

    Both entry points are unauthenticated by design, so a thousand distinct ZIPs would be a
    thousand uncooled keys, each queueing a collection from ten real retailers. What has to
    be bounded is the total in flight.
    """

    async def test_a_malformed_zip_is_refused_before_it_becomes_a_key(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        for bad in ("abcde", "9410a", "  941", "94105x", "../../x"):
            response = await client.post(
                "/products/refresh", json={"zip_code": bad, "query": "eggs"}
            )
            assert response.status_code == 422, f"{bad!r} was accepted"
        assert scrape_calls == []

    async def test_a_malformed_zip_cannot_trigger_a_refresh_through_search_either(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        settings_override(search_auto_refresh=True)
        response = await client.get("/products/search", params={"q": "eggs", "zip_code": "abcde"})
        assert response.status_code == 422
        assert scrape_calls == []

    async def test_distinct_zips_cannot_queue_more_than_the_budget(
        self, client: AsyncClient, settings_override, blocking_scrape
    ) -> None:
        settings_override(search_max_concurrent_refreshes=2, search_refresh_cooldown_seconds=0)
        zips = [f"9410{n}" for n in range(6)]

        states = [(await _refresh(client, zip_code=z, query="eggs"))["state"] for z in zips]

        assert states.count("started") == 2, (
            f"the budget is two refreshes in flight, not one per key: {states}"
        )
        assert set(states[2:]) == {"cooling_down"}, states
        blocking_scrape.set()

    async def test_the_registry_does_not_grow_without_bound(
        self, client: AsyncClient, settings_override, scrape_calls
    ) -> None:
        settings_override(
            search_refresh_registry_max_keys=16,
            search_refresh_cooldown_seconds=0,
            search_max_concurrent_refreshes=16,
        )
        for n in range(40):
            await _refresh(client, zip_code=f"9{n:04d}", query="eggs")
            await _drain()

        assert len(registry._entries) <= 16, len(registry._entries)


class TestOverlappingKeys:
    async def test_a_whole_zip_refresh_blocks_a_category_refresh_while_it_runs(
        self, client: AsyncClient, settings_override, blocking_scrape
    ) -> None:
        """Both cover the same offers, and a scrape expires what it did not confirm."""
        settings_override(search_refresh_cooldown_seconds=0)

        assert (await _refresh(client, query=None))["state"] == "started"
        assert (await _refresh(client, query="eggs"))["state"] == "already_running"
        blocking_scrape.set()

    async def test_a_category_refresh_blocks_a_whole_zip_refresh_while_it_runs(
        self, client: AsyncClient, settings_override, blocking_scrape
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=0)

        assert (await _refresh(client, query="eggs"))["state"] == "started"
        assert (await _refresh(client, query=None))["state"] == "already_running"
        blocking_scrape.set()

    async def test_two_unrelated_categories_still_run_together(
        self, client: AsyncClient, settings_override, blocking_scrape
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=0, search_max_concurrent_refreshes=4)

        assert (await _refresh(client, query="eggs"))["state"] == "started"
        assert (await _refresh(client, query="milk"))["state"] == "started"
        blocking_scrape.set()


class TestCooldownScope:
    async def test_a_single_category_run_does_not_block_collecting_the_whole_zip(
        self, client: AsyncClient, db: AsyncSession, settings_override, scrape_calls
    ) -> None:
        """Collecting eggs five minutes ago says nothing about milk."""
        settings_override(search_refresh_cooldown_seconds=300)
        db.add(
            ScrapeRun(
                retailer_slug="alpha",
                zip_code=ZIP,
                categories=["eggs"],
                status="succeeded",
                started_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
        await db.commit()
        registry.reset()

        assert (await _refresh(client, query=None))["state"] == "started"

    async def test_a_whole_zip_run_does_cool_down_every_category(
        self, client: AsyncClient, db: AsyncSession, settings_override, scrape_calls
    ) -> None:
        settings_override(search_refresh_cooldown_seconds=300)
        db.add(
            ScrapeRun(
                retailer_slug="alpha",
                zip_code=ZIP,
                categories=list(CATEGORIES),
                status="succeeded",
                started_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
        await db.commit()
        registry.reset()

        assert (await _refresh(client, query="eggs"))["state"] == "cooling_down"
        assert (await _refresh(client, query=None))["state"] == "cooling_down"


class TestRefreshKey:
    def test_a_zip_is_normalized_to_five_digits(self) -> None:
        assert refresh_key("94105-4321", "eggs") == ("94105", "eggs")
        assert refresh_key(" 94105 ", None) == ("94105", ALL_CATEGORIES)
