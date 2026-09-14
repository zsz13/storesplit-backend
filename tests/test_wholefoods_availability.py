"""What Whole Foods' own availability endpoint means, read conservatively.

The measured behaviour this encodes, over 574 ASINs at store 10151:

* `IN_STOCK` always arrives with an `offerDetails.offerListingId`.
* `availability: null` with no `offerDetails` at all is the shape the product page renders as
  **"Out of Stock"** (and, for `B0014GPSKQ` at SoMa, "Currently not sold in SoMa").
* That null flaps. Of 86 such nulls, 13 reported a live offer again on a later read.
* Two ASINs of 574 came back absent from the response entirely.

**The null is an absent answer, at any number of reads.** StoreSplit used to assert it as
`out_of_stock` once a second, separately batched read came back the same way, on the
reasoning that the shape was stable when it was real. The later measurement below shows the
two reads are not independent evidence about a shelf, and that Whole Foods never states a
negative at all -- so the page's "Out of Stock" is the page inferring from the same null.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.normalize.availability import IN_STOCK, OUT_OF_STOCK, UNKNOWN
from app.retailers.base import ProductListing
from app.retailers.wholefoods.adapter import resolve_availability, wwos_signal

FIXTURES = Path(__file__).parent / "fixtures" / "wholefoods"
RECORDS: dict[str, Any] = {
    record["asin"]: record for record in json.loads((FIXTURES / "wwos_products.json").read_text())
}
NO_OFFER_BANANA = "B0014GPSKQ"  # "Organic Banana, 1 Bunch (4-5 Count)"
ORDERABLE_BANANA = "B0787Y4V6T"  # "Organic Banana, 1 Each"


def _listing(sku: str) -> ProductListing:
    return ProductListing(
        retailer_sku=sku,
        title=sku,
        store_external_id="10151",
        price=Decimal("0.99"),
        regular_price=Decimal("0.99"),
    )


def _resolve(skus: list[str], first: dict[str, Any], confirm: dict[str, Any] | None) -> list[str]:
    resolved = resolve_availability([_listing(sku) for sku in skus], first, confirm)
    return [item.availability for item in resolved]


def test_a_live_offer_listing_is_the_positive_signal() -> None:
    assert wwos_signal(RECORDS[ORDERABLE_BANANA]) == "positive"
    assert wwos_signal(RECORDS[NO_OFFER_BANANA]) == "no_offer"
    assert wwos_signal(None) == "absent"


def test_the_orderable_banana_is_in_stock() -> None:
    """B0787Y4V6T: `IN_STOCK`, a price, a delivery window, and an offer listing to buy."""
    states = _resolve([ORDERABLE_BANANA], RECORDS, RECORDS)

    assert states == [IN_STOCK]


def test_the_banana_with_no_offer_is_unknown_not_out_of_stock() -> None:
    """B0014GPSKQ, the ASIN from the original report: no availability and no offer at all.

    Its own page prints "Out of Stock" over this, and a second read of it came back the same.
    Neither makes it a statement about the shelf -- see the flapping measurement below.
    """
    assert _resolve([NO_OFFER_BANANA], RECORDS, RECORDS) == [UNKNOWN]


def test_a_second_read_that_finds_an_offer_makes_it_in_stock() -> None:
    rescued = {NO_OFFER_BANANA: RECORDS[ORDERABLE_BANANA] | {"asin": NO_OFFER_BANANA}}

    states = _resolve([NO_OFFER_BANANA], RECORDS, rescued)

    assert states == [IN_STOCK]


def test_no_offer_stays_unknown_when_the_second_read_never_ran() -> None:
    """A re-read that failed or was never offered leaves the absent answer absent."""
    assert _resolve([NO_OFFER_BANANA], RECORDS, None) == [UNKNOWN]
    assert _resolve([NO_OFFER_BANANA], RECORDS, {}) == [UNKNOWN]


def test_an_asin_the_endpoint_never_answered_for_is_unknown() -> None:
    assert _resolve(["B000NOTHING"], RECORDS, RECORDS) == [UNKNOWN]


def test_a_price_without_a_way_to_order_is_not_stock() -> None:
    """ "Being listed or priced is not being in stock" -- there is no offer listing here."""
    priced_only = {
        "asin": "B00PRICED1",
        "availability": None,
        "offerDetails": {"price": {"priceAmount": 4.99}},
    }

    assert wwos_signal(priced_only) == "ambiguous"
    assert _resolve(["B00PRICED1"], {"B00PRICED1": priced_only}, {"B00PRICED1": priced_only}) == [
        UNKNOWN
    ]


def test_a_stated_negative_is_the_only_negative_believed() -> None:
    """Mapped defensively: Whole Foods has never stated one, and if it starts, this reads it."""
    stated = {"asin": "B00STATED1", "availability": "OUT_OF_STOCK", "offerDetails": None}

    assert wwos_signal(stated) == "stated_negative"
    assert _resolve(["B00STATED1"], {"B00STATED1": stated}, None) == [OUT_OF_STOCK]


def test_an_availability_word_nobody_mapped_never_reaches_in_stock() -> None:
    hedged = {"asin": "B00HEDGED1", "availability": "LOW_STOCK", "offerDetails": None}

    assert _resolve(["B00HEDGED1"], {"B00HEDGED1": hedged}, {"B00HEDGED1": hedged}) == [UNKNOWN]


def test_the_retailers_own_wording_is_kept_next_to_the_verdict() -> None:
    resolved = resolve_availability(
        [_listing(NO_OFFER_BANANA), _listing(ORDERABLE_BANANA)], RECORDS, RECORDS
    )

    assert resolved[0].stock_status == "availability=None no offer"
    assert resolved[1].stock_status == "availability=IN_STOCK"


def test_the_positive_signal_is_fulfillment_not_a_stated_state() -> None:
    """The requirement's "delivery window / ordering availability" case.

    A record can carry a live offer listing and a delivery promise while stating no
    availability at all -- 3 of 574 measured records did. That is somewhere to click "add to
    basket", which is a first-party statement that the item can be had, so it is `in_stock`
    without a confirming read. A price on its own never is.
    """
    orderable = RECORDS[ORDERABLE_BANANA]
    assert orderable["offerDetails"]["offerListingId"], "the captured record carries one"
    assert orderable["deliveryPromiseHtml"], "and a delivery window"

    silent = {**orderable, "asin": "B00SILENT1", "availability": None}

    assert wwos_signal(silent) == "positive"
    # No confirming read is offered, and it still resolves without one.
    assert _resolve(["B00SILENT1"], {"B00SILENT1": silent}, None) == [IN_STOCK]


def test_a_delivery_promise_without_a_way_to_order_is_not_enough() -> None:
    """A promise about delivery in general is not an offer of this item at this store."""
    promise_only = {
        "asin": "B00PROMISE1",
        "availability": None,
        "deliveryPromiseHtml": RECORDS[ORDERABLE_BANANA]["deliveryPromiseHtml"],
        "offerDetails": {"price": {"priceAmount": 1.99}},
    }

    assert wwos_signal(promise_only) == "ambiguous"
    assert _resolve(["B00PROMISE1"], {"B00PROMISE1": promise_only}, None) == [UNKNOWN]


# -- Regression: the empty record is an absent answer, not a negative -------------------
#
# Measured at store 10717 (Stonestown), 30 ASINs x 12 reads of one batch, 2026-09-13:
# 14 of them came back with a live offer on some reads and with nothing on others (one on 1
# read of 12, another on 11 of 12), 13 were never positive, 3 always were. Neither a slower
# cadence (15s apart) nor a single-ASIN batch changed the rate. So for an item whose offer
# appears on a fraction `p` of reads, two empty reads agree with probability (1 - p)^2 --
# 85% at p=0.08. Agreement measured the endpoint, not the shelf.
#
# `wwos_products_flapping.json` is that measurement: for B07YFT8JTH it holds an empty read
# and a live-offer read of the same ASIN at the same store, minutes apart.
FLAPPING: dict[str, Any] = json.loads((FIXTURES / "wwos_products_flapping.json").read_text())
BARE = FLAPPING["bare_negative_reads"]
LIVE = FLAPPING["live_offer_reads"]
CHICKEN_365 = "B07814PS49"  # 365 by Whole Foods Market Boneless Skinless Chicken Breast
CHICKEN_MARYS = "B07YFT8JTH"  # Mary's Chicken Boneless Skinless Heirloom Chicken Breast


def test_an_empty_record_is_unknown_however_many_times_it_is_read() -> None:
    """The reported bug: two absent answers were being read as a statement of absence."""
    assert _resolve([NO_OFFER_BANANA], RECORDS, RECORDS) == [UNKNOWN]


def test_the_365_chicken_breast_is_not_out_of_stock() -> None:
    """B07814PS49, the ASIN from the report: bare-null at every store measured.

    Never once positive over 26 reads across six Bay Area stores, yet its own page sells it
    for delivery. Whichever of those is true of the shelf, the endpoint did not say, so the
    honest answer is `unknown` -- and it is emphatically not `out_of_stock`.
    """
    empty = {CHICKEN_365: BARE[CHICKEN_365]}

    assert _resolve([CHICKEN_365], empty, empty) == [UNKNOWN]


def test_the_marys_chicken_breast_flaps_between_a_live_offer_and_nothing() -> None:
    """B07YFT8JTH: both of these are real reads of one ASIN at one store, minutes apart."""
    live = {CHICKEN_MARYS: LIVE[CHICKEN_MARYS]}
    empty = {CHICKEN_MARYS: BARE[CHICKEN_MARYS]}

    assert _resolve([CHICKEN_MARYS], live, live) == [IN_STOCK]
    assert _resolve([CHICKEN_MARYS], empty, empty) == [UNKNOWN]


def test_a_second_read_may_still_rescue_a_positive() -> None:
    """Re-asking is worth keeping -- to find an offer the first read missed, never to deny one."""
    empty = {CHICKEN_MARYS: BARE[CHICKEN_MARYS]}
    live = {CHICKEN_MARYS: LIVE[CHICKEN_MARYS]}

    assert _resolve([CHICKEN_MARYS], empty, live) == [IN_STOCK]


def test_a_hypothetical_stated_negative_for_the_marys_chicken_would_be_believed() -> None:
    """The requested case "B07YFT8JTH -> out_of_stock when explicitly unavailable", and the
    reason it can only be written like this.

    **The record below is fabricated**, by splicing `availability: "OUT_OF_STOCK"` onto that
    ASIN's real empty read. Whole Foods has never returned a stated negative for it or for
    anything else (0 in ~400 observations), so there is no captured payload to assert on: the
    product page's "Currently unavailable / Out of Stock" is the page's own inference, and the
    same page sold the item minutes later. What the live reads resolve to is `unknown`, which
    `test_the_marys_chicken_breast_flaps_between_a_live_offer_and_nothing` pins. So this
    proves only the rule -- if the endpoint ever states a negative, it is taken -- and must
    not be read as evidence that this product is out of stock anywhere.
    """
    fabricated = {**BARE[CHICKEN_MARYS], "availability": "OUT_OF_STOCK"}

    assert _resolve([CHICKEN_MARYS], {CHICKEN_MARYS: fabricated}, None) == [OUT_OF_STOCK]


def test_a_stated_negative_outranks_an_offer_listing_that_contradicts_it() -> None:
    """A record can carry both. The retailer's own word about stock is the one to believe.

    This is the same precedence `availability_from_flag` applies to every other retailer --
    "a retailer that says `available: false` is out of stock whatever its level says" -- and
    it matters more here than anywhere, because a stated negative is the *only* route left to
    `out_of_stock` for Whole Foods. Read the other way, an offer whose own stored wording
    says `OUT_OF_STOCK` could lead a card and enter a basket.
    """
    contradictory = {
        "asin": "B00CONFLICT",
        "availability": "OUT_OF_STOCK",
        "offerDetails": {"offerListingId": "L1", "price": {"priceAmount": 4.99}},
    }

    assert wwos_signal(contradictory) == "stated_negative"
    assert _resolve(["B00CONFLICT"], {"B00CONFLICT": contradictory}, None) == [OUT_OF_STOCK]


def test_a_reread_that_finds_a_stated_word_records_it_rather_than_the_first_silence() -> None:
    """The second read is the only place a word Whole Foods has never used would show up."""
    empty = {CHICKEN_MARYS: BARE[CHICKEN_MARYS]}
    hedged = {CHICKEN_MARYS: {**BARE[CHICKEN_MARYS], "availability": "LOW_STOCK"}}

    resolved = resolve_availability([_listing(CHICKEN_MARYS)], empty, hedged)

    assert resolved[0].availability == UNKNOWN
    assert resolved[0].stock_status == "availability=LOW_STOCK"
