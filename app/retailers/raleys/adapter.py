"""Raley's / Bel Air / Nob Hill adapter.

www.raleys.com disallows `/api`, `/search`, `/cart`, `/checkout` and the account paths in
robots.txt, but explicitly publishes `/sitemap/*` and serves `/product/*` — and those product
pages are fully server-rendered, so the adapter never touches a disallowed path:
  * GET /product/<productId>/<slug>
        `__NEXT_DATA__` carries the commercetools record: sku, name, the price channel (the
        store number), `regularPrice`, `discounted`, and `attributesRaw` with `primaryUPC`,
        `brand`, `unitOfMeasure`, `unitsPerPackage`, `packageCount`, `weightInPounds` and
        `unitSellType`.
  * cookie `FLDR.User=shopType=;storeId=<n>;postalCode=;sessionId=;device=desktop;`
        selects the store the page is priced for (confirmed: the same avocado is $2.99 at
        store 01 and $1.99 at store 415).
  * GET /sitemap/products-sitemap.xml -> 16 category sitemaps of every product URL, used by
        scripts/discover_raleys_products.py to build the vendored per-category catalogue.

Store lookup is vendored (`stores.json`, refreshed by scripts/discover_raleys_stores.py): the
stores sitemap names each store's number, banner, street, city and state in the URL slug, but
no ZIP or coordinates are published on any allowed surface, so the script geocodes each address
with the Census geocoder that already supplies this repo's ZCTA centroids.
"""

import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

from app.concurrency import fanout_limit, gather_bounded
from app.normalize.availability import OUT_OF_STOCK, STOCK_NOT_PUBLISHED, UNKNOWN, StockReporting
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url
from app.retailers.zipmatch import rank_stores_by_zip

log = logging.getLogger("storesplit.retailers.raleys")

SITE_URL = "https://www.raleys.com"
STORES_PATH = Path(__file__).with_name("stores.json")
CATALOGUE_PATH = Path(__file__).with_name("catalogue.json")
PRODUCT_SOURCE = "raleys:product-page"
MAX_PRODUCTS_PER_QUERY = 30
_CENT = Decimal("0.01")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# attributesRaw unitOfMeasure -> token the quantity parser understands
_UOM_TEXT = {
    "EA": "ct",
    "CT": "ct",
    "OZ": "oz",
    "FZ": "fl oz",
    "LB": "lb",
    "GA": "gal",
    "QT": "qt",
    "PT": "pt",
    "ML": "ml",
    "LT": "l",
    "GM": "g",
    "KG": "kg",
}


class RaleysAdapter:
    slug = "raleys"
    name = "Raley's"
    site_url = SITE_URL
    # commercetools with `inventoryMode: "None"` -- inventory is not tracked at all.
    stock_reporting: StockReporting = STOCK_NOT_PUBLISHED

    def __init__(self, clients: RetailerClients) -> None:
        # The store is selected by an explicit per-request Cookie header, which survives a
        # shared jar: http.cookiejar never injects a stored cookie over a header the caller
        # already set. So Raley's needs no session state of its own and shares the client.
        self._client = clients.shared()

    def is_configured(self) -> bool:
        return bool(store_directory()) and bool(catalogue())

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        return rank_stores_by_zip(list(store_directory()), zip_code)

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        # /search is robots-disallowed, so a category is the vendored list of product ids the
        # discovery script found in Raley's own category sitemaps. One page per product, and
        # those pages are independent: fetch them concurrently under the per-retailer bound.
        product_ids = catalogue().get(query, [])[:MAX_PRODUCTS_PER_QUERY]
        results = await gather_bounded(
            fanout_limit(),
            [partial(self.fetch_product, product_id, store) for product_id in product_ids],
        )
        return [listing for listing in results if listing is not None]

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{SITE_URL}/product/{retailer_sku}/-",
            headers={"Accept": "text/html", "Cookie": store_cookie(store.external_id)},
            max_retries=1,
        )
        if response.status_code != 200:
            return None
        return parse_product_page(response.text, store.external_id)

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)


def store_cookie(store_number: str) -> str:
    """The site's own store-selection cookie, URL-encoded exactly as the browser writes it."""
    return (
        "FLDR.User=shopType%3D%3BstoreId%3D"
        f"{store_number}"
        "%3BpostalCode%3D%3BsessionId%3D%3Bdevice%3Ddesktop%3B"
    )


@lru_cache
def store_directory() -> tuple[StoreLocation, ...]:
    if not STORES_PATH.exists():
        return ()
    records = json.loads(STORES_PATH.read_text())
    return tuple(
        StoreLocation(
            external_id=str(record["number"]),
            name=record["name"],
            address_line1=record.get("address_line1"),
            city=record.get("city"),
            state=record.get("state"),
            zip_code=record.get("zip_code"),
            latitude=record.get("latitude"),
            longitude=record.get("longitude"),
        )
        for record in records
    )


@lru_cache
def catalogue() -> dict[str, list[str]]:
    """search query -> product ids, from Raley's own category sitemaps."""
    if not CATALOGUE_PATH.exists():
        return {}
    return json.loads(CATALOGUE_PATH.read_text())


def parse_product_page(html: str, store_external_id: str) -> ProductListing | None:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        page_props = json.loads(match.group(1)).get("props", {}).get("pageProps", {})
    except ValueError:
        return None
    # `currentStoreNumber` is the site's own statement of which store it priced this page
    # for, and `price.channel.key` is the commercetools channel behind it. Either disagreeing
    # with the store the cookie asked for means the cookie did not take, and the price on the
    # page is another store's.
    current_store = str(page_props.get("currentStoreNumber") or "").strip()
    product = page_props.get("product") or {}
    current = (product.get("masterData") or {}).get("current") or {}
    variant = current.get("masterVariant") or {}
    title = str(current.get("name") or "").strip()
    sku = str(variant.get("sku") or product.get("key") or "").strip()
    price = variant.get("price") or {}
    fields = {
        field.get("name"): field.get("value")
        for field in (price.get("custom") or {}).get("customFieldsRaw") or []
    }
    attributes = {
        attribute.get("name"): attribute.get("value")
        for attribute in variant.get("attributesRaw") or []
    }
    regular = _cents(fields.get("regularPrice"))
    current_price = _cents((price.get("discounted") or {}).get("value")) or regular
    if not sku or not title or current_price is None:
        return None
    channel = str((price.get("channel") or {}).get("key") or "").strip()
    answered_for = current_store or channel
    if answered_for != store_external_id:
        # Refused when it names *another* store, and equally when it names none. A page that
        # states no store is what a store cookie that stopped taking looks like -- the site
        # then prices for its own default -- and accepting it would file those prices under
        # whichever store was asked about, which is the failure this check exists to catch.
        return None
    if channel and current_store and channel != current_store:
        return None
    by_weight = str((attributes.get("unitSellType") or {}).get("key") or "") == "byWeight"
    upc = str(attributes.get("primaryUPC") or "").strip() or None
    return ProductListing(
        retailer_sku=sku,
        title=title,
        store_external_id=store_external_id,
        price=current_price,
        regular_price=max(regular or current_price, current_price),
        loyalty_price=None,
        brand=str(attributes.get("brand") or "").strip() or None,
        product_url=clean_product_url(
            f"/product/{sku}/{current.get('slug') or sku}", base_url=SITE_URL
        ),
        image_url=_first_image(variant),
        gtin=upc,
        size_text=None if by_weight else _size_text(attributes),
        price_basis=PER_POUND if by_weight else PACKAGE,
        # commercetools `inventoryMode: "None"` means Raley's tracks no stock for this
        # product, so the only inventory fact the page states is discontinuation.
        # Anything else is unknown -- a sellable page is not a stocked shelf.
        availability=OUT_OF_STOCK if fields.get("discontinued") else UNKNOWN,
        stock_status=(
            "discontinued"
            if fields.get("discontinued")
            else f"inventoryMode={fields.get('inventoryMode') or 'unset'}"
        ),
        source=PRODUCT_SOURCE,
        store_context=answered_for or None,
        attributes={
            "store_channel": channel,
            "department": str((attributes.get("fulfillmentDepartment") or {}).get("label") or ""),
            "promotions": ",".join(attributes.get("promotionIndicators") or []),
            "weight_lb": str(attributes.get("weightInPounds") or ""),
        },
    )


def _size_text(attributes: dict[str, Any]) -> str | None:
    """ "12 ct" from unitsPerPackage + unitOfMeasure.

    A bare "1 ct" says nothing about the package, so it is dropped rather than guessed.
    """
    quantity = _decimal(attributes.get("unitsPerPackage"))
    unit = _UOM_TEXT.get(str(attributes.get("unitOfMeasure") or "").strip().upper())
    if quantity is None or quantity <= 0 or unit is None:
        return None
    if unit == "ct" and quantity == 1:
        return None
    return f"{quantity.normalize():f} {unit}"


def _first_image(variant: dict[str, Any]) -> str | None:
    """Raley's images are `[{"url": "//host/path", "label": ...}]`.

    The URL is protocol-relative; `clean_image_url` upgrades it to https. It used to be
    read with `str(image.get("url") or "")`, which turned a nested object into its repr.
    """
    return listing_image_url(variant.get("images"))


def _cents(money: Any) -> Decimal | None:
    if not isinstance(money, dict):
        return None
    amount = money.get("centAmount")
    digits = money.get("fractionDigits", 2)
    if amount is None:
        return None
    return (Decimal(amount) / (Decimal(10) ** int(digits))).quantize(_CENT, ROUND_HALF_UP)


def _decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None
