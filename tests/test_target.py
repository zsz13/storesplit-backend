"""Target: what its own page data says, and the two fields that would lie if believed.

Target is the first retailer here that no HTTP client can reach, so it is read through the
browser layer. The payloads below are real captures from a verified session
(`tests/fixtures/target/`), and the tests are mostly about *not* believing the wrong field:

* `shipping_options.availability_status` reads `OUT_OF_STOCK` for every product on the
  captured shelf -- including ones with ten sitting in the store being priced. It is whether
  Target will post the item, and it is not stock.
* `in_store_only` and the store's own count agree everywhere they were observed, so where
  they would disagree the answer is `unknown` rather than whichever one is cheerier.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.normalize.availability import IN_STOCK, OUT_OF_STOCK, UNKNOWN
from app.normalize.unit_price import unit_price
from app.normalize.units import Quantity
from app.retailers.target.adapter import (
    SITE_URL,
    _money,
    categories,
    parse_category,
    parse_store,
    price_semantics,
    store_availability,
)
from app.services.scraper import listing_quantity

FIXTURES = Path(__file__).parent / "fixtures" / "target"
STORE = "3264"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def captured() -> list[tuple[str, dict]]:
    return [
        (
            "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2?x=1",
            load("plp_search_v2.json"),
        ),
        (
            "https://redsky.target.com/redsky_aggregations/v1/web/product_summary_with_fulfillment_v1",
            load("product_summary_with_fulfillment_v1.json"),
        ),
    ]


def listings() -> list:
    return parse_category(captured(), STORE, "target:test")


def by_sku(sku: str):
    return next(item for item in listings() if item.retailer_sku == sku)


# ------------------------------------------------------------------------ the catalogue


def test_every_supported_staple_has_a_target_category() -> None:
    """The scrape asks by `search_query`, so the catalogue is keyed by exactly those."""
    from app.normalize.categories import CATEGORIES

    wanted = {category.search_query for category in CATEGORIES.values()}
    assert wanted <= set(categories())


def test_the_catalogue_only_names_robots_allowed_paths() -> None:
    """Target disallows `/s?`, `/shop/` and `/pl/`; `/c/` category pages it does not."""
    for path in categories().values():
        assert path.startswith("/c/")


# ------------------------------------------------------------------------------ parsing


def test_the_shelf_parses_into_offers() -> None:
    items = listings()
    assert items
    for item in items:
        assert item.retailer_sku and item.title and item.price > 0
        assert item.availability in {IN_STOCK, OUT_OF_STOCK, UNKNOWN}


def test_titles_are_unescaped_not_stored_as_html_entities() -> None:
    """The payload says `Eggland&#39;s Best`; a shopper must not be shown that."""
    title = by_sku("83880304").title
    assert "Eggland's Best" in title
    assert "&#" not in title


def test_product_urls_are_targets_own_buy_url() -> None:
    """`enrichment.buy_url` is the retailer's link, so nothing here invents a pattern."""
    for item in listings():
        assert item.product_url is not None
        assert item.product_url.startswith(f"{SITE_URL}/p/")
        assert "/-/A-" in item.product_url
        assert item.retailer_sku in item.product_url


def test_the_store_comes_from_targets_own_store_call() -> None:
    store = parse_store([("…/store_location_v1?store_id=3264", load("store_location_v1.json"))])
    assert store is not None
    assert store.external_id == "3264"
    assert store.name == "San Francisco Stonestown"
    assert store.zip_code == "94132"  # trimmed from the payload's ZIP+4
    assert store.city == "San Francisco" and store.state == "CA"


def test_no_store_call_means_no_store_rather_than_a_guess() -> None:
    assert parse_store([("…/plp_search_v2", {"data": {}})]) is None


# ------------------------------------------------------------------------- availability


def test_stock_is_read_from_the_store_not_from_shipping() -> None:
    """The regression this retailer would otherwise have shipped with.

    Every product in the captured payload is `OUT_OF_STOCK` for shipping. If that field
    decided, Target would contribute no buyable offers at all while its shelves were full.
    """
    shipping = {
        summary["fulfillment"]["shipping_options"]["availability_status"]
        for summary in load("product_summary_with_fulfillment_v1.json")["data"]["product_summaries"]
    }
    assert shipping <= {"OUT_OF_STOCK", "UNAVAILABLE"}, "the captured shelf is all unshippable"
    assert by_sku("14662561").availability == IN_STOCK  # ten of them in the store


def test_a_zero_count_in_the_store_is_out_of_stock() -> None:
    item = by_sku("21506498")
    assert item.availability == OUT_OF_STOCK
    assert item.stock_status == "OUT_OF_STOCK (quantity=0)"


def test_limited_stock_is_in_stock_because_the_count_agrees() -> None:
    """`LIMITED_STOCK` at a count of one is one left, not "likely gone"."""
    item = by_sku("52235226")
    assert item.availability == IN_STOCK
    assert item.stock_status == "LIMITED_STOCK (quantity=1)"


def test_a_product_whose_stock_never_loaded_is_unknown() -> None:
    """The shelf is longer than the stock the page had fetched; the rest are not assumed."""
    assert by_sku("83880304").availability == UNKNOWN
    assert by_sku("83880304").stock_status is None


class TestStoreAvailability:
    def option(self, **kwargs: object) -> dict:
        return {
            "sold_out": False,
            "store_options": [
                {
                    "location_id": STORE,
                    "location_available_to_promise_quantity": kwargs.get("quantity"),
                    "in_store_only": {"availability_status": kwargs.get("in_store")},
                    "order_pickup": {"availability_status": kwargs.get("pickup")},
                }
            ],
            "shipping_options": {"availability_status": "OUT_OF_STOCK"},
        }

    def test_the_word_and_the_count_disagreeing_is_unknown(self) -> None:
        """ "In stock" with nothing left is two first-party fields contradicting."""
        _, state = store_availability(self.option(in_store="IN_STOCK", quantity=0), STORE)
        assert state == UNKNOWN

    def test_the_shelf_is_preferred_over_pickup(self) -> None:
        """`order_pickup` says UNAVAILABLE for things that are on the shelf."""
        _, state = store_availability(
            self.option(in_store="LIMITED_STOCK", pickup="UNAVAILABLE", quantity=1), STORE
        )
        assert state == IN_STOCK

    def test_another_stores_entry_is_not_this_stores_stock(self) -> None:
        block = self.option(in_store="IN_STOCK", quantity=10)
        block["store_options"][0]["location_id"] = "9999"
        _, state = store_availability(block, STORE)
        assert state == UNKNOWN

    def test_sold_out_wins_outright(self) -> None:
        block = self.option(in_store="IN_STOCK", quantity=10)
        block["sold_out"] = True
        raw, state = store_availability(block, STORE)
        assert state == OUT_OF_STOCK and raw == "sold_out"

    def test_a_count_with_no_word_still_answers(self) -> None:
        raw, state = store_availability(self.option(quantity=4), STORE)
        assert state == IN_STOCK and raw == "quantity=4"
        _, empty = store_availability(self.option(quantity=0), STORE)
        assert empty == OUT_OF_STOCK

    @pytest.mark.parametrize("block", [None, {}, {"store_options": []}, "nonsense"])
    def test_anything_unreadable_is_unknown(self, block: object) -> None:
        _, state = store_availability(block, STORE)  # type: ignore[arg-type]
        assert state == UNKNOWN


# --------------------------------------------------- variable-weight meat: the price basis

CHICKEN_SHELF = "plp_search_v2_chicken.json"


def chicken() -> list:
    """The two variable-weight trays, parsed off a shelf carrying Target's own price blocks."""
    return parse_category(
        [
            (
                "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2?x=1",
                load(CHICKEN_SHELF),
            )
        ],
        STORE,
        "target:test",
    )


def chicken_by_sku(sku: str):
    return next(item for item in chicken() if item.retailer_sku == sku)


@pytest.mark.parametrize(
    ("sku", "title", "rate", "maximum"),
    [
        (
            "86676070",
            "Fresh All Natural Boneless & Skinless Chicken Breast Value Pack - "
            "2.5-5.25lbs - price per lb - Good & Gather\u2122",
            Decimal("2.59"),
            Decimal("12.95"),
        ),
        (
            "84991365",
            "Foster Farms No Antibiotics Ever Boneless Skinless Chicken Breasts - "
            "1.25-2.5lbs - price per lb: Certified Humane, High Protein, Cage Free",
            Decimal("5.99"),
            Decimal("11.98"),
        ),
    ],
)
def test_the_verbatim_captured_price_block_reads_as_a_rate(
    sku: str, title: str, rate: Decimal, maximum: Decimal
) -> None:
    """The captured payload, run through the real reader rather than merely asserted about.

    `pdp_price_variable_weight.json` holds Target's own `price` objects exactly as it served
    them. Checking that the file contains the numbers it contains would prove only that
    nobody hand-edited it; this puts them through `price_semantics`, which is the code that
    decides whether $2.59 is a tray or a pound of one.
    """
    rows = load("pdp_price_variable_weight.json")["products"]
    captured = {row["tcin"]: row["price"] for row in rows}

    semantics = price_semantics(captured[sku], {}, title)

    assert semantics.basis == "lb"
    assert semantics.max_total_price == maximum
    assert semantics.size_text is None
    assert semantics.weight_range is not None
    assert _money(captured[sku]["current_retail"]) == rate, "the rate is what Target published"


def test_the_captured_payload_still_says_what_the_fix_was_built_on() -> None:
    """Guards the fixture itself: the bug is only interesting because these are the real
    numbers Target served. A-86676070 is `current_retail: 2.59` under a `/lb` unit price and
    a `max price` headline of $12.95 -- a rate, not $2.59 for the tray."""
    captured_pdp = load("pdp_price_variable_weight.json")
    prices = {row["tcin"]: row["price"] for row in captured_pdp["products"]}
    assert prices["86676070"]["current_retail"] == 2.59
    assert prices["86676070"]["formatted_unit_price"] == "$2.59"
    assert prices["86676070"]["formatted_unit_price_suffix"] == "/lb"
    assert prices["86676070"]["formatted_max_item_price"] == "$12.95"
    assert prices["84991365"]["current_retail"] == 5.99
    assert prices["84991365"]["formatted_max_item_price"] == "$11.98"


def test_a_fixed_package_price_block_is_not_read_as_a_rate() -> None:
    """The eggs on the captured shelf: `current_retail: 5.89` against `"$0.49" "/count"`.
    The two numbers disagreeing is what a real package total looks like."""
    eggs = next(
        p["price"]
        for p in load("plp_search_v2.json")["data"]["search"]["products"]
        if p["tcin"] == "83880304"
    )

    semantics = price_semantics(eggs, {"bullet_descriptions": []}, "Large Cage Free White Eggs")

    assert semantics.basis == "package"
    assert semantics.max_total_price is None and semantics.weight_range is None


@pytest.mark.parametrize(
    ("sku", "rate", "maximum", "low", "high"),
    [
        ("86676070", Decimal("2.59"), Decimal("12.95"), Decimal("2.5"), Decimal("5.25")),
        ("84991365", Decimal("5.99"), Decimal("11.98"), Decimal("1.25"), Decimal("2.5")),
    ],
)
def test_a_price_per_lb_tray_is_read_as_a_rate_not_a_pack_total(
    sku: str, rate: Decimal, maximum: Decimal, low: Decimal, high: Decimal
) -> None:
    item = chicken_by_sku(sku)
    assert item.price_basis == "lb", "Target says `price per lb` in the title and the suffix"
    assert item.price == rate
    assert item.max_total_price == maximum, "copied from Target, never price x max weight"
    assert item.weight_range is not None
    assert item.weight_range.minimum == Quantity(low, "lb")
    assert item.weight_range.maximum == Quantity(high, "lb")
    assert item.size_text is None, "a variable-weight tray has no single package size"


@pytest.mark.parametrize(
    ("sku", "expected"), [("86676070", Decimal("2.59")), ("84991365", Decimal("5.99"))]
)
def test_the_unit_price_is_the_rate_itself_and_is_not_divided_again(
    sku: str, expected: Decimal
) -> None:
    """The regression. The published weight range used to be parsed off the title and divided
    into a price that had already accounted for it: $2.59/lb became $0.49/lb (2.59 / 5.25)
    and $5.99/lb became $2.40/lb (5.99 / 2.5)."""
    item = chicken_by_sku(sku)
    quantity = listing_quantity(item)
    assert quantity == Quantity(Decimal(1), "lb")
    assert unit_price(item.price, quantity, "lb") == expected


def test_target_max_total_is_the_retailers_own_number_not_rate_times_max_weight() -> None:
    """$2.59 x 5.25 lb is $13.60. Target charges at most $12.95. Deriving it would be wrong
    while looking exactly as authoritative, so only the published figure is kept."""
    item = chicken_by_sku("86676070")
    assert item.weight_range is not None
    derived = item.price * item.weight_range.maximum.value
    assert derived == Decimal("13.5975")
    assert item.max_total_price == Decimal("12.95") != derived


def test_a_fixed_package_on_the_same_shelf_is_still_a_package_total() -> None:
    """The eggs shelf prices `current_retail: 5.89` against `"$0.49" "/count"`. The numbers
    disagree, which is what a real package total looks like, and it must stay one."""
    eggs = by_sku("83880304")
    assert eggs.price_basis == "package"
    assert eggs.price == Decimal("5.89") and eggs.size_text == "12 ct"
    assert eggs.weight_range is None and eggs.max_total_price is None
    quantity = listing_quantity(eggs)
    assert quantity == Quantity(Decimal(12), "count")
    assert unit_price(eggs.price, quantity, "count") == Decimal("0.4908")


def test_target_searches_one_category_at_a_time() -> None:
    """Its seven categories share one browser page, so they are not scraped concurrently.

    Declared on the adapter rather than discovered by the scrape service piling seven tasks
    onto one lock: they would take turns regardless, and the six that queue would spend the
    retailer's deadline doing it.
    """
    from app.retailers.target.adapter import TargetAdapter

    assert TargetAdapter.max_concurrent_searches == 1
    assert len(categories()) == 7


def covered_page() -> list[tuple[str, dict]]:
    """One category page load: its store call and a shelf whose stock is fully covered.

    The captured shelf fixture lists more products than the captured summary answers for, which
    is exactly what `stock_covers_shelf` refuses -- so it is trimmed here to the products whose
    stock really did arrive. That is what a complete page looks like, and only a complete one may
    stand in for a second load.
    """
    shelf = load("plp_search_v2.json")
    covered = {
        str(summary["tcin"])
        for summary in load("product_summary_with_fulfillment_v1.json")["data"]["product_summaries"]
    }
    products = shelf["data"]["search"]["products"]
    shelf["data"]["search"]["products"] = [p for p in products if str(p["tcin"]) in covered]
    return [
        (
            "https://redsky.target.com/redsky_aggregations/v1/web/store_location_v1?store_id=3264",
            load("store_location_v1.json"),
        ),
        ("https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2?x=1", shelf),
        (
            "https://redsky.target.com/redsky_aggregations/v1/web/product_summary_with_fulfillment_v1",
            load("product_summary_with_fulfillment_v1.json"),
        ),
    ]


class StubClients:
    """Just enough of `RetailerClients` to build the adapter: the pool's browser, and no HTTP."""

    def __init__(self, session: object) -> None:
        self._session = session

    def browser(self) -> object:
        return self._session

    def shared(self) -> None:
        return None  # only `fetch_store_details` uses it, and no test here calls that


async def test_find_stores_and_the_first_category_are_one_page_load() -> None:
    """The store the session is shopping is read off the page a search wants anyway.

    Two loads of one page is the most expensive request this makes, spent twice for one answer,
    and each is another arrival at a retailer that counts them. Pinned through the adapter
    because the saving depends on both halves naming the same URL: `find_stores` asks for the
    first category and `search_products` asks for the category it was given.
    """
    from app.retailers.browser import BrowserSession
    from app.retailers.target.adapter import SLUG, TargetAdapter

    from tests.test_browser_fallback import ShelfPage

    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, SLUG, covered_page())
    session._pages[SLUG] = page  # type: ignore[assignment]
    adapter = TargetAdapter(StubClients(session))  # type: ignore[arg-type]

    stores = await adapter.find_stores("94132")
    assert [store.external_id for store in stores] == [STORE]

    first_category = next(iter(categories()))
    listings = await adapter.search_products(first_category, stores[0])

    assert listings, "the shelf was read from the capture the store lookup already produced"
    assert len(page.visits) == 1, "one page load, not two"
    assert session.diagnostics()["pages_reused_from_capture"] == {SLUG: 1}
    assert session.diagnostics()["page_loads"] == {SLUG: 1}


def test_a_listing_carries_the_store_the_page_answered_for() -> None:
    """Target picks a store from the session, so the echo is what makes a price checkable.

    `ingest_listing` already refuses a listing whose `store_context` names another store; with no
    echo it has nothing to compare, and a real price from store B is written as store A's.
    """
    asked_about = "9999"
    listings = parse_category(covered_page(), asked_about, "target:test")
    assert listings
    assert {listing.store_context for listing in listings} == {STORE}, (
        "the store the page said it was shopping, not the one it was asked about"
    )
