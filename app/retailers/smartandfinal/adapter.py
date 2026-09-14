"""Smart & Final adapter.

The site (www.smartandfinal.com) is a Mi9/Mercatus storefront whose public gateway answers
plain HTTPS JSON without cookies, tokens or login:
  * GET https://storefrontgateway.smartandfinal.com/api/stores
        all stores with address, ZIP and coordinates (no ZIP filter; ranked locally).
  * GET .../api/stores/<retailerStoreId>/search?q=<q>&take=<n>&skip=<n>
        store-specific search results (prices differ between stores); `take` is required,
        max 100.
  * GET .../api/stores/<retailerStoreId>/products/<sku>
        one product at one store (404 when not ranged there).
SKUs are 14-digit GTINs (produce uses zero-padded PLU codes). Loyalty pricing is not exposed
anonymously; temporary price reductions (TPR) are, as `wasPrice` + `priceSource: "tpr"`.
"""

import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.normalize.availability import (
    IN_STOCK,
    LIVE_STOCK,
    OUT_OF_STOCK,
    UNKNOWN,
    Availability,
    StockReporting,
    availability_from_flag,
)
from app.normalize.hours import DayHours, StoreHours, hours_from_weekly, parse_clock_12h
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import image_url_from
from app.retailers.urls import clean_image_url, clean_product_url
from app.retailers.zipmatch import rank_stores_by_zip

log = logging.getLogger("storesplit.retailers.smartandfinal")

BASE_URL = "https://storefrontgateway.smartandfinal.com/api"
SITE_URL = "https://www.smartandfinal.com"
SEARCH_SOURCE = "smartandfinal:api/search"
PRODUCT_SOURCE = "smartandfinal:api/products"
PAGE_SIZE = 100
_STORES_TTL_SECONDS = 6 * 3600
# Smart & Final's own three levels, and what each means *here*. Spelling this out rather
# than letting a generic reading fall back to the `available` flag is the point: "low" is a
# hedge word, and on the Instacart storefront the same idea is the shopper-facing "Likely
# out of stock". It is not that here. Smart & Final keeps a separate "out", and an item its
# search calls "low" comes back "high" from its own product endpoint a scrape later, so
# "low" is a quantity that is still on the shelf.
_STOCK_STATUS = {"plenty": "high", "low": "low", "out": "out"}
_STOCK_LEVELS: dict[str, Availability] = {
    "high": IN_STOCK,
    "low": IN_STOCK,
    "out": OUT_OF_STOCK,
}
_CENT = Decimal("0.01")
_MONEY_RE = re.compile(r"\$?\s*(\d+(?:\.\d+)?)")

# The directory, and the raw records it was read from. The records carry a store's own
# `timeZone` and `openingHours`, which `StoreLocation` has nowhere to keep and which the
# store-details capability would otherwise have to fetch a second time to see.
_store_cache: tuple[float, list[StoreLocation], dict[str, dict[str, Any]]] | None = None


class SmartAndFinalAdapter:
    site_url = SITE_URL
    slug = "smartandfinal"
    name = "Smart & Final"
    # `attributes["Stock Status"]`: plenty / low / out, mapped in this adapter.
    stock_reporting: StockReporting = LIVE_STOCK

    # Anonymous JSON gateway, no cookies or tokens: the shared application client.
    def __init__(self, clients: RetailerClients) -> None:
        self._client = clients.shared()

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        return rank_stores_by_zip(await self._all_stores(), zip_code)

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}/stores/{store.external_id}/search",
            params={"q": query, "take": PAGE_SIZE, "skip": 0},
        )
        response.raise_for_status()
        return parse_search_results(response.json(), store.external_id)

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}/stores/{store.external_id}/products/{retailer_sku}",
            max_retries=1,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return listing_from_item(response.json(), store.external_id, PRODUCT_SOURCE)

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """Timezone and opening hours, from the directory the scrape already downloaded.

        Smart & Final states both on every record of `/api/stores` -- an IANA `timeZone` and
        an `openingHours` sentence -- so this capability costs no request at all beyond the
        one `find_stores` made. The sentence is the only hours source here that is prose
        rather than structure, so it is parsed strictly and anything unrecognised yields
        nothing: a store whose wording changes shows "Hours not published", never a guess.
        """
        await self._all_stores()
        record = (_store_cache[2] if _store_cache else {}).get(store.external_id)
        if record is None:
            return None
        return store_details_from_record(record)

    async def _all_stores(self) -> list[StoreLocation]:
        global _store_cache
        now = time.monotonic()
        if _store_cache is not None and now - _store_cache[0] < _STORES_TTL_SECONDS:
            return _store_cache[1]
        response = await request_with_retry(self._client, "GET", f"{BASE_URL}/stores")
        response.raise_for_status()
        payload = response.json()
        stores = parse_stores(payload)
        records = {
            str(item.get("retailerStoreId")): item
            for item in payload.get("items", [])
            if item.get("retailerStoreId")
        }
        _store_cache = (now, stores, records)
        return stores


def parse_stores(payload: dict[str, Any]) -> list[StoreLocation]:
    stores: list[StoreLocation] = []
    for item in payload.get("items", []):
        store_id = item.get("retailerStoreId")
        if not store_id or item.get("status") != "Active":
            continue
        location = item.get("location") or {}
        zip_code = str(item.get("postCode") or "").strip()[:5]
        stores.append(
            StoreLocation(
                external_id=str(store_id),
                name=f"Smart & Final {_display_name(item.get('name') or '', str(store_id))}",
                address_line1=_title(item.get("addressLine1")),
                city=_title(item.get("city")),
                state=item.get("countyProvinceState") or None,
                zip_code=zip_code or None,
                latitude=location.get("latitude"),
                longitude=location.get("longitude"),
            )
        )
    return stores


def parse_search_results(payload: dict[str, Any], store_external_id: str) -> list[ProductListing]:
    listings: list[ProductListing] = []
    for item in payload.get("items", []):
        listing = listing_from_item(item, store_external_id, SEARCH_SOURCE)
        if listing is not None:
            listings.append(listing)
    return listings


def listing_from_item(
    item: dict[str, Any], store_external_id: str, source: str
) -> ProductListing | None:
    # Search items carry numeric fields (priceNumeric, wholePrice, unitOfSize); the product
    # detail endpoint carries the same data as display strings (price, unitPrice, unitsOfSize).
    sku = item.get("sku") or item.get("productId")
    name = item.get("name")
    if not sku or not name:
        return None
    size = item.get("unitOfSize") or item.get("unitsOfSize") or {}
    price_unit = item.get("unitOfPrice") or {}
    size_value = _decimal(size.get("size"))
    price = _decimal(item.get("priceNumeric"))
    if price is None:
        price = _money(item.get("price"))
    was = _decimal(item.get("wasPriceNumeric"))
    if was is None:
        was = _money(item.get("wasPrice"))
    whole = _decimal(item.get("wholePrice"))
    if whole is None:
        whole = _money(item.get("pricePerUnit") or item.get("unitPrice"))
    if price is None:
        return None

    priced_per_pound = (price_unit.get("abbreviation") or "").lower() == "lb"
    if priced_per_pound:
        # `price` is an estimated per-package amount (size x $/lb); the shelf price is $/lb.
        price_basis = PER_POUND
        size_text = None
        price = (whole if whole is not None else price).quantize(_CENT, ROUND_HALF_UP)
        regular = price
        if was is not None and size_value:
            regular = (was / size_value).quantize(_CENT, ROUND_HALF_UP)
    else:
        price_basis = PACKAGE
        size_text = _size_text(size_value, size.get("abbreviation") or size.get("label"))
        price = price.quantize(_CENT, ROUND_HALF_UP)
        regular = was.quantize(_CENT, ROUND_HALF_UP) if was is not None else price

    attributes = item.get("attributes") or {}
    stock_raw = attributes.get("Stock Status") or attributes.get("StockStatus")
    stock_status = _STOCK_STATUS.get(str(stock_raw).lower()) if stock_raw else None
    levelled = _STOCK_LEVELS.get(stock_status or "", UNKNOWN)
    flagged = availability_from_flag(item.get("available"))
    # The flag denies outright; otherwise this retailer's own level decides, and only an
    # unmapped level leaves the flag to answer alone.
    if flagged == OUT_OF_STOCK:
        availability = OUT_OF_STOCK
    elif levelled != UNKNOWN:
        availability = levelled
    else:
        availability = flagged
    if stock_raw and stock_status is None:
        stock_status = str(stock_raw)  # an unmapped word is kept verbatim, and stays unknown
    gtin = str(sku) if str(sku).isdigit() else None
    image = _image_url(item)
    listing_attributes = {
        "price_source": str(item.get("priceSource") or ""),
        "price_label": str(item.get("priceLabel") or ""),
        "price_per_unit": str(item.get("pricePerUnit") or item.get("unitPrice") or ""),
        "has_loyalty_discount": str(bool(item.get("hasLoyaltyDiscount"))).lower(),
    }
    return ProductListing(
        retailer_sku=str(sku),
        title=name,
        store_external_id=store_external_id,
        price=max(price, Decimal("0")),
        regular_price=max(regular, price),
        loyalty_price=None,
        brand=(item.get("brand") or "").strip() or None,
        product_url=clean_product_url(
            f"/sm/pickup/rsid/{store_external_id}/product/{sku}", base_url=SITE_URL
        ),
        image_url=image,
        gtin=gtin,
        size_text=size_text,
        price_basis=price_basis,
        availability=availability,
        stock_status=stock_status,
        source=source,
        attributes=listing_attributes,
    )


def _size_text(value: Decimal | None, unit: str | None) -> str | None:
    if value is None or value <= 0:
        return None
    unit_text = (unit or "").strip().lower()
    if unit_text in {"", "each"}:
        unit_text = "ct"
    return f"{value.normalize():f} {unit_text}"


def _money(text: Any) -> Decimal | None:
    match = _MONEY_RE.search(str(text)) if text else None
    return Decimal(match.group(1)) if match else None


def _decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None


def _display_name(name: str, store_id: str) -> str:
    # Gateway names look like "351 - San Francisco - Seventh Ave".
    parts = [p.strip() for p in name.split(" - ")]
    if parts and parts[0] == store_id:
        parts = parts[1:]
    return " - ".join(parts) or f"#{store_id}"


def _title(raw: str | None) -> str | None:
    return raw.strip().title() if raw and raw.strip() else None


# The CDN serves `cell` (thumbnail), `detail` and `zoom`. Anything else 404s.
_TEMPLATE_SIZE = "cell"


def _image_url(item: dict[str, Any]) -> str | None:
    """Smart & Final spells an image as `{cell, default, details, template, zoom}`, under
    `image` on a search hit and `primaryImage` on a product.

    Some items carry only `default`. This used to read
    `(item.get("image") or item.get("primaryImage") or {}).get("template")`, which falls
    back on the *container*: `item["image"]` was truthy, so `primaryImage` was never
    consulted and a missing `template` silently produced no image at all.

    The concrete keys are preferred over `template` because `template` is the one value that
    can be got wrong: it carries a literal `{size}` and the CDN serves only `cell`, `detail`
    and `zoom` (verified live -- `medium`, which this used to substitute, 404s for every
    product). `cell` is the thumbnail, which is what a result card wants.
    """
    for container in (item.get("image"), item.get("primaryImage")):
        if not isinstance(container, dict):
            continue
        for key in ("cell", "default", "details", "zoom", "template"):
            raw = image_url_from(container.get(key))
            if raw is not None:
                return clean_image_url(raw.replace("{size}", _TEMPLATE_SIZE))
    return None


# ------------------------------------------------------------------ hours from the directory

STORE_DIRECTORY_SOURCE = "smartandfinal:api/stores"
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_INDEX = {name: index for index, name in enumerate(_DAYS)}
_DAY_INDEX.update({name[:3]: index for index, name in enumerate(_DAYS)})
# "Sunday-Saturday: 6 AM - 10 PM" and "Mon-Fri: 7 AM - 9 PM, Sat: 8 AM - 8 PM". Deliberately
# narrow: a sentence this does not match yields no hours at all.
_HOURS_CLAUSE = re.compile(
    r"(?P<days>[A-Za-z]{3,9}(?:\s*[-\u2013]\s*[A-Za-z]{3,9})?)\s*:\s*"
    r"(?:"
    r"(?P<open>\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?)\s*[-\u2013]\s*"
    r"(?P<close>\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?)"
    r"|(?P<closed>[Cc]losed)"
    r")"
)


def store_details_from_record(item: dict[str, Any]) -> StoreDetails | None:
    """One `/api/stores` record as published store details."""
    store_id = str(item.get("retailerStoreId") or "").strip()
    if not store_id:
        return None
    location = item.get("location") or {}
    timezone = str(item.get("timeZone") or "").strip() or None
    return StoreDetails(
        external_id=store_id,
        name=None,  # the directory's name is what `find_stores` already wrote
        address_line1=_title(item.get("addressLine1")),
        city=_title(item.get("city")),
        state=item.get("countyProvinceState") or None,
        zip_code=str(item.get("postCode") or "").strip()[:5] or None,
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        hours=parse_opening_hours(item.get("openingHours"), timezone),
        source=STORE_DIRECTORY_SOURCE,
    )


def parse_opening_hours(text: Any, timezone: str | None) -> StoreHours | None:
    """ "Sunday-Saturday: 6 AM - 10 PM" -> a weekly pattern, or None.

    The one hours source in this repo that a retailer writes as a sentence. Every clause has
    to parse and every clause has to name days that are understood; a sentence with any
    unreadable part produces nothing, because half a week of opening times presented as a
    full one is worse than admitting the hours are unknown. Across the 253 stores the
    directory lists there are four distinct sentences, all of this shape.
    """
    sentence = str(text or "").strip()
    if not sentence or not timezone:
        return None
    clauses = list(_HOURS_CLAUSE.finditer(sentence))
    if not clauses:
        return None
    weekly: dict[int, DayHours] = {}
    for clause in clauses:
        days = _weekday_span(clause.group("days"))
        if days is None:
            return None
        if clause.group("closed"):
            window = DayHours(None, None)
        else:
            opens = parse_clock_12h(clause.group("open") or "")
            closes = parse_clock_12h(clause.group("close") or "")
            if opens is None or closes is None:
                return None
            window = DayHours(opens, closes)
        for day in days:
            weekly[day] = window
    # **All seven days or none.** A sentence that parses in part is the dangerous outcome:
    # the days it missed are not "unknown" to a reader, they are simply absent, and an absent
    # weekday reads as "Closed - opens tomorrow" once the dated exceptions age out. Half a
    # week presented as a whole one is worse than admitting the hours are unknown.
    if set(weekly) != set(range(7)):
        return None
    return hours_from_weekly(weekly, {}, timezone)


def _weekday_span(raw: str) -> list[int] | None:
    """ "Sunday-Saturday" -> every weekday; "Sat" -> just Saturday; anything else -> None."""
    parts = [p.strip().lower() for p in re.split(r"[-\u2013]", raw) if p.strip()]
    indexes = [_DAY_INDEX.get(p if p in _DAY_INDEX else p[:3]) for p in parts]
    if not indexes or any(i is None for i in indexes):
        return None
    if len(indexes) == 1:
        return [indexes[0]]  # type: ignore[list-item]
    start, end = indexes[0], indexes[-1]
    assert start is not None and end is not None
    # A span may wrap the week ("Sunday-Saturday" starts on the last weekday index).
    span = [(start + step) % 7 for step in range((end - start) % 7 + 1)]
    return span
