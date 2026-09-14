"""Trader Joe's adapter.

Data sources (plain HTTPS, no cookies or tokens; www.traderjoes.com sits behind Akamai, which
accepts the shared client's desktop browser User-Agent and rejects library defaults):
  * POST https://www.traderjoes.com/api/graphql
        the site's own Magento-style GraphQL. `SearchProducts` (keyword search filtered by
        store_code) and `SearchProduct` (one SKU). storeCode is the store number shown in the
        locator ("100"); "TJ" is the national catalogue.
  * POST https://hosted.where2getit.com/traderjoes/rest/locatorsearch
        the store locator embedded on traderjoes.com (public app key from its locator page);
        returns stores within a radius of a ZIP, nearest first, with coordinates.
Trader Joe's has no loyalty programme, no sale/regular price split and no UPCs; prices are
national in practice, store_code changes the assortment. Everything is private label.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.normalize.availability import STOCK_NOT_PUBLISHED, UNKNOWN, StockReporting
from app.normalize.hours import (
    WEEKDAY_INDEX,
    DayHours,
    UnzonedHours,
    parse_clock_24h,
)
from app.normalize.phone import e164_us
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import image_url_from
from app.retailers.urls import clean_image_url, clean_product_url

log = logging.getLogger("storesplit.retailers.traderjoes")

SITE_URL = "https://www.traderjoes.com"
GRAPHQL_URL = f"{SITE_URL}/api/graphql"
LOCATOR_URL = "https://hosted.where2getit.com/traderjoes/rest/locatorsearch"
LOCATOR_APP_KEY = "8559C922-54E3-11E7-8321-40B4F48ECC77"  # public constant from the locator page
SEARCH_SOURCE = "traderjoes:graphql/SearchProducts"
PRODUCT_SOURCE = "traderjoes:graphql/SearchProduct"
DETAILS_SOURCE = "traderjoes:locator"
BRAND = "Trader Joe's"
PAGE_SIZE = 50
_CENT = Decimal("0.01")

_ITEM_FIELDS = """
  sku item_title name sales_size sales_uom_description sales_uom_code retail_price
  availability promotion product_label new_product primary_image
  price_range { minimum_price { final_price { currency value } } }
  category_hierarchy { name }
"""
# `availability: {match: "1"}` stays in both queries even though the field says nothing
# about stock (see `_listing_from_item`): it is how this catalogue spells "published", every
# product carries it, and asking for "0" matches nothing. Dropping it would change which
# rows come back, not what they mean.
SEARCH_QUERY = f"""
query SearchProducts($search: String, $pageSize: Int, $currentPage: Int,
                     $storeCode: String = "TJ", $availability: String = "1",
                     $published: String = "1") {{
  products(search: $search
           filter: {{ store_code: {{ eq: $storeCode }}, published: {{ eq: $published }},
                     availability: {{ match: $availability }} }}
           pageSize: $pageSize currentPage: $currentPage) {{
    items {{ {_ITEM_FIELDS} }}
    total_count
    page_info {{ current_page page_size total_pages }}
  }}
}}
"""
PRODUCT_QUERY = f"""
query SearchProduct($sku: String, $storeCode: String = "TJ", $published: String = "1") {{
  products(filter: {{ sku: {{ eq: $sku }}, store_code: {{ eq: $storeCode }},
                     published: {{ eq: $published }} }}) {{
    items {{ {_ITEM_FIELDS} }}
    total_count
  }}
}}
"""
# Unit-of-measure descriptions -> tokens the quantity parser understands.
_UOM_TEXT = {"doz": "dozen", "each": "ct", "ea": "ct"}


class TraderJoesAdapter:
    site_url = SITE_URL
    slug = "traderjoes"
    name = "Trader Joe's"
    # Trader Joe's sells nothing online and states nothing about its shelves: its
    # `availability` field is `"1"` for all 344 catalogue items, which is carriage.
    stock_reporting: StockReporting = STOCK_NOT_PUBLISHED

    # Every request is a JSON POST (httpx sets Content-Type) with no session state, so the
    # shared application client serves Trader Joe's.
    def __init__(self, clients: RetailerClients) -> None:
        self._client = clients.shared()
        # The locator entries `find_stores` read, kept for the run: a store's week is in
        # the same record as its address, so hours cost no request of their own.
        self._entries: dict[str, dict[str, Any]] = {}

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        response = await request_with_retry(
            self._client, "POST", LOCATOR_URL, json=locator_request(zip_code[:5])
        )
        response.raise_for_status()
        payload = response.json()
        self._entries.update(entries_by_store(payload))
        return parse_locator_results(payload)

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """The store's week, from the locator entry that named the store in the first place.

        Trader Joe's publishes `monday_open` .. `sunday_close` for every store and **no
        timezone on any surface** -- not in the locator entry, not on the store page, not in
        the GraphQL API. So this returns `unzoned_hours` rather than `hours`: the adapter
        states what the retailer states and nothing more, and the week becomes a schedule
        only once a zone is established for the store elsewhere.

        That zone is no longer bought. The same locator entry carries the store's
        coordinates, and a point lies in exactly one timezone, so `normalize/timezones.py`
        reads it offline and `scraper.py::_resolved_hours` pairs the two -- with Trader Joe's
        still recorded as the source, because the clock is entirely its own and only the
        frame it is read in came from anywhere else. A store the locator places nowhere still
        says "Hours not published", which remains the honest answer for it.
        """
        entry = self._entries.get(store.external_id)
        if entry is None and store.zip_code:
            response = await request_with_retry(
                self._client, "POST", LOCATOR_URL, json=locator_request(store.zip_code[:5])
            )
            response.raise_for_status()
            self._entries.update(entries_by_store(response.json()))
            entry = self._entries.get(store.external_id)
        if entry is None:
            return None
        return StoreDetails(
            external_id=store.external_id,
            phone=e164_us(entry.get("phone")),
            unzoned_hours=parse_locator_hours(entry),
            source=DETAILS_SOURCE,
        )

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        payload = await self._graphql(
            "SearchProducts",
            SEARCH_QUERY,
            {
                "storeCode": store.external_id,
                "availability": "1",
                "published": "1",
                "search": query,
                "currentPage": 1,
                "pageSize": PAGE_SIZE,
            },
        )
        return parse_products(payload, store.external_id, SEARCH_SOURCE)

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        payload = await self._graphql(
            "SearchProduct",
            PRODUCT_QUERY,
            {"sku": retailer_sku, "storeCode": store.external_id, "published": "1"},
        )
        listings = parse_products(payload, store.external_id, PRODUCT_SOURCE)
        return listings[0] if listings else None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def _graphql(
        self, operation: str, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        response = await request_with_retry(
            self._client,
            "POST",
            GRAPHQL_URL,
            json={"operationName": operation, "query": query, "variables": variables},
        )
        if response.status_code == 403:
            # Akamai turns this request away by TLS fingerprint, not by anything in it: the
            # same code, the same headers and the same egress IP are served from a macOS host
            # and refused from inside the Docker image, whose Python links a different
            # OpenSSL. Matching the host's fingerprint would be spoofing it, which this repo
            # does not do -- so the honest answer is to say where the scrape does work rather
            # than let Trader Joe's quietly vanish from a containerised run.
            raise RuntimeError(
                "Trader Joe's refused this client (403). It serves the host's Python but not "
                "the container's TLS fingerprint; run the scrape from the host "
                "(uv run python scripts/scrape.py --zip <zip> --retailers traderjoes) "
                "to refresh its prices."
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"Trader Joe's GraphQL error: {payload['errors'][0]}")
        return payload


def locator_request(zip_code: str, limit: int = 10, radius_miles: int = 50) -> dict[str, Any]:
    return {
        "request": {
            "appkey": LOCATOR_APP_KEY,
            "formdata": {
                "geoip": False,
                "dataview": "store_default",
                "limit": limit,
                "geolocs": {
                    "geoloc": [
                        {"addressline": zip_code, "country": "US", "latitude": "", "longitude": ""}
                    ]
                },
                "searchradius": str(radius_miles),
                "where": {"warehouse": {"distinctfrom": "1"}},
            },
        }
    }


def parse_locator_results(payload: dict[str, Any]) -> list[StoreLocation]:
    collection = (payload.get("response") or {}).get("collection") or []
    stores: list[StoreLocation] = []
    for entry in collection:
        store_id = entry.get("clientkey")
        if not store_id or entry.get("Coming Soon") == "Yes":
            continue
        stores.append(
            StoreLocation(
                external_id=str(store_id),
                name=f"Trader Joe's {entry.get('name') or store_id}",
                address_line1=entry.get("address1") or None,
                city=entry.get("city") or None,
                state=entry.get("state") or None,
                zip_code=(entry.get("postalcode") or "")[:5] or None,
                latitude=_float(entry.get("latitude")),
                longitude=_float(entry.get("longitude")),
            )
        )
    return stores


def entries_by_store(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The locator collection keyed the way a `StoreLocation` is, for later re-reading."""
    collection = (payload.get("response") or {}).get("collection") or []
    found: dict[str, dict[str, Any]] = {}
    for entry in collection:
        key = entry.get("clientkey") if isinstance(entry, dict) else None
        if key:
            found[str(key)] = entry
    return found


def parse_locator_hours(entry: dict[str, Any]) -> UnzonedHours | None:
    """`<weekday>_open` / `<weekday>_close` as a week, with no zone attached to it.

    The machine-readable pair is read (`monday_open` "09:00"), not the human one
    (`monopen` "9AM"): they carry the same clock and only one of them is unambiguous.

    `holidayhours`, `Temp Hours Note` and `Alcohol Hours` are free text a person wrote for
    other people ("Closing at 5pm on Christmas Eve") and are deliberately not parsed. Half a
    week read wrongly is worse than a week nobody claimed, and a dated exception guessed at
    is exactly the sentence that sends a shopper to a shut door.
    """
    weekly: dict[int, DayHours] = {}
    for name, weekday in WEEKDAY_INDEX.items():
        opens = parse_clock_24h(entry.get(f"{name}_open"))
        closes = parse_clock_24h(entry.get(f"{name}_close"))
        if opens is not None and closes is not None:
            weekly[weekday] = DayHours(opens, closes)
    return UnzonedHours(weekly=weekly) if weekly else None


def parse_products(
    payload: dict[str, Any], store_external_id: str, source: str
) -> list[ProductListing]:
    items = ((payload.get("data") or {}).get("products") or {}).get("items") or []
    listings: list[ProductListing] = []
    for item in items:
        listing = _listing_from_item(item, store_external_id, source)
        if listing is not None:
            listings.append(listing)
    return listings


def _listing_from_item(
    item: dict[str, Any], store_external_id: str, source: str
) -> ProductListing | None:
    sku = item.get("sku")
    title = item.get("item_title") or item.get("name")
    money = (((item.get("price_range") or {}).get("minimum_price") or {}).get("final_price")) or {}
    value = money.get("value")
    if value is None:
        value = item.get("retail_price")
    if not sku or not title or value in (None, ""):
        return None
    price = Decimal(str(value)).quantize(_CENT, ROUND_HALF_UP)
    if price <= 0:
        return None  # "0.00" placeholders in the national catalogue
    uom = str(item.get("sales_uom_description") or "").strip().lower()
    size = item.get("sales_size")
    if uom == "lb" and size in (1, 1.0, "1"):
        price_basis = PER_POUND
        size_text = None
    else:
        price_basis = PACKAGE
        size_text = _size_text(size, uom)
    availability = str(item.get("availability") or "")
    categories = [c.get("name") for c in item.get("category_hierarchy") or [] if c.get("name")]
    image = image_url_from(item.get("primary_image")) or ""
    return ProductListing(
        retailer_sku=str(sku),
        title=title,
        store_external_id=store_external_id,
        price=price,
        regular_price=price,
        loyalty_price=None,
        brand=BRAND,
        product_url=clean_product_url(f"/home/products/pdp/{sku}", base_url=SITE_URL),
        # A single leading slash is a path on the Trader Joe's site; a double one is
        # protocol-relative and already names its own host, which `clean_image_url` upgrades
        # to https. Joining that to SITE_URL would build `https://www.traderjoes.com//cdn/…`,
        # which validates and 404s.
        image_url=clean_image_url(
            f"{SITE_URL}{image}" if image.startswith("/") and not image.startswith("//") else image
        ),
        gtin=None,
        size_text=size_text,
        price_basis=price_basis,
        # `availability` is "1" on every product in the catalogue -- 344 of 344 with no
        # filter applied, and `availability: {match: "0"}` matches nothing -- so it says
        # the item is published, not that a store has it. A field that cannot vary is not
        # a signal, exactly like Whole Foods' `isAvailable`. Trader Joe's publishes no
        # per-store inventory anywhere (its site sells nothing), so an offer here is
        # `unknown`: real, priced, and of unproven stock.
        availability=UNKNOWN,
        stock_status=f"availability={availability}" if availability else None,
        currency=str(money.get("currency") or "USD"),
        source=source,
        attributes={
            "raw_name": str(item.get("name") or ""),
            "category": " > ".join(categories),
            "new_product": str(item.get("new_product") or "0"),
        },
    )


def _size_text(size: Any, uom: str) -> str | None:
    if size in (None, "") or not uom:
        return None
    try:
        value = Decimal(str(size))
    except ArithmeticError:
        return None
    if value <= 0:
        return None
    return f"{value.normalize():f} {_UOM_TEXT.get(uom, uom)}"


def _float(raw: Any) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None
