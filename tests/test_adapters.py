import json
from decimal import Decimal
from pathlib import Path

from app.retailers.kroger.adapter import parse_locations, parse_products
from app.retailers.wholefoods.adapter import (
    index_wwos,
    parse_product_page,
    parse_search_results,
    resolve_availability,
)
from app.retailers.wholefoods.stores import find_stores_for_zip, store_from_summary

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_wholefoods_search_parsing() -> None:
    listings = parse_search_results(load("wholefoods/search_eggs.json"), "10151")
    assert listings, "fixture should yield listings"
    pete = next(item for item in listings if "PETE" in (item.brand or "").upper())
    assert pete.title == "Large Grade A Eggs, 12 CT"
    assert pete.price == Decimal("6.79") and pete.regular_price == Decimal("6.79")
    assert pete.size_text == "12 CT" and pete.sold_by == "unit"
    assert pete.retailer_sku.startswith("B0") and pete.product_url == (
        "https://www.wholefoodsmarket.com/grocery/product/"
        "pete-gerrys-large-grade-a-eggs-12-ct-b0bt377kp2"
    )
    assert pete.store_external_id == "10151" and pete.source == "wholefoods:api/search"


def test_wholefoods_sale_price_and_counter_items() -> None:
    chicken = parse_search_results(load("wholefoods/search_chicken_breast.json"), "10151")
    counter = next(i for i in chicken if i.title == "Organic Boneless Skinless Chicken Breast")
    assert counter.sold_by == "weight" and counter.brand is None and counter.size_text is None
    branded = next(i for i in chicken if i.title == "Boneless Skinless Chicken Breast")
    assert branded.sold_by == "weight" and branded.brand == "Pine Manor"  # payload uom == lb
    rice = parse_search_results(load("wholefoods/search_rice.json"), "10151")
    bulk = next(i for i in rice if i.sold_by == "weight")
    assert bulk.size_text is None and bulk.brand
    boxed = next(i for i in rice if i.size_text)
    assert boxed.sold_by == "unit"
    kabob = next(i for i in chicken if "Kabob" in i.title)
    assert kabob.price == Decimal("7.49") and kabob.regular_price == Decimal("8.99")
    bananas = parse_search_results(load("wholefoods/search_bananas.json"), "10151")
    banana = next(i for i in bananas if i.title == "Banana")
    assert banana.sold_by == "weight" and banana.price == Decimal("0.59")


def test_wholefoods_product_page_parsing() -> None:
    html = (FIXTURES / "wholefoods/product_page.html").read_text()
    data = parse_product_page(html)
    assert data is not None
    assert data["asin"] == "B0BT377KP2" and data["brand"] == "Pete & Gerry's"
    assert data["availability"] == "IN_STOCK" and data["unit_price_unit"] == "ounce"


def test_wholefoods_store_summary_and_zip_lookup() -> None:
    store = store_from_summary(load("wholefoods/store_summary.json"))
    assert store is not None
    assert store.external_id == "10151" and store.zip_code == "94107" and store.state == "CA"
    nearby = find_stores_for_zip("94105")
    assert nearby and all(s.zip_code and s.zip_code.startswith("941") for s in nearby)
    # Both SoMa (10151) and Potrero Hill (10238) are in 94107; Potrero Hill is nearer its centroid.
    assert [s.external_id for s in find_stores_for_zip("94107")[:2]] == ["10238", "10151"]


def test_kroger_products_parsing() -> None:
    listings = parse_products(load("kroger/products_eggs.json"), "70300175")
    assert len(listings) == 3  # the product without a price is dropped
    eggs = listings[0]
    assert eggs.retailer_sku == "0001111060903" and eggs.gtin == "0001111060903"
    assert eggs.brand == "Kroger" and eggs.size_text == "12 ct"
    assert eggs.regular_price == Decimal("2.99") and eggs.price == Decimal("2.99")
    assert eggs.loyalty_price == Decimal("2.49") and eggs.stock_status == "high"
    assert eggs.image_url and eggs.product_url.endswith("/0001111060903")
    by_weight = listings[1]
    assert by_weight.sold_by == "weight" and by_weight.loyalty_price is None
    no_discount = listings[2]
    assert no_discount.price == Decimal("4.49") and no_discount.loyalty_price is None
    assert no_discount.stock_status == "low"


def test_kroger_locations_parsing() -> None:
    stores = parse_locations(load("kroger/locations.json"))
    assert [s.external_id for s in stores] == ["70300175"]
    assert stores[0].zip_code == "90012" and stores[0].name.startswith("Ralphs")


def test_smartandfinal_search_parsing() -> None:
    from app.retailers.smartandfinal.adapter import parse_search_results

    listings = parse_search_results(load("smartandfinal/search_eggs.json"), "351")
    assert len(listings) == 5
    eggs = next(i for i in listings if i.retailer_sku == "00715141514643")
    assert (
        eggs.title == "Eggland's Best Brown Cage Free Large Eggs" and eggs.brand == "Eggland's Best"
    )
    assert eggs.price == Decimal("5.99") and eggs.regular_price == Decimal("5.99")
    assert eggs.size_text == "12 ct" and eggs.sold_by == "unit" and eggs.gtin == "00715141514643"
    assert eggs.stock_status == "high" and eggs.store_external_id == "351"
    assert eggs.source == "smartandfinal:api/search" and eggs.loyalty_price is None
    sale = next(i for i in listings if i.attributes["price_source"] == "tpr")
    assert sale.price == Decimal("7.59") and sale.regular_price == Decimal("8.59")


def test_smartandfinal_maps_its_own_stock_levels_rather_than_guessing() -> None:
    """ "low" means low-but-there at Smart & Final, and the adapter has to say so itself.

    The same word is a hedge elsewhere -- on the Instacart storefront `lowStock` is the
    page's "Likely out of stock" -- so nothing generic may decide it. Smart & Final keeps a
    separate "out", and an item its search calls "low" comes back "high" from its own
    product endpoint a scrape later, so here it is a quantity still on the shelf.
    """
    from app.retailers.smartandfinal.adapter import parse_search_results

    payload = load("smartandfinal/search_eggs.json")

    def state_for(stock: object) -> tuple[str, str | None]:
        item = json.loads(json.dumps(payload["items"][0]))
        item["attributes"] = {**(item.get("attributes") or {}), "Stock Status": stock}
        item["available"] = True
        listing = parse_search_results({**payload, "items": [item]}, "351")[0]
        return listing.availability, listing.stock_status

    assert state_for("plenty") == ("in_stock", "high")
    assert state_for("low") == ("in_stock", "low")
    assert state_for("out") == ("out_of_stock", "out")
    # A level nobody has mapped falls back to the retailer's own boolean, and is kept raw.
    assert state_for("mystery") == ("in_stock", "mystery")


def test_smartandfinal_weighed_and_sized_items() -> None:
    from app.retailers.smartandfinal.adapter import parse_search_results

    chicken = parse_search_results(load("smartandfinal/search_chicken_breast.json"), "351")
    weighed = next(i for i in chicken if i.retailer_sku == "00282035000006")
    # "$12.68 avg/ea" for a 4.24 lb pack at $2.99/lb on sale (was $4.29/lb): shelf price is $/lb.
    assert weighed.sold_by == "weight" and weighed.size_text is None
    assert weighed.price == Decimal("2.99") and weighed.regular_price == Decimal("4.29")
    boxed = next(i for i in chicken if i.retailer_sku == "00023700027498")
    assert boxed.sold_by == "unit" and boxed.size_text == "10 lb" and boxed.stock_status == "out"
    ounces = next(i for i in chicken if i.retailer_sku == "00041512100840")
    assert ounces.size_text == "10 oz" and ounces.price == Decimal("2.99")
    bananas = parse_search_results(load("smartandfinal/search_bananas.json"), "351")
    banana = next(i for i in bananas if i.retailer_sku == "00000000004011")
    assert banana.sold_by == "weight" and banana.price == Decimal("0.59") and banana.brand is None


def test_smartandfinal_product_and_stores() -> None:
    from app.retailers.smartandfinal.adapter import listing_from_item, parse_stores
    from app.retailers.zipmatch import rank_stores_by_zip

    product = listing_from_item(
        load("smartandfinal/product.json"), "522", "smartandfinal:api/products"
    )
    assert product is not None and product.retailer_sku == "00715141514643"
    assert product.store_external_id == "522" and product.size_text == "12 ct"
    stores = parse_stores(load("smartandfinal/stores.json"))
    assert "999" not in {s.external_id for s in stores}  # closed store dropped
    sf = next(s for s in stores if s.external_id == "351")
    assert sf.name == "Smart & Final San Francisco - Seventh Ave" and sf.zip_code == "94118"
    assert sf.city == "San Francisco" and sf.state == "CA" and sf.latitude
    ranked = rank_stores_by_zip(stores, "94118")
    assert ranked[0].external_id == "351"
    assert "320" in {s.external_id for s in ranked}  # Daly City (940xx) is within 30 miles
    assert "304" not in {s.external_id for s in ranked}  # Glendale is not
    assert next(iter(rank_stores_by_zip(stores, "94611"))).external_id == "445"


def test_traderjoes_search_parsing() -> None:
    from app.retailers.traderjoes.adapter import parse_products

    listings = parse_products(
        load("traderjoes/search_eggs.json"), "100", "traderjoes:graphql/SearchProducts"
    )
    eggs = next(i for i in listings if i.retailer_sku == "062124")
    assert eggs.title == "Pasture Raised Large Brown Eggs" and eggs.brand == "Trader Joe's"
    assert eggs.price == Decimal("5.99") == eggs.regular_price and eggs.loyalty_price is None
    assert eggs.size_text == "1 dozen" and eggs.sold_by == "unit" and eggs.gtin is None
    # `availability` is "1" on the whole catalogue, so it is carriage rather than stock;
    # the raw wording is still recorded. See tests/test_traderjoes_urls.py.
    assert eggs.availability == "unknown" and eggs.stock_status == "availability=1"
    assert eggs.store_external_id == "100"
    assert eggs.product_url == ("https://www.traderjoes.com/home/products/pdp/062124")
    assert eggs.image_url and eggs.image_url.startswith("https://www.traderjoes.com/content/dam/")
    butter = next(i for i in listings if i.retailer_sku == "006252")
    assert butter.size_text == "16 oz"
    chicken = parse_products(load("traderjoes/search_chicken_breast.json"), "100", "x")
    per_lb = next(i for i in chicken if i.retailer_sku == "063550")
    assert (
        per_lb.sold_by == "weight" and per_lb.size_text is None and per_lb.price == Decimal("7.49")
    )
    packaged = next(i for i in chicken if i.retailer_sku == "083524")
    assert packaged.sold_by == "unit" and packaged.size_text == "12 oz"
    bananas = parse_products(load("traderjoes/search_bananas.json"), "100", "x")
    banana = next(i for i in bananas if i.retailer_sku == "048053")
    assert banana.size_text == "1 ct" and banana.price == Decimal("0.23")


def test_traderjoes_product_and_locator() -> None:
    from app.retailers.traderjoes.adapter import (
        locator_request,
        parse_locator_results,
        parse_products,
    )

    product = parse_products(
        load("traderjoes/product.json"), "100", "traderjoes:graphql/SearchProduct"
    )
    assert len(product) == 1 and product[0].retailer_sku == "062124"
    stores = parse_locator_results(load("traderjoes/locator_94110.json"))
    assert [s.external_id for s in stores] == [
        "78",
        "226",
        "225",
        "100",
        "200",
    ]  # coming-soon dropped
    first = stores[0]
    assert first.name == "Trader Joe's San Francisco - 9th St (78)" and first.zip_code == "94103"
    assert first.city == "San Francisco" and first.latitude and first.longitude
    body = locator_request("94110")
    assert body["request"]["formdata"]["geolocs"]["geoloc"][0]["addressline"] == "94110"


def test_ranch99_search_and_stores() -> None:
    from app.retailers.ranch99.adapter import parse_search_results, parse_stores

    eggs = parse_search_results(load("ranch99/search_eggs.json"), "1769")
    assert len(eggs) == 4
    quail = eggs[0]
    assert quail.retailer_sku == "1630908" and quail.gtin == "673367309088"
    assert quail.title == "Asn/Tas Canned Quail Egg" and quail.brand == "ASN/TAS"
    assert quail.price == Decimal("2.49") and quail.regular_price == Decimal("3.69")  # sale
    assert quail.size_text == "15 oz" and quail.sold_by == "unit"
    # 99 Ranch reports a per-store quantity; the raw count is kept next to the state.
    assert quail.availability == "in_stock" and quail.stock_status == "available=75"
    assert quail.store_external_id == "1769" and quail.source == "ranch99:be-api/search"
    salted = eggs[1]
    assert salted.price == salted.regular_price == Decimal("8.59") and salted.size_text == "6 ct"
    milk = parse_search_results(load("ranch99/search_milk.json"), "1769")
    assert [m.size_text for m in milk] == ["13.5 fl oz", "0.5 gal", "12 fl oz"]
    stores = parse_stores(load("ranch99/stores_94110.json"))
    assert [s.external_id for s in stores] == ["1769", "1781", "1782", "1762"]
    assert stores[0].name == "99 Ranch Market Daly City" and stores[0].zip_code == "94015"
    assert stores[0].latitude and stores[0].longitude and stores[0].state == "CA"


def test_ranch99_image_object_is_read_as_a_url() -> None:
    """`productImage` arrives as {"type": 0, "path": <url>}; a dict reached a String column."""
    from app.retailers.ranch99.adapter import parse_search_results

    payload = load("ranch99/search_eggs.json")
    items = payload["data"]["list"]
    items[0].pop("image", None)
    items[0]["productImage"] = {"type": 0, "path": "https://img.99ranch.test/a.JPG"}
    items[1]["image"] = ""
    items[1]["productImage"] = {"type": 0, "path": ""}
    listings = parse_search_results(payload, "1769")
    assert listings[0].image_url == "https://img.99ranch.test/a.JPG"
    assert listings[1].image_url is None  # an empty path is no image, not an empty string


def test_ranch99_product_page_parsing() -> None:
    from app.retailers.ranch99.adapter import parse_product_page

    html = (FIXTURES / "ranch99/product_page.html").read_text()
    product = parse_product_page(html, "1769")
    assert product is not None and product.retailer_sku == "1630908"
    assert product.gtin == "673367309088" and product.brand == "ASN/TAS"
    assert product.price == Decimal("2.49") and product.regular_price == Decimal("3.69")
    assert product.source == "ranch99:product-page" and product.size_text == "15 oz"


def test_sprouts_items_parsing() -> None:
    from app.retailers.sprouts.adapter import parse_items, parse_search_item_ids

    ids = parse_search_item_ids(load("sprouts/search_eggs.json"))
    assert len(ids) == 7 and ids[0].startswith("items_19462-")
    eggs = parse_items(load("sprouts/items_eggs.json"), "357771")
    assert [e.retailer_sku for e in eggs] == ["18021451", "17264530", "3115497"]  # deduplicated
    dozen = eggs[0]
    assert (
        dozen.title.startswith("Vital Farms Pasture-Raised Large") and dozen.brand == "Vital Farms"
    )
    assert dozen.price == Decimal("7.99") == dozen.regular_price and dozen.size_text == "12 ct"
    assert dozen.gtin == "00861745000010" and dozen.stock_status == "inStock"
    assert dozen.sold_by == "unit" and dozen.source == "sprouts:graphql/Items"
    assert dozen.product_url.startswith("https://shop.sprouts.com/store/sprouts/products/")
    sale = eggs[2]
    assert sale.price == Decimal("10.49") and sale.regular_price == Decimal("12.49")
    chicken = parse_items(load("sprouts/items_chicken_breast.json"), "357771")
    per_lb = next(c for c in chicken if c.retailer_sku == "41327369")
    assert (
        per_lb.sold_by == "weight" and per_lb.price == Decimal("14.99") and per_lb.size_text is None
    )
    packaged = next(c for c in chicken if c.retailer_sku == "110027539")
    assert packaged.size_text == "20 oz" and packaged.regular_price == Decimal("14.99")


def test_sprouts_shops_parsing() -> None:
    from app.retailers.sprouts.adapter import parse_shops

    stores = parse_shops(load("sprouts/idp_shops_94110.json"))
    # One entry per physical store; Daly City keeps its in-store shop id, others their pickup id.
    assert [s.external_id for s in stores] == ["357771", "219", "28"]
    daly = stores[0]
    assert daly.name == "Sprouts Farmers Market Daly City (Store #276)" and daly.zip_code == "94015"
    assert (
        daly.address_line1 == "301 Gellert Blvd" and daly.city == "Daly City" and daly.state == "CA"
    )


# ------------------------------------------------- Whole Foods availability and product URLs


def test_wholefoods_search_never_claims_stock_it_was_not_told_about() -> None:
    """`/api/search` has no availability field, so a listing starts out `unknown`."""
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10145")
    assert listings
    assert all(item.availability == "unknown" for item in listings)
    assert all(item.stock_status is None for item in listings)


def test_wholefoods_product_urls_use_the_current_grocery_path() -> None:
    """`/product/<slug>` answers 301; the live page is `/grocery/product/<slug>`."""
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10145")
    for item in listings:
        assert item.product_url is not None
        assert item.product_url.startswith("https://www.wholefoodsmarket.com/grocery/product/")
        assert item.product_url.endswith(item.attributes["slug"])


def test_wholefoods_banana_maps_slug_to_asin_and_keeps_the_right_page() -> None:
    """The banana the report flagged: the SKU mapping was never wrong."""
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10145")
    banana = next(i for i in listings if i.title == "Organic Banana")
    assert banana.retailer_sku == "B0014GPSKQ"
    assert banana.product_url == (
        "https://www.wholefoodsmarket.com/grocery/product/produce-organic-banana-b0014gpskq"
    )
    assert banana.sold_by == "weight"  # payload uom == "lb"


def test_wholefoods_reports_unknown_because_it_publishes_carriage_not_stock() -> None:
    """Whole Foods' only store-scoped signal (`/api/product?store=`.isAvailable) is true for
    every item in that store's own search and false for the ones it does not carry, so it
    restates the search rather than reporting shelf inventory. Calling it `in_stock` would
    be the guess this project forbids -- and its product page renders for a default store
    in another state, which is what made a stocked item look out of stock."""
    for fixture in ("wholefoods/search_eggs.json", "wholefoods/search_bananas.json"):
        for item in parse_search_results(load(fixture), "10151"):
            assert item.availability == "unknown"
            assert item.stock_status is None


# --------------------------------------- Whole Foods store-scoped availability (wwos)


def test_wholefoods_wwos_availability_is_read_per_asin() -> None:
    """`/api/wwos/products?asins=...` is the signal the product page itself renders from.

    Unlike `/api/product?store=`.isAvailable (carriage, true for everything a store lists),
    this one varies inside a single store's own results: measured 305 IN_STOCK against 267
    not, across 574 ASINs from store 10151's own search.
    """
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10151")
    banana = next(i for i in listings if i.retailer_sku == "B0014GPSKQ")
    records = index_wwos(load("wholefoods/wwos_products.json"))

    resolved = resolve_availability([banana], records, records)

    assert len(resolved) == 1
    # The item from the original report: listed with a price, and answered for with no
    # availability and no offer at all. Its page prints "Out of Stock" over that; the
    # endpoint did not say so, and re-reading the same silence does not either.
    assert resolved[0].availability == "unknown"
    assert resolved[0].stock_status == "availability=None no offer"


def test_wholefoods_wwos_in_stock_is_applied() -> None:
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10151")
    conventional = next(i for i in listings if i.retailer_sku == "B07FYYKKQK")
    records = index_wwos(load("wholefoods/wwos_products.json"))

    resolved = resolve_availability([conventional], records, records)

    assert resolved[0].availability == "in_stock"
    assert resolved[0].stock_status == "availability=IN_STOCK"


def test_wholefoods_wwos_leaves_unmentioned_listings_untouched() -> None:
    """An ASIN the batch did not answer for keeps `unknown` rather than being guessed."""
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10151")
    plantain = next(i for i in listings if "Plantain" in i.title)
    records = index_wwos(load("wholefoods/wwos_products.json"))

    resolved = resolve_availability([plantain], records, records)

    assert resolved[0].availability == "unknown"
    assert resolved[0].stock_status is None


def test_wholefoods_wwos_handles_a_failed_lookup() -> None:
    listings = parse_search_results(load("wholefoods/search_bananas.json"), "10151")

    assert all(i.availability == "unknown" for i in resolve_availability(listings, {}, None))


def test_smartandfinal_image_is_a_size_the_cdn_actually_serves() -> None:
    """`template` carries a literal `{size}`; the CDN serves cell/detail/zoom and nothing else.

    Substituting `medium` -- which is what this adapter did -- 404s for every product, so
    every Smart & Final result rendered a placeholder instead of a photograph.
    """
    from app.retailers.smartandfinal.adapter import parse_search_results

    listings = parse_search_results(load("smartandfinal/search_eggs.json"), "351")
    images = [item.image_url for item in listings if item.image_url]
    assert images, "the eggs fixture has images"
    for url in images:
        assert "{" not in url and "}" not in url, f"unsubstituted template: {url}"
        assert "/medium/" not in url, f"medium is not a size the CDN serves: {url}"
        assert any(f"/{size}/" in url for size in ("cell", "detail", "zoom")), url
