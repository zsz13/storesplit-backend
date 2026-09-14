"""Parsing tests for the retailers added after the second access review: Safeway, Lucky /
Save Mart (Instacart storefront) and Raley's. Fixtures are trimmed real payloads."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.retailers import adapter_slugs
from app.retailers.raleys.adapter import (
    RaleysAdapter,
    store_cookie,
)
from app.retailers.raleys.adapter import (
    parse_product_page as parse_raleys_product,
)
from app.retailers.raleys.adapter import store_directory as raleys_stores
from app.retailers.safeway.adapter import (
    parse_product_page as parse_safeway_product,
)
from app.retailers.safeway.adapter import (
    parse_similar_products,
    parse_stores,
    seeds,
)
from app.retailers.savemartco.adapter import LuckyAdapter, SaveMartAdapter
from app.retailers.savemartco.storefront import (
    LUCKY_BANNER,
    SAVEMART_BANNER,
    parse_items,
    parse_search_item_ids,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def read(name: str) -> str:
    return (FIXTURES / name).read_text()


# --------------------------------------------------------------------------- Safeway


def test_safeway_store_parsing() -> None:
    stores = parse_stores(load("safeway/stores_94105.json"))
    assert stores, "fixture should yield stores"
    first = stores[0]
    assert first.external_id == "4601"
    assert first.address_line1 == "145 Jackson St"
    assert (first.city, first.state, first.zip_code) == ("San Francisco", "CA", "94111")
    # storeresolver returns them nearest first; the adapter keeps that order.
    assert [store.external_id for store in stores] == ["4601", "2606", "1490"]


def test_safeway_similar_products_parsing() -> None:
    shelf = seeds()["eggs"][0]["shelf"]
    listings = parse_similar_products(load("safeway/similar_eggs.json"), "4601", shelf)
    assert listings, "fixture should yield egg listings"
    for listing in listings:
        assert listing.store_external_id == "4601"
        assert listing.source == "safeway:xapi/aisles/similar-products"
        assert listing.gtin and listing.gtin.isdigit()
        assert listing.price > 0 and listing.regular_price >= listing.price
        assert listing.size_text and listing.size_text.endswith("ct")


def test_safeway_off_shelf_neighbours_are_dropped() -> None:
    payload = load("safeway/similar_eggs.json")
    payload["response"]["docs"][0]["shelfNameWithId"] = "Coffee - Ground|1_5_3_1"
    kept = parse_similar_products(payload, "4601", "Eggs|1_11_3_1")
    # similar-products is a recommender, so a doc off the seed's shelf is not the category.
    assert all(item.attributes["shelf"] == "Eggs|1_11_3_1" for item in kept)
    assert len(kept) == len(payload["response"]["docs"]) - 1
    assert len(parse_similar_products(payload, "4601", None)) == len(payload["response"]["docs"])


def test_safeway_half_gallon_label_becomes_gallons() -> None:
    listings = parse_similar_products(load("safeway/similar_milk.json"), "4601", None)
    half_gallons = [item for item in listings if item.size_text == "0.5 gal"]
    assert half_gallons, 'Safeway writes half gallons as "1 hg"; it must reach us as gallons'


def test_safeway_product_page_parsing() -> None:
    listing = parse_safeway_product(read("safeway/product_page.html"), "3132")
    assert listing is not None
    assert listing.retailer_sku == "970123451"
    assert listing.title == "Uncle Eddies Large White Eggs - 1 DZ"
    assert listing.price == Decimal("4.99") and listing.regular_price == Decimal("4.99")
    assert listing.gtin == "0002142895137" and listing.size_text == "12 ct"
    assert listing.sold_by == "unit" and listing.source == "safeway:product-page"
    assert listing.attributes["shelf"] == "Eggs|1_11_3_1"


def test_safeway_product_page_without_payload_is_none() -> None:
    assert parse_safeway_product("<html><body>nothing here</body></html>", "3132") is None


def test_safeway_seeds_cover_every_category() -> None:
    from app.normalize.categories import CATEGORIES

    for category in CATEGORIES.values():
        entries = seeds().get(category.search_query)
        assert entries, f"no Safeway seed for {category.key}"
        assert all(entry["pid"] and entry["shelf"] for entry in entries)


# ------------------------------------------------------- Lucky / Save Mart (Instacart)


def test_lucky_search_item_ids() -> None:
    ids = parse_search_item_ids(load("lucky/search_eggs.json"))
    assert ids and all(item_id.startswith("items_") for item_id in ids)
    assert len(ids) == len(set(ids)), "ids must be de-duplicated"


def test_lucky_item_parsing_recovers_package_size() -> None:
    listings = parse_items(
        load("lucky/items_eggs.json"), "24854", "lucky:graphql/Items", banner=LUCKY_BANNER
    )
    assert listings, "fixture should yield listings"
    for listing in listings:
        assert listing.store_external_id == "24854"
        assert listing.price > 0 and listing.regular_price >= listing.price
        assert listing.gtin is None  # not exposed to guests on these banners
    # The package size is exactly what the retired Swiftly search could not supply.
    sized = [item for item in listings if item.size_text]
    assert sized, "every Instacart item carries a size string"
    assert all(any(char.isdigit() for char in item.size_text or "") for item in sized)


def test_lucky_sale_price_is_below_regular() -> None:
    listings = parse_items(
        load("lucky/items_eggs.json"), "24854", "lucky:graphql/Items", banner=LUCKY_BANNER
    )
    on_sale = [item for item in listings if item.regular_price > item.price]
    assert on_sale, "fixture captured at least one struck-through price"


def test_lucky_product_urls_are_real_lucky_pages_never_serialized_objects() -> None:
    """Regression: `productCanonicalUrl` is an object, and stringifying it produced
    `{'id': ..., 'canonicalUrl': None, ...}` as the stored URL."""
    listings = parse_items(
        load("lucky/items_eggs_94105.json"), "23130", "lucky:graphql/Items", banner=LUCKY_BANNER
    )
    assert listings
    for listing in listings:
        assert listing.product_url is not None
        assert listing.product_url.startswith(
            "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/"
        )
        assert "{" not in listing.product_url and "canonicalUrl" not in listing.product_url
        assert listing.product_url.endswith(listing.retailer_sku)


def test_lucky_uses_the_canonical_url_string_when_the_payload_supplies_one() -> None:
    payload = load("lucky/items_eggs_94105.json")
    payload["data"]["items"][0]["productCanonicalUrl"]["canonicalUrl"] = (
        "/store/lucky-supermarkets/products/28294760-sunnyside-farms-cage-free-eggs"
    )
    listing = parse_items(payload, "23130", "lucky:graphql/Items", banner=LUCKY_BANNER)[0]
    assert listing.product_url == (
        "https://shop.luckysupermarkets.com"
        "/store/lucky-supermarkets/products/28294760-sunnyside-farms-cage-free-eggs"
    )


def test_lucky_ignores_a_canonical_url_pointing_at_another_host() -> None:
    payload = load("lucky/items_eggs_94105.json")
    payload["data"]["items"][0]["productCanonicalUrl"]["canonicalUrl"] = (
        "https://www.instacart.com/store/items/items_31528-28294760"
    )
    listing = parse_items(payload, "23130", "lucky:graphql/Items", banner=LUCKY_BANNER)[0]
    assert listing.product_url == (
        "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/28294760"
    )


def test_lucky_availability_comes_from_the_shop_the_search_used() -> None:
    listings = parse_items(
        load("lucky/items_eggs_94105.json"), "23130", "lucky:graphql/Items", banner=LUCKY_BANNER
    )
    by_sku = {item.retailer_sku: item for item in listings}
    assert by_sku["28294760"].availability == "in_stock"
    assert by_sku["28294760"].stock_status == "inStock"
    # "lowStock" is the storefront's "Likely out of stock", so it is not a buyable offer.
    # The retailer's own wording is kept for debugging either way.
    assert by_sku["21825938"].availability == "unknown"
    assert by_sku["21825938"].stock_status == "lowStock"
    assert by_sku["20227964"].availability == "out_of_stock"
    assert by_sku["20227964"].stock_status == "outOfStock"


def test_savemart_product_urls_use_the_savemart_banner() -> None:
    listings = parse_items(
        load("lucky/items_eggs_94105.json"),
        "23130",
        "savemart:graphql/Items",
        banner=SAVEMART_BANNER,
    )
    assert all(
        item.product_url is not None
        and item.product_url.startswith("https://shop.savemart.com/store/savemart/products/")
        for item in listings
    )


def test_lucky_default_shop_fixture_has_a_shop_id() -> None:
    payload = load("lucky/default_shop_94538.json")
    assert payload["data"]["defaultShop"]["id"] == "24854"
    assert payload["data"]["defaultShop"]["retailer"]["name"] == "Lucky Supermarkets"


def test_savemartco_banners_differ(clients) -> None:
    lucky, savemart = LuckyAdapter(clients), SaveMartAdapter(clients)
    assert (lucky.slug, lucky.banner.host) == ("lucky", "shop.luckysupermarkets.com")
    assert (savemart.slug, savemart.banner.host) == ("savemart", "shop.savemart.com")
    assert lucky.is_configured() and savemart.is_configured()


def test_each_banner_keeps_its_own_session_client(clients) -> None:
    """Guest session cookies are per host, so the banners must not share a cookie jar."""
    lucky, savemart = LuckyAdapter(clients), SaveMartAdapter(clients)
    assert lucky._storefront._client is not savemart._storefront._client
    assert lucky._storefront._client is not clients.shared()


# --------------------------------------------------------------------------- Raley's


def test_raleys_product_page_parsing() -> None:
    listing = parse_raleys_product(read("raleys/product_page.html"), "415")
    assert listing is not None
    assert listing.retailer_sku == "10700256" and listing.title == "Hass Avocado"
    assert listing.store_external_id == "415"
    assert listing.attributes["store_channel"] == "415"  # the cookie really selected the store
    assert listing.gtin == "00000000040464"
    assert listing.brand == "Hass"
    assert listing.price > 0 and listing.regular_price >= listing.price
    assert listing.source == "raleys:product-page"
    assert listing.image_url and listing.image_url.startswith("https://")


def test_raleys_single_count_size_is_dropped() -> None:
    # unitsPerPackage 1 with unitOfMeasure "ea" says nothing about the package.
    listing = parse_raleys_product(read("raleys/product_page.html"), "415")
    assert listing is not None and listing.size_text is None


def test_raleys_product_page_without_payload_is_none() -> None:
    assert parse_raleys_product("<html><body>no next data</body></html>", "415") is None


def test_raleys_store_cookie_shape() -> None:
    cookie = store_cookie("415")
    assert cookie.startswith("FLDR.User=")
    assert "storeId%3D415%3B" in cookie


async def test_raleys_stores_are_vendored_and_ranked(clients) -> None:
    stores = raleys_stores()
    assert len(stores) > 50, "the stores sitemap has ~115 stores"
    located = [store for store in stores if store.latitude and store.zip_code]
    assert len(located) > 0.8 * len(stores), "most stores must be geocoded to rank by ZIP"
    adapter = RaleysAdapter(clients)
    near_sacramento = await adapter.find_stores("95814")
    assert near_sacramento, "Raley's home market must resolve"
    assert all(store.external_id for store in near_sacramento)


def test_raleys_catalogue_covers_every_category() -> None:
    from app.normalize.categories import CATEGORIES
    from app.retailers.raleys.adapter import catalogue

    for category in CATEGORIES.values():
        assert catalogue().get(category.search_query), f"no Raley's products for {category.key}"


# --------------------------------------------------------------------------- registry


@pytest.mark.parametrize("slug", ["safeway", "lucky", "savemart", "raleys"])
def test_new_adapters_are_registered(slug: str) -> None:
    assert slug in adapter_slugs()
