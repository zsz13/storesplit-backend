"""The Instacart storefront's own words decide stock, not its `stockLevel` token.

Lucky product 19830961 is the regression this file exists for. StoreSplit showed it
`in_stock` while the storefront's product page said it was not, because the payload's
`stockLevel` is `lowStock` and StoreSplit read that token as "low but buyable". On this
platform `lowStock` is rendered to the shopper as **"Likely out of stock"** -- the wording
lives in `availability.viewSection.stockLevelLabelString`, which is the string the product
page itself displays. Reading the token and ignoring the label is what promoted an item the
retailer was hedging about into a buyable offer.

The payloads here are real captures from `shop.luckysupermarkets.com` (see
`tests/fixtures/lucky/`), one per availability shape the platform emits.
"""

import json
from pathlib import Path

import pytest
from app.normalize.availability import IN_STOCK, OUT_OF_STOCK, UNKNOWN
from app.retailers.instacart_storefront import storefront_availability
from app.retailers.savemartco.storefront import LUCKY_BANNER, parse_items

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def by_sku(listings: list, sku: str):
    return next(item for item in listings if item.retailer_sku == sku)


# ------------------------------------------------------------------ the resolution itself


def block(available: object, level: object, label: object = None) -> dict:
    return {
        "available": available,
        "stockLevel": level,
        "viewSection": {"stockLevelLabelString": label},
    }


class TestStorefrontAvailability:
    def test_likely_out_of_stock_is_never_in_stock(self) -> None:
        """The exact shape Lucky 19830961 returns at shop 23130."""
        raw, state = storefront_availability(block(True, "lowStock", "Likely out of stock"))
        assert state == UNKNOWN
        assert raw == "Likely out of stock"

    def test_an_unavailable_flag_wins_over_a_stale_in_stock_level(self) -> None:
        """Lucky 19830961 at shop 7705: `available` false, `stockLevel` still "inStock"."""
        _, state = storefront_availability(block(False, "inStock", "Out of stock"))
        assert state == OUT_OF_STOCK

    def test_plain_in_stock_carries_no_label(self) -> None:
        _, state = storefront_availability(block(True, "inStock", None))
        assert state == IN_STOCK

    def test_many_in_stock_is_in_stock(self) -> None:
        raw, state = storefront_availability(block(True, "highlyInStock", "Many in stock"))
        assert state == IN_STOCK
        assert raw == "Many in stock"

    def test_out_of_stock_label_alone_is_out_of_stock(self) -> None:
        _, state = storefront_availability(block(None, None, "Out of stock"))
        assert state == OUT_OF_STOCK

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"available": None, "stockLevel": None},
            block(True, None, None),
            block(None, "inStock", None),  # a level with no flag behind it
            block(True, "someNewLevel", None),
            block(True, "inStock", "Some wording we have never seen"),
        ],
    )
    def test_anything_we_cannot_read_is_unknown(self, payload: dict) -> None:
        """A shape we do not recognise is `unknown`. It is never promoted to `in_stock`."""
        _, state = storefront_availability(payload)
        assert state == UNKNOWN

    def test_a_lowstock_level_is_unknown_even_without_a_label(self) -> None:
        """The token on its own is a hedge, so it cannot carry an item to `in_stock`."""
        _, state = storefront_availability(block(True, "lowStock", None))
        assert state == UNKNOWN


# ------------------------------------------------------------- the captured Lucky payloads


def test_lucky_19830961_is_not_in_stock_at_shop_23130() -> None:
    """The regression: the storefront hedges, so StoreSplit must not call it buyable."""
    listings = parse_items(
        load("lucky/items_chicken_breast_94105.json"),
        "23130",
        "lucky:graphql/Items",
        banner=LUCKY_BANNER,
    )
    chicken = by_sku(listings, "19830961")
    assert chicken.availability != IN_STOCK
    assert chicken.availability == UNKNOWN
    assert chicken.stock_status == "Likely out of stock"


def test_lucky_19830961_is_out_of_stock_where_the_shop_says_so() -> None:
    """Same product, the shop the reported product page resolved to."""
    listings = parse_items(
        load("lucky/items_19830961_out_of_stock.json"),
        "7705",
        "lucky:graphql/Items",
        banner=LUCKY_BANNER,
    )
    chicken = by_sku(listings, "19830961")
    assert chicken.availability == OUT_OF_STOCK
    assert chicken.stock_status == "Out of stock"


def test_the_genuinely_stocked_items_in_the_same_payload_survive() -> None:
    """Correctness must not cost every offer: real in-stock items stay `in_stock`."""
    listings = parse_items(
        load("lucky/items_chicken_breast_94105.json"),
        "23130",
        "lucky:graphql/Items",
        banner=LUCKY_BANNER,
    )
    assert by_sku(listings, "19831039").availability == IN_STOCK  # stockLevel inStock
    assert by_sku(listings, "27178749").availability == IN_STOCK  # highlyInStock


def test_lucky_prefers_the_canonical_slug_the_storefront_itself_uses() -> None:
    """`evergreenUrl` is the slug the product page redirects to; prefer it over the bare id."""
    listings = parse_items(
        load("lucky/items_chicken_breast_94105.json"),
        "23130",
        "lucky:graphql/Items",
        banner=LUCKY_BANNER,
    )
    assert by_sku(listings, "19830961").product_url == (
        "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/"
        "19830961-boneless-skinless-master-cut-chicken-breast-3-5-lb"
    )


# ------------------------------------------------------------- the platform's other banners


def test_savemart_reads_its_own_payload_not_luckys() -> None:
    """A genuine Save Mart capture, so the banner is not proven only by relabelling Lucky."""
    from app.retailers.savemartco.storefront import SAVEMART_BANNER

    listings = parse_items(
        load("savemart/items_eggs_95350.json"),
        "7971",
        "savemart:graphql/Items",
        banner=SAVEMART_BANNER,
    )
    assert listings
    for item in listings:
        assert item.product_url is not None
        assert item.product_url.startswith("https://shop.savemart.com/store/savemart/products/")
    many = by_sku(listings, "20227964")
    assert many.availability == IN_STOCK and many.stock_status == "Many in stock"
    plain = by_sku(listings, "21825938")
    assert plain.availability == IN_STOCK and plain.stock_status == "inStock"


def test_sprouts_reads_the_label_out_of_its_own_payload() -> None:
    """Sprouts' parser must reach the same nested key, not merely survive a flat one."""
    from app.retailers.sprouts.adapter import parse_items as sprouts_items

    payload = load("sprouts/items_eggs_viewsection.json")
    assert "viewSection" in (payload["data"]["items"][0]["availability"])  # the real shape
    listings = sprouts_items(payload, "26417")
    assert listings
    for item in listings:
        assert item.availability == IN_STOCK

    # Same payload, one item hedged the way the platform spells it.
    payload["data"]["items"][0]["availability"]["stockLevel"] = "lowStock"
    payload["data"]["items"][0]["availability"]["viewSection"]["stockLevelLabelString"] = (
        "Likely out of stock"
    )
    hedged = sprouts_items(payload, "26417")[0]
    assert hedged.availability == UNKNOWN
    assert hedged.stock_status == "Likely out of stock"


# ------------------------------------------------------------------ product URL precedence


def test_an_explicit_canonical_url_beats_the_evergreen_slug() -> None:
    """Both signals present at once — the precedence the fixtures never exercise together."""
    payload = load("lucky/items_chicken_breast_94105.json")
    item = payload["data"]["items"][0]
    assert item["evergreenUrl"]  # the slug is really there, so this proves an order
    item["productCanonicalUrl"] = {
        "canonicalUrl": "/store/lucky-supermarkets/products/canonical-wins"
    }
    listing = parse_items(payload, "23130", "lucky:test", banner=LUCKY_BANNER)[0]
    assert listing.product_url == (
        "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/canonical-wins"
    )


@pytest.mark.parametrize(
    "evergreen",
    ["..", "../..", "null", "undefined", "n/a", "", "   ", "a/b", "https://evil.test/x", 42, None],
)
def test_a_payload_slug_that_is_not_a_slug_falls_back_to_the_product_id(
    evergreen: object,
) -> None:
    """`clean_product_url` checks the assembled URL, so ".." would pass as a store page."""
    payload = load("lucky/items_chicken_breast_94105.json")
    payload["data"]["items"][0]["evergreenUrl"] = evergreen
    listing = parse_items(payload, "23130", "lucky:test", banner=LUCKY_BANNER)[0]
    assert listing.product_url == (
        "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/19830961"
    )


def test_a_negative_level_is_never_overridden_into_in_stock_by_a_label() -> None:
    """The two disagreeing must not promote: the code and its docstring have to agree."""
    _, state = storefront_availability(block(True, "outOfStock", "Many in stock"))
    assert state == UNKNOWN
    _, agreed = storefront_availability(block(True, "outOfStock", "Out of stock"))
    assert agreed == OUT_OF_STOCK
