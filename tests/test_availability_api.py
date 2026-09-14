"""Availability is first-class: it is stored, it filters search, and it rules baskets.

The default everywhere that ranks offers is in-stock only. An out-of-stock offer is shown
when it is asked for by name, and never recommended.
"""

from decimal import Decimal

import pytest
from app.db.models import Offer, RetailerProduct
from app.services.scraper import run_scrape
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import STORE_A, STORE_B, FakeAdapter, listing
from tests.test_scrape_and_api import use_adapters

CHEAP_BUT_GONE = "gone-eggs"
DEARER_BUT_THERE = "here-eggs"


def mixed_stock_adapters() -> tuple[FakeAdapter, FakeAdapter]:
    """The shape the Lucky report described: one product, two stores, different stock.

    Store A has it cheaper but out of stock; store B has it dearer and on the shelf. Same
    brand and package, so the two offers merge into one canonical product and compete.
    Store A also carries a product no retailer reports stock for.
    """
    alpha = FakeAdapter(
        "alpha",
        [STORE_A],
        {
            "eggs": {
                "A1": [
                    listing(
                        CHEAP_BUT_GONE,
                        "Eggs, 12 CT",
                        "A1",
                        "1.99",
                        brand="Solid",
                        availability="out_of_stock",
                    ),
                    listing(
                        "unknown-eggs",
                        "Mystery Eggs, 12 CT",
                        "A1",
                        "2.49",
                        brand="Mystery",
                        availability="unknown",
                    ),
                ]
            },
            "milk": {"A1": [listing("a-milk", "Whole Milk, 1 GL", "A1", "5.49", brand="Alpha")]},
        },
    )
    beta = FakeAdapter(
        "beta",
        [STORE_B],
        {
            "eggs": {
                "B1": [
                    listing(
                        DEARER_BUT_THERE,
                        "Eggs, 12 CT",
                        "B1",
                        "4.99",
                        brand="Solid",
                        availability="in_stock",
                    )
                ]
            },
            "milk": {"B1": [listing("b-milk", "Whole Milk, 1 GL", "B1", "6.49", brand="Beta")]},
        },
    )
    return alpha, beta


@pytest.fixture
async def mixed(sessionmaker, clients, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha, beta = mixed_stock_adapters()
    use_adapters(monkeypatch, alpha, beta)
    await run_scrape(sessionmaker, clients, "94105", None, ["eggs", "milk"])


async def test_availability_is_persisted_per_offer(db: AsyncSession, mixed) -> None:
    rows = {
        sku: availability
        for sku, availability in await db.execute(
            select(RetailerProduct.retailer_sku, Offer.availability).join(
                Offer, Offer.retailer_product_id == RetailerProduct.id
            )
        )
    }
    assert rows[CHEAP_BUT_GONE] == "out_of_stock"
    assert rows[DEARER_BUT_THERE] == "in_stock"
    assert rows["unknown-eggs"] == "unknown"


async def test_search_defaults_to_in_stock_only(client: AsyncClient, mixed) -> None:
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    assert body["availability"] == "in_stock"
    skus = {o["retailer_sku"] for p in body["products"] for o in p["offers"]}
    assert skus == {DEARER_BUT_THERE}
    assert all(o["availability"] == "in_stock" for p in body["products"] for o in p["offers"])


async def test_the_cheapest_flag_ignores_offers_the_filter_removed(
    client: AsyncClient, mixed
) -> None:
    """The out-of-stock egg is the cheapest row in the table; it must not win the badge."""
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    cheapest = body["cheapest_offer_id"]
    flagged = [o for p in body["products"] for o in p["offers"] if o["id"] == cheapest]
    assert flagged and flagged[0]["retailer_sku"] == DEARER_BUT_THERE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("in_stock", {DEARER_BUT_THERE}),
        ("out_of_stock", {CHEAP_BUT_GONE}),
        ("unknown", {"unknown-eggs"}),
        ("all", {CHEAP_BUT_GONE, DEARER_BUT_THERE, "unknown-eggs"}),
    ],
)
async def test_every_filter_selects_exactly_its_state(
    client: AsyncClient, mixed, value: str, expected: set[str]
) -> None:
    body = (
        await client.get(
            "/products/search",
            params={"q": "eggs", "zip_code": "94105", "availability": value},
        )
    ).json()
    assert body["availability"] == value
    assert {o["retailer_sku"] for p in body["products"] for o in p["offers"]} == expected


async def test_an_unknown_filter_value_is_rejected(client: AsyncClient, mixed) -> None:
    response = await client.get(
        "/products/search", params={"q": "eggs", "zip_code": "94105", "availability": "maybe"}
    )
    assert response.status_code == 422


async def test_offers_carry_the_retailers_own_wording_for_display(
    client: AsyncClient, mixed
) -> None:
    body = (
        await client.get(
            "/products/search",
            params={"q": "eggs", "zip_code": "94105", "availability": "out_of_stock"},
        )
    ).json()
    offer = next(o for p in body["products"] for o in p["offers"])
    assert offer["availability"] == "out_of_stock"
    assert "stock_status" in offer


async def test_basket_never_recommends_an_out_of_stock_offer(client: AsyncClient, mixed) -> None:
    body = (
        await client.post(
            "/basket/compare",
            json={"zip_code": "94105", "items": [{"query": "eggs", "quantity": 12}]},
        )
    ).json()
    assert body["availability"] == "in_stock"
    cheapest = body["items"][0]["cheapest"]
    assert cheapest is not None
    assert cheapest["offer"]["retailer_sku"] == DEARER_BUT_THERE
    every_offer = [
        line["offer"] for option in body["single_store_options"] for line in option["lines"]
    ]
    assert every_offer and all(o["availability"] == "in_stock" for o in every_offer)


async def test_a_basket_may_be_asked_for_everything_explicitly(client: AsyncClient, mixed) -> None:
    """Widening the filter surfaces the other offers; it does not change what is recommended."""
    body = (
        await client.post(
            "/basket/compare",
            json={
                "zip_code": "94105",
                "items": [{"query": "eggs", "quantity": 12}],
                "availability": "all",
            },
        )
    ).json()
    assert body["availability"] == "all"
    stores = {line["offer"]["store"]["retailer_name"] for line in body["items"][0]["options"]}
    assert len(stores) > 1, "the out-of-stock store's line is now visible as an option"
    assert body["items"][0]["cheapest"]["offer"]["retailer_sku"] == DEARER_BUT_THERE


async def test_product_offers_shows_every_state_so_a_drill_down_hides_nothing(
    client: AsyncClient, db: AsyncSession, mixed
) -> None:
    product_id = await db.scalar(
        select(RetailerProduct.canonical_product_id).where(
            RetailerProduct.retailer_sku == CHEAP_BUT_GONE
        )
    )
    body = (await client.get(f"/products/{product_id}/offers")).json()
    assert body["availability"] == "all"
    assert any(o["availability"] == "out_of_stock" for o in body["product"]["offers"])


async def test_a_cheapest_badge_never_lands_on_an_offer_you_cannot_buy(
    client: AsyncClient, mixed
) -> None:
    """Showing an out-of-stock offer is fine when it was asked for; crowning it is not."""
    body = (
        await client.get(
            "/products/search", params={"q": "eggs", "zip_code": "94105", "availability": "all"}
        )
    ).json()
    contested = next(
        p
        for p in body["products"]
        if {o["retailer_sku"] for o in p["offers"]} >= {CHEAP_BUT_GONE, DEARER_BUT_THERE}
    )
    # Both offers are shown, and the dearer one that is actually on a shelf leads and is badged.
    assert contested["offers"][0]["retailer_sku"] == DEARER_BUT_THERE
    badged = [o for o in contested["offers"] if o["is_cheapest_for_product"]]
    assert [o["retailer_sku"] for o in badged] == [DEARER_BUT_THERE]
    assert contested["best_offer_id"] == badged[0]["id"]
    # And the headline "cheapest" is never something the shopper cannot buy.
    overall = [
        o for p in body["products"] for o in p["offers"] if o["id"] == body["cheapest_offer_id"]
    ]
    assert overall and overall[0]["availability"] == "in_stock"


async def test_within_one_state_the_set_is_still_ranked_by_price(
    client: AsyncClient, mixed
) -> None:
    """Filtering to out-of-stock only must still rank that set by price."""
    body = (
        await client.get(
            "/products/search",
            params={"q": "eggs", "zip_code": "94105", "availability": "out_of_stock"},
        )
    ).json()
    assert body["products"]
    for product in body["products"]:
        prices = [Decimal(o["unit_price"] or o["price"]) for o in product["offers"]]
        assert prices == sorted(prices)


async def test_nothing_unbuyable_is_ever_badged_best_for_this_product(
    client: AsyncClient, mixed
) -> None:
    """The badge reads "Best for this product", so it may only sit on something buyable.

    Asking to see out-of-stock or unknown offers is a reasonable thing to do; being told the
    best of them is the best choice for the product is not.
    """
    for wanted in ("in_stock", "out_of_stock", "unknown", "all"):
        body = (
            await client.get(
                "/products/search",
                params={"q": "eggs", "zip_code": "94105", "availability": wanted},
            )
        ).json()
        for product in body["products"]:
            badged = [o for o in product["offers"] if o["is_cheapest_for_product"]]
            assert all(o["availability"] == "in_stock" for o in badged), wanted
            if product["best_offer_id"] is not None:
                best = next(o for o in product["offers"] if o["id"] == product["best_offer_id"])
                assert best["availability"] == "in_stock", wanted


async def test_a_basket_asked_for_everything_still_prefers_a_buyable_line(
    client: AsyncClient, mixed
) -> None:
    body = (
        await client.post(
            "/basket/compare",
            json={
                "zip_code": "94105",
                "items": [{"query": "eggs", "quantity": 12}],
                "availability": "all",
            },
        )
    ).json()
    assert body["items"][0]["cheapest"]["offer"]["availability"] == "in_stock"


async def test_the_cheapest_single_store_is_one_you_can_actually_buy_from(
    client: AsyncClient, mixed
) -> None:
    """Store A's best line is merely `unknown`; store B's is on the shelf and costs more.

    Ranking single-store baskets on price alone crowned store A, which also made
    `cheapest_split` (correctly in-stock) look *more* expensive than the "cheapest" single
    store and reported a negative saving.
    """
    body = (
        await client.post(
            "/basket/compare",
            json={
                "zip_code": "94105",
                "items": [{"query": "eggs", "quantity": 12}],
                "availability": "all",
            },
        )
    ).json()
    single = body["cheapest_single_store"]
    assert single is not None
    assert all(line["offer"]["availability"] == "in_stock" for line in single["lines"])
    # The two headline baskets must stay comparable: a split is never dearer than the single
    # store it is compared against, so the saving is never negative.
    assert body["cheapest_split"] is not None
    assert Decimal(body["cheapest_split"]["total"]) <= Decimal(single["total"])
    assert Decimal(body["savings"]) >= 0


async def test_an_empty_page_says_whether_anything_was_collected_at_all(
    client: AsyncClient, mixed
) -> None:
    """ "Nothing scraped" and "the filter hid it" need opposite advice, so they are told apart."""
    filtered = (
        await client.get(
            "/products/search",
            params={"q": "chicken breast", "zip_code": "94105", "availability": "in_stock"},
        )
    ).json()
    assert filtered["products"] == []
    assert filtered["offers_before_filter"] == 0, "nothing was ever collected for this category"

    # Eggs exist, but only out-of-stock and unknown ones survive an out_of_stock query.
    hidden = (
        await client.get(
            "/products/search",
            params={"q": "eggs", "zip_code": "94105", "availability": "out_of_stock"},
        )
    ).json()
    assert hidden["products"], "sanity: the fixture has an out-of-stock egg"

    nothing_unknown_here = (
        await client.get(
            "/products/search",
            params={"q": "milk", "zip_code": "94105", "availability": "out_of_stock"},
        )
    ).json()
    assert nothing_unknown_here["products"] == []
    assert nothing_unknown_here["offers_before_filter"] > 0, "milk offers exist, just not these"


# ------------------------------------------------- retailers that publish no stock at all


async def test_a_retailer_that_publishes_no_stock_is_shown_beside_not_among(
    client: AsyncClient, mixed
) -> None:
    """The Trader Joe's problem: filtered out of the default view, it vanished completely.

    Its prices and links are real; only its stock is unpublished. So the default view keeps
    showing confirmed offers first, and returns the unknown ones separately for a client to
    put under its own heading -- visible, but never mistaken for confirmed.
    """
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    assert body["availability"] == "in_stock"
    assert all(o["availability"] == "in_stock" for p in body["products"] for o in p["offers"])
    unknown = body["unknown_products"]
    assert unknown, "an unknown-stock offer exists and must still be reachable"
    assert all(o["availability"] == "unknown" for p in unknown for o in p["offers"])


async def test_the_unknown_section_still_carries_prices_and_links(
    client: AsyncClient, mixed
) -> None:
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    for product in body["unknown_products"]:
        for offer in product["offers"]:
            assert Decimal(offer["price"]) > 0


async def test_the_unknown_section_never_carries_a_best_badge(client: AsyncClient, mixed) -> None:
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    for product in body["unknown_products"]:
        assert product["best_offer_id"] is None
        assert not any(o["is_cheapest_for_product"] for o in product["offers"])
        assert not any(o["is_cheapest_overall"] for o in product["offers"])


async def test_a_product_already_confirmed_is_not_repeated_in_the_unknown_section(
    client: AsyncClient, mixed
) -> None:
    """A product with both a confirmed and an unknown offer belongs in the confirmed list."""
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()
    shown = {p["id"] for p in body["products"]}
    assert not (shown & {p["id"] for p in body["unknown_products"]})


async def test_an_explicit_filter_gets_exactly_what_it_asked_for(
    client: AsyncClient, mixed
) -> None:
    """`unknown` and `all` are the shopper asking; they need no separate section."""
    for wanted in ("unknown", "all", "out_of_stock"):
        body = (
            await client.get(
                "/products/search",
                params={"q": "eggs", "zip_code": "94105", "availability": wanted},
            )
        ).json()
        assert body["unknown_products"] == [], wanted


async def test_an_unknown_offer_never_wins_a_basket_by_default(client: AsyncClient, mixed) -> None:
    body = (
        await client.post(
            "/basket/compare",
            json={"zip_code": "94105", "items": [{"query": "eggs", "quantity": 12}]},
        )
    ).json()
    for item in body["items"]:
        if item.get("cheapest"):
            assert item["cheapest"]["offer"]["availability"] == "in_stock"


# ------------------------------------- which kind of `unknown`, as the API reports it


@pytest.fixture
def alpha_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `alpha` a retailer of the Trader Joe's kind, as far as the API is concerned.

    `stock_reporting` is read off the adapter registry by slug, and the fakes are not in it,
    so without this every store in these tests serializes `live` and the whole
    `not_published` half of the contract goes unexercised by any API test.
    """
    from app.services import stores as stores_service

    real = stores_service.stock_reporting
    monkeypatch.setattr(
        stores_service,
        "stock_reporting",
        lambda slug: "not_published" if slug == "alpha" else real(slug),
    )


def _stores_in(body: dict) -> list[dict]:
    """Every store the response mentions, wherever it mentions it."""
    found = list(body.get("stores", []))
    for key in ("products", "unknown_products"):
        for product in body.get(key, []):
            found.extend(offer["store"] for offer in product["offers"])
            if product.get("best_offer"):
                found.append(product["best_offer"]["store"])
    return found


async def test_search_tells_a_client_which_retailers_publish_stock_at_all(
    client: AsyncClient, mixed, alpha_publishes_nothing
) -> None:
    """Without this field a client cannot tell "we could not read it" from "they do not
    publish it", and has to word both of them "Stock unknown"."""
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()

    reported = {s["retailer_slug"]: s["stock_reporting"] for s in _stores_in(body)}
    assert reported["alpha"] == "not_published"
    assert reported["beta"] == "live"


async def test_the_basket_carries_it_too(
    client: AsyncClient, mixed, alpha_publishes_nothing
) -> None:
    body = (
        await client.post(
            "/basket/compare",
            json={
                "zip_code": "94105",
                "items": [{"query": "eggs", "quantity": 12}],
                "availability": "all",
            },
        )
    ).json()

    reported = {s["retailer_slug"]: s["stock_reporting"] for s in body["stores"]}
    assert reported["alpha"] == "not_published"
    assert reported["beta"] == "live"
    lines = [line["offer"]["store"]["stock_reporting"] for line in body["cheapest_split"]["lines"]]
    assert all(value in ("live", "not_published") for value in lines)


async def test_a_retailer_publishing_nothing_is_ranked_exactly_like_any_other_unknown(
    client: AsyncClient, mixed, alpha_publishes_nothing
) -> None:
    """The wording changed; the ranking must not have.

    `stock_reporting` is presentation, and nothing downstream of `Offer.availability` may
    read it. This is the test that fails if somebody ever special-cases it in search or
    basket ranking -- the ordinary unknown-stock assertions, run again against a retailer
    that publishes no stock at all.
    """
    body = (await client.get("/products/search", params={"q": "eggs", "zip_code": "94105"})).json()

    unknown = body["unknown_products"]
    assert unknown, "the unpublished-stock product is still returned beside the results"
    assert any(o["store"]["retailer_slug"] == "alpha" for p in unknown for o in p["offers"])
    for product in unknown:
        assert product["best_offer"] is None
        assert product["best_offer_id"] is None
        assert not any(o["is_cheapest_for_product"] for o in product["offers"])
        assert not any(o["is_cheapest_overall"] for o in product["offers"])
    # And it is still absent from the confirmed list, and still not the global cheapest.
    assert all(o["availability"] == "in_stock" for p in body["products"] for o in p["offers"])
    cheapest = body["cheapest_offer_id"]
    assert cheapest not in {o["id"] for p in unknown for o in p["offers"]}


async def test_a_retailer_publishing_nothing_still_never_wins_a_basket(
    client: AsyncClient, mixed, alpha_publishes_nothing
) -> None:
    body = (
        await client.post(
            "/basket/compare",
            json={"zip_code": "94105", "items": [{"query": "eggs", "quantity": 12}]},
        )
    ).json()

    for item in body["items"]:
        if item.get("cheapest"):
            assert item["cheapest"]["offer"]["availability"] == "in_stock"
    for option in body["single_store_options"]:
        for line in option["lines"]:
            assert line["offer"]["availability"] == "in_stock"
