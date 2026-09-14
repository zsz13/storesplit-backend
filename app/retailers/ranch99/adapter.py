"""99 Ranch Market adapter.

The site's own backend-for-frontend answers plain HTTPS JSON without cookies or tokens; the
only requirement is the `storeid` header the web app sends (store number, or 8899 for the
site default before a store is chosen):
  * POST https://www.99ranch.com/be-api/store/web/nearby/stores  {"zipCode": ..., ...}
        stores near a ZIP with address, coordinates and distance, nearest first.
  * POST https://www.99ranch.com/be-api/search/web/products      {"keyword": ..., "page": ...}
        store-specific search results (header storeid = store number) with regular/sale
        prices, UPC, net weight + unit and stock quantity (out-of-stock items included).
  * GET  https://www.99ranch.com/product-details/<productId>/<storeNumber>/<upc>
        product page whose __NEXT_DATA__ carries the same variant record; used for
        fetch_product, which therefore needs the UPC seen in an earlier search.
Prices are the store's online-order prices. No loyalty programme pricing is exposed.
"""

import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.normalize.availability import IN_STOCK, LIVE_STOCK, OUT_OF_STOCK, UNKNOWN, StockReporting
from app.normalize.hours import (
    DayHours,
    StoreHours,
    hours_from_weekly,
    parse_clock_24h,
    parse_day_range,
)
from app.normalize.phone import e164_us
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url

log = logging.getLogger("storesplit.retailers.ranch99")

SITE_URL = "https://www.99ranch.com"
BASE_URL = f"{SITE_URL}/be-api"
DEFAULT_STORE_ID = "8899"  # the web app's placeholder store before one is chosen
SEARCH_SOURCE = "ranch99:be-api/search"
PRODUCT_SOURCE = "ranch99:product-page"
DETAILS_SOURCE = "ranch99:be-api/nearby-stores"
PAGE_SIZE = 28
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_CENT = Decimal("0.01")
# netWeightUom -> token understood by the quantity parser
_UOM_TEXT = {
    "OZ": "oz",
    "FOZ": "fl oz",
    "FL OZ": "fl oz",
    "LB": "lb",
    "LBS": "lb",
    "GAL": "gal",
    "QT": "qt",
    "PT": "pt",
    "ML": "ml",
    "L": "l",
    "G": "g",
    "KG": "kg",
    "PC": "ct",
    "PCS": "ct",
    "EA": "ct",
    "EACH": "ct",
    "CT": "ct",
}


class Ranch99Adapter:
    site_url = SITE_URL
    slug = "ranch99"
    name = "99 Ranch Market"
    # `available` is a numeric per-store quantity, so zero is a real out-of-stock.
    stock_reporting: StockReporting = LIVE_STOCK

    # The site's BFF needs no cookies or tokens; its two constant headers ride on each
    # request, so 99 Ranch uses the shared application client.
    def __init__(self, clients: RetailerClients) -> None:
        self._client = clients.shared()
        self._upc_by_sku: dict[str, str] = {}
        # The store records `find_stores` already read, kept for the run so that hours
        # cost no request of their own: everything `fetch_store_details` needs -- the
        # zone, the shop's own week -- is in the payload the ZIP lookup returned.
        self._records: dict[str, dict[str, Any]] = {}

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        payload = await self._post(
            "/store/web/nearby/stores",
            DEFAULT_STORE_ID,
            {
                "zipCode": zip_code[:5],
                "pageSize": 12,
                "pageNum": 1,
                "type": 1,
                "source": "WEB",
                "within": None,
            },
        )
        self._records.update(records_by_store(payload))
        return parse_stores(payload)

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """Hours, timezone and phone, from the record the ZIP lookup already returned.

        99 Ranch states both an IANA `timeZone` and the shop's own week on every store in
        the nearby-stores payload, so an ordinary scrape pays nothing for hours: the record
        is the one `find_stores` read minutes earlier. Called on its own -- by a probe, or
        for a store discovered under a different ZIP -- it re-reads the ZIP the store sits
        in, which is the only question this endpoint answers.
        """
        record = self._records.get(store.external_id)
        if record is None and store.zip_code:
            payload = await self._post(
                "/store/web/nearby/stores",
                DEFAULT_STORE_ID,
                {
                    "zipCode": store.zip_code[:5],
                    "pageSize": 12,
                    "pageNum": 1,
                    "type": 1,
                    "source": "WEB",
                    "within": None,
                },
            )
            self._records.update(records_by_store(payload))
            record = self._records.get(store.external_id)
        if record is None:
            return None
        return store_details_from_record(record, store.external_id)

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        payload = await self._post(
            "/search/web/products",
            store.external_id,
            {"page": 1, "pageSize": PAGE_SIZE, "keyword": query},
        )
        listings = parse_search_results(payload, store.external_id)
        for listing in listings:
            if listing.gtin:
                self._upc_by_sku[listing.retailer_sku] = listing.gtin
        return listings

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        # The product page URL needs the UPC as well as the product id; the search API has no
        # lookup by id, so only products seen in a search during this adapter's lifetime resolve.
        upc = self._upc_by_sku.get(retailer_sku)
        if upc is None:
            return None
        response = await request_with_retry(
            self._client,
            "GET",
            f"{SITE_URL}/product-details/{retailer_sku}/{store.external_id}/{upc}",
            headers={"Accept": "text/html", "lang": "en_US"},
            max_retries=1,
        )
        if response.status_code != 200:
            return None
        return parse_product_page(response.text, store.external_id)

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def _post(self, path: str, store_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await request_with_retry(
            self._client,
            "POST",
            BASE_URL + path,
            json=body,
            headers={"storeid": store_id, "lang": "en_US"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, "0", None) or payload.get("success") is False:
            raise RuntimeError(
                f"99 Ranch API error {payload.get('code')}: {payload.get('message')}"
            )
        return payload


def parse_stores(payload: dict[str, Any]) -> list[StoreLocation]:
    records = (payload.get("data") or {}).get("records") or []
    stores: list[StoreLocation] = []
    for record in records:
        number = record.get("storeNumber")
        if number is None:
            continue
        stores.append(
            StoreLocation(
                external_id=str(number),
                name=f"99 Ranch Market {record.get('name') or number}",
                address_line1=record.get("street") or None,
                city=record.get("city") or None,
                state=record.get("state") or None,
                zip_code=str(record.get("zipCode") or "")[:5] or None,
                latitude=_float(record.get("latitude")),
                longitude=_float(record.get("longitude")),
            )
        )
    return stores


def records_by_store(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The nearby-stores payload keyed the way a `StoreLocation` is, for later re-reading."""
    records = (payload.get("data") or {}).get("records") or []
    found: dict[str, dict[str, Any]] = {}
    for record in records:
        number = record.get("storeNumber")
        if isinstance(record, dict) and number is not None:
            found[str(number)] = record
    return found


def store_details_from_record(record: dict[str, Any], external_id: str) -> StoreDetails | None:
    """One nearby-stores record as published store details."""
    if not record.get("storeNumber"):
        return None
    timezone = str(record.get("timeZone") or "").strip() or None
    return StoreDetails(
        external_id=external_id,
        name=f"99 Ranch Market {record.get('name') or record.get('storeNumber')}",
        address_line1=record.get("street") or None,
        city=record.get("city") or None,
        state=record.get("state") or None,
        zip_code=str(record.get("zipCode") or "")[:5] or None,
        latitude=_float(record.get("latitude")),
        longitude=_float(record.get("longitude")),
        phone=e164_us(record.get("telephone")),
        hours=parse_business_hours(record.get("offlineBusinessTimes"), timezone),
        source=DETAILS_SOURCE,
    )


def parse_business_hours(entries: Any, timezone: str | None) -> StoreHours | None:
    """`[{dayOfWeeks, startTime, endTime}]` as a weekly schedule, in the zone the record states.

    **`offlineBusinessTimes`, never `onlineBusinessTimes`.** The two differ per store -- the
    Richmond shop delivers 08:00-22:00 all week and opens its doors an hour earlier on
    Monday to Thursday than it closes them on Friday to Sunday -- and a shopper standing
    outside a door is asking about the door. Passing the delivery window would state a shop
    open at an hour it is shut, which is the one answer worth less than "hours unknown".

    Days are stated as inclusive ranges rather than as seven entries, so each range is
    expanded (`parse_day_range`) and each day takes the window of the entry naming it. An
    entry whose range or clock cannot be read contributes nothing rather than a guess; a
    later entry naming the same day wins, which is how a record that restates a day resolves
    to the last thing the retailer said about it.
    """
    weekly: dict[int, DayHours] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        opens = parse_clock_24h(entry.get("startTime"))
        closes = parse_clock_24h(entry.get("endTime"))
        if opens is None or closes is None:
            log.info("ranch99_unread_hours", extra={"entry": str(entry)[:200]})
            continue
        for weekday in parse_day_range(entry.get("dayOfWeeks")):
            weekly[weekday] = DayHours(opens, closes)
    return hours_from_weekly(weekly, {}, timezone)


def parse_search_results(payload: dict[str, Any], store_external_id: str) -> list[ProductListing]:
    items = (payload.get("data") or {}).get("list") or []
    listings: list[ProductListing] = []
    for item in items:
        listing = _listing_from_item(item, store_external_id)
        if listing is not None:
            listings.append(listing)
    return listings


def parse_product_page(html: str, store_external_id: str) -> ProductListing | None:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    props = json.loads(match.group(1)).get("props", {}).get("pageProps", {})
    product = (props.get("productDataRes") or {}).get("data") or {}
    variants = product.get("variants") or []
    if not variants:
        return None
    item = dict(variants[0])
    if not item.get("brandName"):
        item["brandName"] = (product.get("brand") or {}).get("name")
    return _listing_from_item(item, store_external_id, PRODUCT_SOURCE)


def _listing_from_item(
    item: dict[str, Any], store_external_id: str, source: str = SEARCH_SOURCE
) -> ProductListing | None:
    product_id = item.get("productId")
    title = (item.get("productName") or item.get("productNameEN") or "").strip()
    price = _decimal(item.get("price"))
    if product_id is None or not title or price is None:
        return None
    retail = _decimal(item.get("retailPrice")) or price
    sale = _decimal(item.get("salePrice"))
    current = sale if sale is not None and Decimal(0) < sale < price else price
    regular = max(retail, price, current)
    uom = str(item.get("saleUom") or "").strip().upper()
    weight_unit = str(item.get("netWeightUom") or "").strip().upper()
    net_weight = _decimal(item.get("netWeight"))
    if uom in {"LB", "LBS", "POUND"}:
        price_basis = PER_POUND
        size_text = None
    else:
        price_basis = PACKAGE
        size_text = _size_text(net_weight, weight_unit)
    # 99 Ranch reports a per-store quantity, and the retailer's own product tile reads it as
    # `E = (0 === t.available)` -> a "Sold Out / In stock soon" mask. So the states mirror
    # that rule exactly: a stated zero is the retailer's own out-of-stock, a positive count
    # is stock, and anything else -- a missing field, a non-number, or a negative the tile
    # would still render as buyable -- is no answer at all rather than a negative StoreSplit
    # invented. (The product page is stock-blind for every product, so it can never
    # contradict this; see `tests/test_ranch99_availability.py`.)
    available = item.get("available")
    if isinstance(available, int | float) and not isinstance(available, bool):
        stock_status = f"available={available:g}"
        state = IN_STOCK if available > 0 else OUT_OF_STOCK if available == 0 else UNKNOWN
    else:
        state, stock_status = UNKNOWN, None
    upc = str(item.get("upcId") or "").strip() or None
    return ProductListing(
        retailer_sku=str(product_id),
        title=title,
        store_external_id=store_external_id,
        price=current.quantize(_CENT, ROUND_HALF_UP),
        regular_price=regular.quantize(_CENT, ROUND_HALF_UP),
        loyalty_price=None,
        brand=(item.get("brandName") or "").strip() or None,
        product_url=clean_product_url(
            f"/product-details/{product_id}/{store_external_id}" + (f"/{upc}" if upc else ""),
            base_url=SITE_URL,
        ),
        image_url=_image_url(item),
        gtin=upc,
        size_text=size_text,
        price_basis=price_basis,
        availability=state,
        stock_status=stock_status,
        source=source,
        attributes={
            "variant_name": str(item.get("variantName") or ""),
            "available_qty": str(available if available is not None else ""),
            "coupon": str(item.get("coupon") if item.get("coupon") is not None else ""),
        },
    )


def _image_url(item: dict[str, Any]) -> str | None:
    """99 Ranch spells an image either as a URL string or as {"type": 0, "path": <url>}.

    The search payload carries `image` as a string but `productImage` as that object, and a
    product without `image` used to hand the object straight to a String column. The
    unwrapping this adapter proved now lives in `retailers/images.py` for every retailer.
    """
    return listing_image_url(item.get("image"), item.get("productImage"))


def _size_text(value: Decimal | None, uom: str) -> str | None:
    if value is None or value <= 0 or not uom:
        return None
    unit = _UOM_TEXT.get(uom)
    if unit is None:
        return None
    return f"{value.normalize():f} {unit}"


def _decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None


def _float(raw: Any) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None
