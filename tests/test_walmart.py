"""Walmart: what its browse page really says, and the fields that only mean something together.

The payloads are real captures from a human-verified session (`tests/fixtures/walmart/`).
The point of most of these tests is restraint. A Walmart browse page says `IN_STOCK` about
almost everything on it, because it does not list what it has not got -- so believing that
field on its own would be exactly the "a search result is not stock" mistake. It is only
worth something alongside `canAddToCart`, which really does vary, and the store id the row
is fulfilled from.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.normalize.availability import IN_STOCK, UNKNOWN
from app.retailers.walmart.adapter import (
    SITE_URL,
    browse_availability,
    categories,
    parse_browse,
    store_id_from_browse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "walmart"
STORE = "2648"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def listings() -> list:
    return parse_browse(load("browse_eggs.json"), STORE, "walmart:test")


def by_sku(sku: str):
    return next(item for item in listings() if item.retailer_sku == sku)


# ------------------------------------------------------------------------ the catalogue


def test_every_supported_staple_has_a_walmart_category() -> None:
    from app.normalize.categories import CATEGORIES

    assert {category.search_query for category in CATEGORIES.values()} <= set(categories())


def test_the_catalogue_avoids_the_disallowed_search_paths() -> None:
    """`robots.txt` disallows `/search` and `/api/`; `/cp/` category pages it does not."""
    for path in categories().values():
        assert path.startswith("/cp/")
        assert "/search" not in path and "/api/" not in path


# ------------------------------------------------------------------------------ parsing


def test_the_shelf_parses_with_prices() -> None:
    items = listings()
    assert items
    for item in items:
        assert item.retailer_sku and item.title and item.price > 0


def test_prices_come_from_the_nested_price_lines() -> None:
    """The flat `priceInfo.itemPrice` is empty on these rows; the real number is nested."""
    assert by_sku("100966386").price == Decimal("2.47")
    assert by_sku("421705528").price == Decimal("1.67")


def test_product_urls_are_walmarts_own_canonical_path_without_tracking() -> None:
    for item in listings():
        assert item.product_url is not None
        assert item.product_url.startswith(f"{SITE_URL}/ip/")
        assert "?" not in item.product_url  # classType/athbdg tracking dropped
        assert item.retailer_sku in item.product_url


def test_a_row_with_no_id_or_price_is_skipped_rather_than_half_written() -> None:
    assert all(item.retailer_sku for item in listings())


def test_the_store_is_read_from_the_rows_own_fulfilment() -> None:
    assert store_id_from_browse(load("browse_eggs.json")) == STORE


def test_the_marketplace_placeholder_is_not_mistaken_for_a_store() -> None:
    """Marketplace rows carry `storeId: "0"`, which is not a shop that has anything."""
    payload = load("browse_eggs.json")
    ids = {
        summary.get("storeId")
        for item in payload["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0][
            "items"
        ]
        for summary in item.get("fulfillmentSummary") or []
    }
    assert "0" in ids, "the fixture really does contain the placeholder"
    assert store_id_from_browse(payload) == STORE


# ------------------------------------------------------------------------- availability


def test_a_real_shelf_row_is_in_stock() -> None:
    assert by_sku("100966386").availability == IN_STOCK


def test_a_marketplace_row_is_not_in_stock_however_cheerful_it_sounds() -> None:
    """It says "In stock" and carries a price, and this store still cannot sell it."""
    item = by_sku("5475222501")
    assert item.availability == UNKNOWN
    assert item.stock_status is not None and "In stock" in item.stock_status


class TestBrowseAvailability:
    def row(self, **kwargs: object) -> dict:
        return {
            "availabilityStatusV2": {
                "display": kwargs.get("display", "In stock"),
                "value": kwargs.get("value", "IN_STOCK"),
            },
            "canAddToCart": kwargs.get("cart", True),
            "fulfillmentSummary": [
                {"fulfillment": "PICKUP", "storeId": kwargs.get("store", STORE)}
            ],
        }

    def test_all_three_agreeing_is_in_stock(self) -> None:
        _, state = browse_availability(self.row(), STORE)
        assert state == IN_STOCK

    def test_a_row_this_store_cannot_sell_is_unknown(self) -> None:
        _, state = browse_availability(self.row(cart=False), STORE)
        assert state == UNKNOWN

    def test_another_stores_row_is_unknown(self) -> None:
        _, state = browse_availability(self.row(store="9999"), STORE)
        assert state == UNKNOWN

    def test_a_status_that_is_not_in_stock_is_unknown_not_out_of_stock(self) -> None:
        """A browse page omits what it lacks, so absence here is not evidence of absence."""
        row = self.row(value="OUT_OF_STOCK", display="Out of stock")
        _, state = browse_availability(row, STORE)
        assert state == UNKNOWN

    @pytest.mark.parametrize("row", [{}, {"availabilityStatusV2": None}, {"canAddToCart": True}])
    def test_anything_unreadable_is_unknown(self, row: dict) -> None:
        _, state = browse_availability(row, STORE)
        assert state == UNKNOWN

    def test_no_row_ever_reports_out_of_stock(self) -> None:
        """Documented limitation, pinned: this source cannot observe absence."""
        states = {
            browse_availability(self.row(**kw), STORE)[1]
            for kw in (
                {},
                {"cart": False},
                {"value": "OUT_OF_STOCK"},
                {"store": "1"},
                {"value": "X"},
            )
        }
        assert "out_of_stock" not in states
