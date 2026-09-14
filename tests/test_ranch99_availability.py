"""What 99 Ranch's `available` field means, and why its product page cannot contradict it.

`available` is a per-store quantity, and zero is the retailer's own out-of-stock state. That
is not an inference from the name -- it is what 99 Ranch's own product tile does with it. In
the site's `_app` bundle the tile component reads:

    x = t.available, E = (0 === x), ... children: [E && <SoldOutMask/>, ...]

and `SoldOutMask` renders `common.sold_out` over `common.stock_soon` -- shopper-visible as
**"Sold Out" / "In stock soon"**, with the title greyed out. Measured against that component
on 2026-09-13: 467 rendered tiles across the two ZIPs 94404 and 94014, 13 of which the
`be-api` payload gave `available: 0`; exactly those 13 carried the mask and no others, in
both ZIPs, with zero mismatches.

The field is a real count, not a flag: 81-100 distinct positive values per store over 335
listings, and it varies per store for the same product (Clover Whole Milk was 20 / 0 / 39
at Foster City / Daly City / Richmond in one read).

**The product page is stock-blind, so its silence proves nothing.** `/product-details/...`
renders an enabled "Add to Cart" for every product, including the ones the retailer's own
grid masks "Sold Out": its click handler gates on the variant id alone --

    onClick: function(){ n ? (s(n,p), l&&l(p)) : warn("productDetail.product_not_available") }

where `n = t.variantId` -- and nothing on that page reads `available`. Verified directly:
products 2001373 and 2075786, both masked "Sold Out / In stock soon" in the grid at Foster
City, open with "Sold Out" nowhere on the page and Add to Cart enabled. So "the product page
does not say it is unavailable" is true of the whole catalogue and is not evidence of stock.
"""

import json
from pathlib import Path
from typing import Any

from app.normalize.availability import IN_STOCK, OUT_OF_STOCK, UNKNOWN
from app.retailers.ranch99.adapter import parse_search_results

FIXTURES = Path(__file__).parent / "fixtures" / "ranch99"
# Both reported product pages, as the store-scoped search answered for them on 2026-09-13.
FOSTER_CITY = "1782"
DALY_CITY = "1769"
ORGANIC_CHICKEN_BREAST = "2049573"  # /product-details/2049573/1782/23592200000
CHICKEN_BREAST_W_RIB = "2008958"  # /product-details/2008958/1782/23533100000


def _states(store: str) -> dict[str, tuple[str, str | None]]:
    payload = json.loads((FIXTURES / f"search_chicken_breast_{store}.json").read_text())
    return {
        item.retailer_sku: (item.availability, item.stock_status)
        for item in parse_search_results(payload, store)
    }


def test_a_zero_quantity_is_the_retailers_own_sold_out_state() -> None:
    """Both reported products at Foster City: `available: 0`, which the grid masks Sold Out."""
    states = _states(FOSTER_CITY)

    assert states[ORGANIC_CHICKEN_BREAST] == (OUT_OF_STOCK, "available=0")
    assert states[CHICKEN_BREAST_W_RIB] == (OUT_OF_STOCK, "available=0")


def test_the_same_product_is_in_stock_at_a_store_that_has_it() -> None:
    """The answer is per store, from the `storeid` header: 0 at Foster City, 22 at Daly City."""
    assert _states(DALY_CITY)[CHICKEN_BREAST_W_RIB] == (IN_STOCK, "available=22")
    assert _states(FOSTER_CITY)[CHICKEN_BREAST_W_RIB] == (OUT_OF_STOCK, "available=0")


def test_a_product_zero_everywhere_is_out_of_stock_everywhere() -> None:
    """2049573 was 0 at all three Bay Area stores -- reported for both stores in the report."""
    assert _states(FOSTER_CITY)[ORGANIC_CHICKEN_BREAST][0] == OUT_OF_STOCK
    assert _states(DALY_CITY)[ORGANIC_CHICKEN_BREAST][0] == OUT_OF_STOCK


def test_the_positive_quantities_in_one_store_are_a_count_not_a_flag() -> None:
    """A flag would take one value. These take several, which is what makes a 0 readable.

    The property, not the numbers: re-capturing the fixture changes today's inventory but
    must not change the fact that the field discriminates. That is the test this project
    applies to any field before trusting it -- does it vary within one store? -- and it is
    what separates `available` from Whole Foods' `isAvailable`, true for all 393 of one
    store's own search results.
    """
    quantities = {
        status
        for availability, status in _states(DALY_CITY).values()
        if availability == IN_STOCK and status
    }

    assert len(quantities) > 1, f"a single value would be a flag, not a count: {quantities}"
    assert all(int(status.removeprefix("available=")) > 0 for status in quantities)


def test_a_missing_quantity_is_unknown_not_out_of_stock() -> None:
    """No field is no answer. Only a stated zero is a stated absence."""
    payload = json.loads((FIXTURES / f"search_chicken_breast_{FOSTER_CITY}.json").read_text())
    for item in payload["data"]["list"]:
        item.pop("available", None)

    listings = parse_search_results(payload, FOSTER_CITY)

    assert listings, "the fixture still parses without the field"
    assert {item.availability for item in listings} == {UNKNOWN}
    assert {item.stock_status for item in listings} == {None}


def test_a_quantity_that_is_not_a_number_is_unknown() -> None:
    """`available: null`, `"0"` or `true` are not quantities and are not read as one."""
    payload = json.loads((FIXTURES / f"search_chicken_breast_{FOSTER_CITY}.json").read_text())
    unreadable: list[Any] = [None, "0", "22", True, False, {}, []]
    for item, value in zip(payload["data"]["list"], unreadable, strict=False):
        item["available"] = value

    states = {item.availability for item in parse_search_results(payload, FOSTER_CITY)}

    assert states == {UNKNOWN}


def test_a_negative_quantity_is_unknown_because_the_retailer_does_not_call_it_sold_out() -> None:
    """The proven rule is `0 === available`, not `available <= 0`.

    99 Ranch's own tile masks "Sold Out" on exactly zero, so a negative -- an oversold count,
    or a value this field is not meant to hold -- is a shape nobody has proved the meaning
    of, and the retailer itself would still show it as buyable. `unknown` is the honest
    reading of it; `out_of_stock` would be StoreSplit inventing a negative the retailer has
    not stated, which is the thing this whole audit was about.
    """
    payload = json.loads((FIXTURES / f"search_chicken_breast_{FOSTER_CITY}.json").read_text())
    payload["data"]["list"] = payload["data"]["list"][:1]
    payload["data"]["list"][0]["available"] = -1

    listing = parse_search_results(payload, FOSTER_CITY)[0]

    assert listing.availability == UNKNOWN
    assert listing.stock_status == "available=-1"
