"""Trader Joe's product URLs, and the availability its catalogue does not actually publish.

Two defects this file pins:

* Every stored URL 404'd. The adapter built `/home/products/pdp/<url_key>`, where `url_key`
  is `<sku>-<slug>` (`093617-pesto-genovese-chicken-breast`). That is an obsolete route:
  today the site serves the product page at `/home/products/pdp/<sku>` and answers the
  slugged path with its "Oops!" page. Checked in a real browser across all seven supported
  categories -- the slug form returned 404, the bare-SKU form 200 -- because
  `www.traderjoes.com` answers a plain HTTP client with 403.
  The API supplies no working canonical path of its own: `canonical_url` is null on every
  item and `url_rewrites` repeats the same slug that 404s, so the SKU route is the only
  page Trader Joe's really serves.

* Availability was read from the catalogue's `availability` field, which is `"1"` for every
  product -- 344 of 344 with no filter applied, and `availability: {match: "0"}` matches
  nothing at all. A field that cannot vary reports carriage, not stock, exactly like Whole
  Foods' `isAvailable`. Trader Joe's publishes no per-store inventory, so its offers are
  `unknown`.
"""

import json
from pathlib import Path

import pytest
from app.normalize.availability import IN_STOCK, UNKNOWN
from app.retailers.traderjoes.adapter import parse_products

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def listings(name: str) -> list:
    return parse_products(load(name), "100", "traderjoes:test")


ALL_SEARCHES = [
    "traderjoes/search_eggs.json",
    "traderjoes/search_chicken_breast.json",
    "traderjoes/search_bananas.json",
]


@pytest.mark.parametrize("fixture", ALL_SEARCHES)
def test_product_urls_are_the_sku_route(fixture: str) -> None:
    for item in listings(fixture):
        assert item.product_url is not None
        assert item.product_url == (
            f"https://www.traderjoes.com/home/products/pdp/{item.retailer_sku}"
        )


@pytest.mark.parametrize("fixture", ALL_SEARCHES)
def test_no_product_url_carries_the_slug_that_404s(fixture: str) -> None:
    """The exact defect: `/pdp/<sku>-<slug>` is the obsolete route."""
    for item in listings(fixture):
        assert item.product_url is not None
        tail = item.product_url.rsplit("/", 1)[-1]
        assert tail == item.retailer_sku
        assert "-" not in tail
        assert not tail.endswith(".html")


def test_the_reported_broken_url_is_no_longer_produced() -> None:
    """`075609-chicken-heirloom-heritage-breasts-bnls-sknls` opened Trader Joe's Oops page."""
    for item in listings("traderjoes/search_chicken_breast.json"):
        assert item.product_url is not None
        assert "chicken-heirloom-heritage-breasts" not in item.product_url


def test_a_missing_sku_yields_no_listing_rather_than_a_guessed_url() -> None:
    payload = load("traderjoes/search_eggs.json")
    payload["data"]["products"]["items"][0]["sku"] = ""
    skus = {item.retailer_sku for item in parse_products(payload, "100", "x")}
    assert "" not in skus


@pytest.mark.parametrize("fixture", ALL_SEARCHES)
def test_availability_is_unknown_because_the_field_cannot_vary(fixture: str) -> None:
    """`availability: "1"` is carriage, not stock; it must never reach `in_stock`."""
    items = listings(fixture)
    assert items
    for item in items:
        assert item.availability == UNKNOWN
        assert item.availability != IN_STOCK


def test_the_raw_catalogue_wording_is_still_recorded() -> None:
    """`unknown` must stay traceable to what the payload actually said."""
    for item in listings("traderjoes/search_eggs.json"):
        assert item.stock_status == "availability=1"
