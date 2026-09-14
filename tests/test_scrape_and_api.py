"""Vertical slice: fake adapters -> scrape service -> database -> search/offers/basket API."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from app.db.models import (
    STOCK_STATUS_MAX,
    CanonicalProduct,
    Offer,
    PriceHistory,
    RetailerProduct,
    ScrapeRun,
)
from app.normalize.categories import CATEGORIES
from app.retailers.base import StoreLocation
from app.services import scraper
from app.services.scraper import run_scrape, scrape_retailer
from app.services.stores import stores_near
from httpx import AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.fakes import STORE_A, STORE_B, FakeAdapter, listing, two_retailers


def use_adapters(monkeypatch: pytest.MonkeyPatch, *adapters: FakeAdapter) -> None:
    """Point the scrape service at fake adapters; the client pool is ignored by them."""
    registry = {adapter.slug: adapter for adapter in adapters}
    monkeypatch.setattr(scraper, "adapter_slugs", lambda: list(registry))
    monkeypatch.setattr(scraper, "get_adapter", lambda slug, clients: registry[slug])


@pytest.fixture
async def scraped(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeAdapter, FakeAdapter]:
    alpha, beta = two_retailers()
    use_adapters(monkeypatch, alpha, beta)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs", "chicken_breast", "milk"])
    return alpha, beta


async def test_scrape_persists_products_offers_and_runs(db: AsyncSession, scraped) -> None:
    runs = list(await db.scalars(select(ScrapeRun)))
    assert [r.status for r in runs] == ["succeeded", "succeeded"]
    assert sum(r.products_seen for r in runs) == 9
    assert sum(r.offers_written for r in runs) == 7  # kimchi, liquid egg whites, ... skipped
    titles = set(await db.scalars(select(RetailerProduct.title)))
    assert "Kimchi, 16 OZ" not in titles and "Liquid Egg Whites" not in titles
    assert await db.scalar(select(func.count()).select_from(Offer)) == 7
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 7


async def test_matching_merges_same_product_and_keeps_sizes_apart(
    db: AsyncSession, scraped
) -> None:
    eggs = list(
        await db.scalars(
            select(CanonicalProduct)
            .where(CanonicalProduct.category == "eggs")
            .options(selectinload(CanonicalProduct.retailer_products))
        )
    )
    dozen = [p for p in eggs if p.count == 12]
    assert len(dozen) == 1, "Farm Co 12 ct from both retailers must merge into one canonical"
    assert len(dozen[0].retailer_products) == 2
    assert dozen[0].gtin == "00001111060903"  # normalized to GTIN-14
    assert {p.count for p in eggs} == {12, 18}
    beta_eggs = await db.scalar(
        select(RetailerProduct).where(RetailerProduct.retailer_sku == "b-eggs-12")
    )
    assert beta_eggs is not None and beta_eggs.match_status == "auto"


async def test_unit_prices_are_computed_per_comparison_unit(db: AsyncSession, scraped) -> None:
    rows = await db.scalars(select(Offer).options(selectinload(Offer.retailer_product)))
    offers = {o.retailer_product.retailer_sku: o for o in rows}
    assert offers["a-eggs-12"].unit_price == Decimal("0.4158")  # $/egg
    assert offers["a-milk"].unit_price == Decimal("5.49")  # $/gal
    assert offers["b-milk"].unit_price == Decimal("6.58")  # half gallon -> $/gal
    assert offers["a-chx"].unit_price == Decimal("6.99")  # per lb by weight
    assert offers["b-chx"].unit_price == Decimal("7.50")  # 22.50 / 3 lb


async def test_rescrape_is_idempotent_and_records_history_on_change(
    db: AsyncSession, scraped
) -> None:
    alpha, _ = scraped
    before = await db.scalar(select(func.count()).select_from(Offer))
    alpha._catalog["eggs"]["A1"][0] = listing(
        "a-eggs-12", "Large Grade A Eggs, 12 CT", "A1", "5.49", brand="Farm Co"
    )
    await scrape_retailer(db, alpha, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(Offer)) == before
    offer = await db.scalar(
        select(Offer)
        .join(Offer.retailer_product)
        .where(RetailerProduct.retailer_sku == "a-eggs-12")
    )
    assert offer is not None and offer.price == Decimal("5.49")
    history = list(
        await db.scalars(
            select(PriceHistory)
            .where(PriceHistory.retailer_product_id == offer.retailer_product_id)
            .order_by(PriceHistory.id)
        )
    )
    assert [h.price for h in history] == [Decimal("4.99"), Decimal("5.49")]


async def test_unconfigured_adapter_is_skipped(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = FakeAdapter("ghost", [STORE_A], {}, configured=False)
    use_adapters(monkeypatch, ghost)
    runs = await run_scrape(sessionmaker, clients, "94105", ["ghost"], ["eggs"])
    assert runs[0].status == "skipped" and "not configured" in (runs[0].error or "")


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_search_api_orders_by_unit_price_and_flags_cheapest(
    client: AsyncClient, scraped
) -> None:
    response = await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})
    body = response.json()
    assert body["category"] == "eggs" and body["comparison_unit"] == "egg"
    assert [s["name"] for s in body["stores"]] == ["Alpha Market Downtown", "Beta Foods SoMa"]
    products = body["products"]
    assert len(products) == 2
    first = products[0]
    assert first["count"] == 12 and len(first["offers"]) == 2
    assert (
        first["offers"][0]["price"] == "3.99" and first["offers"][0]["is_cheapest_overall"] is True
    )
    assert first["offers"][0]["is_cheapest_for_product"] is True
    assert first["offers"][1]["is_cheapest_for_product"] is False
    assert body["cheapest_offer_id"] == first["offers"][0]["id"]
    assert body["last_updated_at"] is not None
    unit_prices = [Decimal(p["offers"][0]["unit_price"]) for p in products]
    assert unit_prices == sorted(unit_prices)


async def test_search_api_unknown_zip_returns_no_stores(client: AsyncClient, scraped) -> None:
    response = await client.get("/products/search", params={"q": "eggs", "zip_code": "10001"})
    body = response.json()
    assert body["stores"] == [] and body["products"] == []


async def test_search_api_free_text_fallback(client: AsyncClient, scraped) -> None:
    response = await client.get("/products/search", params={"q": "value pack", "zip_code": "94105"})
    body = response.json()
    assert body["category"] is None and len(body["products"]) == 1
    assert (
        body["products"][0]["offers"][0]["title"] == "Boneless Skinless Chicken Breast Value Pack"
    )
    assert body["cheapest_offer_id"] is None  # unit prices are not comparable across categories


async def test_product_offers_api(client: AsyncClient, scraped) -> None:
    search = (
        await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})
    ).json()
    product_id = search["products"][0]["id"]
    body = (await client.get(f"/products/{product_id}/offers")).json()
    assert body["product"]["id"] == product_id and len(body["product"]["offers"]) == 2
    # History no longer rides along here: it is a series per store, not a flat list, and it
    # has its own endpoint (see tests/test_price_history.py).
    assert "price_history" not in body
    assert (await client.get("/products/999999/offers")).status_code == 404


async def test_scrape_api_validates_options(client: AsyncClient) -> None:
    response = await client.post("/scrape", json={"zip_code": "94105", "retailers": ["nope"]})
    assert response.status_code == 422
    assert response.json()["detail"]["unknown_retailers"] == ["nope"]
    options = (await client.get("/scrape/options")).json()
    assert "wholefoods" in options["retailers"] and "eggs" in options["categories"]


async def test_basket_compare(client: AsyncClient, scraped) -> None:
    payload = {
        "zip_code": "94105",
        "items": [
            {"query": "eggs", "quantity": 60, "unit": "count"},
            {"query": "chicken breast", "quantity": 5, "unit": "lb"},
            {"query": "milk", "quantity": 1, "unit": "gal"},
        ],
    }
    body = (await client.post("/basket/compare", json=payload)).json()
    assert [i["matching_products"] for i in body["items"]] == [2, 2, 2]
    single = body["cheapest_single_store"]
    split = body["cheapest_split"]
    assert single["covers_all_items"] and len(body["single_store_options"]) == 2
    # Alpha: eggs min(5x4.99=24.95, 4x6.49=25.96)=24.95, chicken 5x6.99=34.95, milk 5.49 -> 65.39
    # Beta:  eggs 5x3.99=19.95, chicken 2x22.50=45.00, milk 2x3.29=6.58 -> 71.53
    # Split: eggs@Beta 19.95 + chicken@Alpha 34.95 + milk@Alpha 5.49 -> 60.39
    assert single["store"]["name"] == "Alpha Market Downtown" and single["total"] == "65.39"
    assert body["single_store_options"][1]["total"] == "71.53"
    assert (
        split["total"] == "60.39" and body["savings"] == "5.00" and body["savings_percent"] == "7.6"
    )
    assert {line["offer"]["store"]["name"] for line in split["lines"]} == {
        "Alpha Market Downtown",
        "Beta Foods SoMa",
    }
    eggs_line = next(line for line in split["lines"] if line["query"] == "eggs")
    assert eggs_line["packs"] == 5 and eggs_line["line_total"] == "19.95"
    assert body["last_updated_at"] and body["oldest_updated_at"]


async def test_basket_dozen_unit_and_errors(client: AsyncClient, scraped) -> None:
    ok = await client.post(
        "/basket/compare",
        json={"zip_code": "94105", "items": [{"query": "eggs", "quantity": 2, "unit": "dozen"}]},
    )
    assert ok.status_code == 200 and ok.json()["items"][0]["needed_quantity"] == "24"
    echoed = ok.json()["items"][0]["cheapest"]
    assert echoed["requested_quantity"] == "2" and echoed["requested_unit"] == "dozen"
    bad_unit = await client.post(
        "/basket/compare",
        json={"zip_code": "94105", "items": [{"query": "eggs", "quantity": 2, "unit": "lb"}]},
    )
    assert bad_unit.status_code == 422
    unknown = await client.post(
        "/basket/compare",
        json={"zip_code": "94105", "items": [{"query": "caviar", "quantity": 1, "unit": "oz"}]},
    )
    assert unknown.status_code == 422


async def test_scrape_failure_is_isolated_per_retailer(
    sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = two_retailers()
    alpha.fail_with = RuntimeError("upstream exploded")
    use_adapters(monkeypatch, alpha, beta)
    runs = await run_scrape(sessionmaker, clients, "94105", ["alpha", "beta"], ["eggs"])
    assert [r.status for r in runs] == ["failed", "succeeded"]
    assert "RuntimeError: upstream exploded" in (runs[0].error or "")
    assert runs[1].offers_written == 1


async def test_scrape_api_happy_path(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha, beta = two_retailers()
    use_adapters(monkeypatch, alpha, beta)
    monkeypatch.setattr("app.api.scrape.adapter_slugs", lambda: [alpha.slug, beta.slug])
    response = await client.post(
        "/scrape", json={"zip_code": "94105", "categories": ["eggs", "milk"]}
    )
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert [r["retailer_slug"] for r in runs] == ["alpha", "beta"]
    assert all(r["status"] == "succeeded" and r["finished_at"] for r in runs)
    assert sum(r["offers_written"] for r in runs) == 5  # alpha: 2 eggs + milk; beta: eggs + milk
    assert runs[0]["categories"] == ["eggs", "milk"]


async def test_stale_offers_expire_after_rescrape(db: AsyncSession, scraped) -> None:
    alpha, _ = scraped
    alpha._catalog["eggs"]["A1"] = [alpha._catalog["eggs"]["A1"][0]]  # 18 ct delisted
    await scrape_retailer(db, alpha, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    rows = await db.scalars(select(Offer).options(selectinload(Offer.retailer_product)))
    titles = {o.retailer_product.title for o in rows}
    assert "Large Grade A Eggs, 18 CT" not in titles
    assert "Large Grade A Eggs, 12 CT" in titles
    assert "Whole Milk, 1 GL" in titles  # other categories at the store are untouched
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 7  # history survives


async def test_sku_matching_two_categories_keeps_first_category(db: AsyncSession) -> None:
    hybrid = listing("a-rb", "Brown Rice Bread, 24 OZ", "A1", "6.00", brand="Grainy")
    adapter = FakeAdapter("alpha", [STORE_A], {"rice": {"A1": [hybrid]}, "bread": {"A1": [hybrid]}})
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["rice"], CATEGORIES["bread"]], 2)
    await db.commit()
    offers = list(
        await db.scalars(
            select(Offer).options(
                selectinload(Offer.retailer_product).selectinload(RetailerProduct.canonical_product)
            )
        )
    )
    assert len(offers) == 1
    product = offers[0].retailer_product.canonical_product
    assert product is not None and product.category == "rice"
    assert offers[0].unit_price == Decimal("4.00") and offers[0].unit_price_unit == "lb"


async def test_canonical_attributes_are_stored(db: AsyncSession, scraped) -> None:
    eggs = await db.scalar(select(CanonicalProduct).where(CanonicalProduct.count == 18))
    assert eggs is not None
    assert eggs.attributes["size_grade"] == "large" and eggs.attributes["grade"] == "a"
    assert eggs.attributes["organic"] is False and eggs.attributes["sold_by"] == "unit"


async def test_store_in_another_zip_prefix_is_still_searchable_when_it_is_near(
    db: AsyncSession,
) -> None:
    """A retailer's own locator crosses ZIP prefixes: 94611 is `946`, the search ZIP is `941`.

    Distance, not the prefix, decides -- so this Oakland store answers a San Francisco ZIP,
    while a store in Los Angeles (`test_stores_near.py`) does not.
    """
    near = StoreLocation(
        "F1",
        "Oakland Foods",
        city="Oakland",
        zip_code="94611",
        latitude=37.8272,
        longitude=-122.2515,
    )
    adapter = FakeAdapter(
        "far", [near], {"eggs": {"F1": [listing("f-eggs", "Eggs, 12 CT", "F1", "2.99")]}}
    )

    async def only_near(zip_code: str) -> list[StoreLocation]:
        return [near]

    adapter.find_stores = only_near  # type: ignore[method-assign]
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert [s.name for s in await stores_near(db, "94105")] == ["Oakland Foods"]
    assert await stores_near(db, "10001") == []  # a real ZIP, 2500 miles away
    assert await stores_near(db, "M5R 3B4") == []  # non-numeric input never raises


async def test_basket_duplicate_queries_count_separately(client: AsyncClient, scraped) -> None:
    body = (
        await client.post(
            "/basket/compare",
            json={
                "zip_code": "94105",
                "items": [
                    {"query": "eggs", "quantity": 12, "unit": "count"},
                    {"query": "eggs", "quantity": 60, "unit": "count"},
                ],
            },
        )
    ).json()
    assert [i["needed_quantity"] for i in body["items"]] == ["12", "60"]
    beta = next(o for o in body["single_store_options"] if o["store"]["name"] == "Beta Foods SoMa")
    assert [line["packs"] for line in beta["lines"]] == [1, 5]
    assert beta["total"] == "23.94"  # 3.99 + 5 * 3.99
    assert body["cheapest_split"]["total"] == "23.94"
    assert [len(i["options"]) for i in body["items"]] == [2, 2]


async def test_basket_partial_and_missing_coverage(
    client: AsyncClient, db: AsyncSession, scraped
) -> None:
    alpha, _ = scraped
    alpha._catalog["bread"] = {
        "A1": [listing("a-bread", "Wheat Bread, 24 OZ", "A1", "3.49", brand="Baker")]
    }
    await scrape_retailer(db, alpha, "94105", [CATEGORIES["bread"]], 2)
    await db.commit()
    items = [
        {"query": "eggs", "quantity": 12, "unit": "count"},
        {"query": "bread", "quantity": 24, "unit": "oz"},
    ]
    body = (await client.post("/basket/compare", json={"zip_code": "94105", "items": items})).json()
    beta = next(o for o in body["single_store_options"] if o["store"]["name"] == "Beta Foods SoMa")
    assert beta["covers_all_items"] is False and beta["missing_items"] == ["bread"]
    assert body["cheapest_single_store"]["store"]["name"] == "Alpha Market Downtown"
    assert body["cheapest_split"]["total"] == "7.48"  # eggs@Beta 3.99 + bread@Alpha 3.49
    assert body["savings"] == "1.00"  # Alpha alone: 4.99 + 3.49
    # an item nobody stocks: no split basket, no single-store basket, no savings
    items = [
        {"query": "eggs", "quantity": 12, "unit": "count"},
        {"query": "bananas", "quantity": 2, "unit": "lb"},
    ]
    body = (await client.post("/basket/compare", json={"zip_code": "94105", "items": items})).json()
    assert body["items"][1]["cheapest"] is None and body["items"][1]["matching_products"] == 0
    assert body["cheapest_split"] is None and body["cheapest_single_store"] is None
    assert body["savings"] is None and body["single_store_options"][0]["missing_items"] == [
        "bananas"
    ]


async def test_split_prefers_cheapest_single_store_on_ties(
    client: AsyncClient, db: AsyncSession
) -> None:
    same_a = FakeAdapter(
        "a",
        [STORE_A],
        {
            "eggs": {"A1": [listing("a1", "Eggs, 12 CT", "A1", "4.00")]},
            "milk": {"A1": [listing("a2", "Whole Milk, 1 GL", "A1", "5.00")]},
        },
    )
    same_b = FakeAdapter(
        "b",
        [STORE_B],
        {
            "eggs": {"B1": [listing("b1", "Eggs, 12 CT", "B1", "4.00")]},
            "milk": {"B1": [listing("b2", "Whole Milk, 1 GL", "B1", "5.00")]},
        },
    )
    for adapter in (same_a, same_b):
        await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"], CATEGORIES["milk"]], 2)
    await db.commit()
    items = [
        {"query": "eggs", "quantity": 12, "unit": "count"},
        {"query": "milk", "quantity": 1, "unit": "gal"},
    ]
    body = (await client.post("/basket/compare", json={"zip_code": "94105", "items": items})).json()
    single_store = body["cheapest_single_store"]["store"]["id"]
    assert [line["offer"]["store"]["id"] for line in body["cheapest_split"]["lines"]] == [
        single_store,
        single_store,
    ]
    assert len(body["cheapest_split"]["stores"]) == 1 and body["savings"] == "0.00"


# ------------------------------------------------- what one (store, category) batch loads


async def test_one_sku_at_two_stores_keeps_an_offer_per_store(db: AsyncSession) -> None:
    """The batch's offer map is per store; two stores must not collapse into one row."""
    catalog = {
        "eggs": {
            "A1": [listing("shared-eggs", "Eggs, 12 CT", "A1", "4.99")],
            "B1": [listing("shared-eggs", "Eggs, 12 CT", "B1", "5.99")],
        }
    }
    adapter = FakeAdapter("alpha", [STORE_A, STORE_B], catalog)
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(RetailerProduct)) == 1
    offers = list(await db.scalars(select(Offer).order_by(Offer.price)))
    assert [o.price for o in offers] == [Decimal("4.99"), Decimal("5.99")]
    assert len({o.store_id for o in offers}) == 2


async def test_a_sku_repeated_in_one_result_writes_one_offer(db: AsyncSession) -> None:
    """A retailer that returns the same product twice must not create two rows for it."""
    catalog = {
        "eggs": {
            "A1": [
                listing("dupe", "Eggs, 12 CT", "A1", "4.99"),
                listing("dupe", "Eggs, 12 CT", "A1", "5.49"),  # same SKU, later price wins
            ]
        }
    }
    adapter = FakeAdapter("alpha", [STORE_A], catalog)
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(RetailerProduct)) == 1
    offer = await db.scalar(select(Offer))
    assert offer is not None and offer.price == Decimal("5.49")
    # Two different prices for one product in one batch: both belong in the history.
    prices = list(await db.scalars(select(PriceHistory.price).order_by(PriceHistory.id)))
    assert prices == [Decimal("4.99"), Decimal("5.49")]


async def test_a_sku_repeated_at_the_same_price_adds_one_history_row(db: AsyncSession) -> None:
    catalog = {
        "eggs": {
            "A1": [
                listing("dupe", "Eggs, 12 CT", "A1", "4.99"),
                listing("dupe", "Eggs, 12 CT", "A1", "4.99"),
            ]
        }
    }
    adapter = FakeAdapter("alpha", [STORE_A], catalog)
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1


async def test_a_gtin_backfilled_mid_batch_matches_the_next_listing(db: AsyncSession) -> None:
    """The batch's canonical index must reflect a GTIN written earlier in the same batch.

    The third listing conflicts on the "organic" attribute, so the fuzzy path refuses it; only
    the GTIN that the second listing backfilled onto the canonical can merge it.
    """
    catalog = {
        "eggs": {
            "A1": [
                listing("no-gtin", "Large Grade A Eggs, 12 CT", "A1", "4.99", brand="Farm Co"),
                listing(
                    "with-gtin",
                    "Large Grade A Eggs, 12 CT",
                    "A1",
                    "5.09",
                    brand="Farm Co",
                    gtin="0001111060903",
                ),
                listing(
                    "organic",
                    "Organic Pasture Raised Eggs, 12 CT",
                    "A1",
                    "7.99",
                    brand="Farm Co",
                    gtin="0001111060903",
                ),
            ]
        }
    }
    adapter = FakeAdapter("alpha", [STORE_A], catalog)
    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()
    canonicals = list(await db.scalars(select(CanonicalProduct)))
    assert len(canonicals) == 1, "all three listings are the same canonical product"
    assert canonicals[0].gtin == "00001111060903"
    organic = await db.scalar(
        select(RetailerProduct).where(RetailerProduct.retailer_sku == "organic")
    )
    assert organic is not None
    assert organic.canonical_product_id == canonicals[0].id
    assert organic.match_status == "exact", "it can only have matched on the backfilled GTIN"


@contextmanager
def count_statements(engine) -> Iterator[list[str]]:
    """Every SQL statement the engine executes inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def eggs_catalog(prefix: str, count: int) -> dict[str, dict[str, list]]:
    return {
        "eggs": {
            "A1": [
                listing(f"{prefix}-{index}", f"Farm {index} Grade A Eggs, 12 CT", "A1", "4.99")
                for index in range(count)
            ]
        }
    }


async def _selects_for(db: AsyncSession, engine, adapter: FakeAdapter) -> int:
    with count_statements(engine) as statements:
        await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
        await db.commit()
    return sum(1 for statement in statements if statement.upper().startswith("SELECT"))


async def test_ingest_reads_do_not_scale_with_the_number_of_listings(
    db: AsyncSession, engine
) -> None:
    """The N+1 this refactor removed: a batch's reads must be per batch, not per listing.

    Both passes matter and read different code. The first matches every listing against the
    canonical products (the query that used to reload the whole category per listing); the
    second finds existing rows, exercising the retailer-product, offer and price-history
    preloads instead.
    """
    small = FakeAdapter("small", [STORE_A], eggs_catalog("s", 4))
    large = FakeAdapter("large", [STORE_A], eggs_catalog("l", 24))

    first = {a.slug: await _selects_for(db, engine, a) for a in (small, large)}
    second = {a.slug: await _selects_for(db, engine, a) for a in (small, large)}

    assert first["small"] == first["large"], (
        f"matching scaled with listing count: {first} (4 listings vs 24)"
    )
    assert second["small"] == second["large"], (
        f"rescrape reads scaled with listing count: {second} (4 listings vs 24)"
    )
    assert max(second.values()) <= 10, f"a batch should need a handful of reads, got {second}"


# ------------------------------------------------- product URL hygiene at the DB boundary


@pytest.mark.parametrize(
    "bad_url",
    [
        "{'id': 'dce2530a', 'canonicalUrl': None, '__typename': 'LandingProductCanonicalUrl'}",
        "[object Object]",
        "None",
        "/relative/path",
        "eggs-12-ct",
        "javascript:alert(1)",
        "https://www.wholefoodsmarket.com/grocery/product/a-b01",  # another retailer's host
    ],
)
async def test_a_url_that_is_not_this_retailers_page_is_never_stored(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """Adapters clean their own URLs; ingest re-checks, so no path can persist a bad one."""
    from dataclasses import replace

    item = replace(listing("a-eggs", "Eggs, 12 CT", "A1", "4.99"), product_url=bad_url)
    adapter = FakeAdapter("alpha", [STORE_A], {"eggs": {"A1": [item]}})
    use_adapters(monkeypatch, adapter)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])

    stored = await db.scalar(select(RetailerProduct.product_url))
    assert stored is None
    assert await db.scalar(select(func.count()).select_from(Offer)) == 1  # the offer survives


async def test_a_real_page_on_the_retailers_own_host_is_kept(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter(
        "alpha", [STORE_A], {"eggs": {"A1": [listing("a-eggs", "Eggs, 12 CT", "A1", "4.99")]}}
    )
    use_adapters(monkeypatch, adapter)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])
    assert await db.scalar(select(RetailerProduct.product_url)) == "https://example.test/a-eggs"


async def test_no_stored_url_can_be_a_relative_path_the_browser_would_resolve_locally(
    db: AsyncSession, scraped
) -> None:
    """A relative value in `href` becomes `http://localhost:3000/...` in the browser."""
    urls = [u for u in await db.scalars(select(RetailerProduct.product_url)) if u is not None]
    assert urls
    assert all(u.startswith("https://") for u in urls)


async def test_the_longest_real_stock_wording_is_stored_whole(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`availability=OUT_OF_STOCK_ONLINE` -- 32 characters -- is a string this codebase
    builds, and the column used to hold 30. It has to survive intact: `stock_status` is the
    wording a verdict gets quoted from, and `availability=OUT_OF_STOCK_ONLI` is a token no
    payload ever contained.
    """
    wording = "availability=OUT_OF_STOCK_ONLINE"
    wordy = replace(
        listing("sku-wordy", "Eggs, 12 CT", STORE_A.external_id, "3.99"),
        availability="out_of_stock",
        stock_status=wording,
    )
    use_adapters(
        monkeypatch, FakeAdapter("wordy", [STORE_A], {"eggs": {STORE_A.external_id: [wordy]}})
    )

    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])

    assert list(await db.scalars(select(Offer.stock_status))) == [wording]


async def test_a_stock_wording_too_long_for_the_column_costs_letters_not_the_scrape(
    db: AsyncSession, sessionmaker, clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop, exact at the boundary: a diagnostic must never fail an insert.

    `offers.stock_status` is bounded, and an over-long value would otherwise abort the whole
    store-and-category batch of offers rather than one field. A value of exactly the column
    width is not truncated; one character more loses exactly that character.
    """
    exact = "x" * STOCK_STATUS_MAX
    over = "y" * (STOCK_STATUS_MAX + 1)
    items = [
        replace(
            listing(f"sku-{size}", f"Eggs, {size} CT", STORE_A.external_id, "3.99"),
            stock_status=wording,
        )
        for size, wording in ((12, exact), (18, over))
    ]
    use_adapters(
        monkeypatch, FakeAdapter("wordy", [STORE_A], {"eggs": {STORE_A.external_id: items}})
    )

    await run_scrape(sessionmaker, clients, "94105", None, ["eggs"])

    stored = set(await db.scalars(select(Offer.stock_status)))
    assert stored == {exact, "y" * STOCK_STATUS_MAX}
