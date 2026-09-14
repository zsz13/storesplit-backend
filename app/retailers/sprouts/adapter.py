"""Sprouts Farmers Market adapter.

shop.sprouts.com is Sprouts' own storefront domain (run on Instacart's platform). Everything
below is plain HTTPS with the guest session cookie the storefront hands to every visitor:
  * GET  https://shop.sprouts.com/store/sprouts/storefront
        sets the `__Host-instacart_sid` guest cookie (no account, no login).
  * POST https://shop.sprouts.com/idp/v1/init  then
    GET  https://shop.sprouts.com/idp/v1/shops?postal_code=<zip>
        the store locator behind sprouts.com/stores: shops (store + fulfilment mode) near a
        ZIP, nearest first, with address and Sprouts store number.
  * GET  https://shop.sprouts.com/graphql?operationName=SearchResultsPlacements&...
    GET  https://shop.sprouts.com/graphql?operationName=Items&...
        the storefront's persisted GraphQL queries: keyword search returns item ids for a
        shop; Items returns price, regular price, size, UPC and stock for those ids.
Persisted-query hashes are public constants in the storefront's JS bundle and change when
Instacart deploys; `OPERATION_HASHES` holds the current ones and a stale hash fails loudly.
Prices observed were identical for in-store, pickup and delivery shops of one store, so the
in-store or pickup shop of each store is used. No loyalty pricing is exposed anonymously.

Store hours come from somewhere else entirely -- `www.sprouts.com`, Sprouts' own WordPress
site, which is a different host with its own permissive robots.txt:
  * GET https://www.sprouts.com/wp-json/spr-wp-rest/v1/store/<store number>
        the store record behind sprouts.com/store/...: address, phone, coordinates, an IANA
        `timezone`, and one `open_time`/`close_time` window. No cookie, no session, no key.
The store number is the one Instacart already publishes as each shop's `location_code`, so
nothing is guessed and no slug is rebuilt. This is what makes Sprouts hours readable at all:
the storefront payload states none, and a wall clock with no zone is not a fact about a
store -- which is why Trader Joe's, whose JSON-LD hours carry no zone anywhere, still has
none. Sprouts publishes the zone, so it does.
"""

import asyncio
import json
import logging
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import Any

from app.concurrency import fanout_limit, gather_bounded
from app.normalize.availability import LIVE_STOCK, StockReporting
from app.normalize.hours import DayHours, StoreHours, hours_from_weekly, parse_clock_12h
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.instacart_storefront import (
    storefront_availability,
    storefront_product_path,
)
from app.retailers.urls import clean_product_url

log = logging.getLogger("storesplit.retailers.sprouts")

SITE_URL = "https://shop.sprouts.com"
STOREFRONT_URL = f"{SITE_URL}/store/sprouts/storefront"
IDP_URL = f"{SITE_URL}/idp/v1"
GRAPHQL_URL = f"{SITE_URL}/graphql"
SEARCH_SOURCE = "sprouts:graphql/Items"
# Sprouts' own site, not the Instacart storefront: a separate host, separate robots.txt, and
# the only surface that states a store's hours together with the timezone they are kept in.
STORE_DETAILS_URL = "https://www.sprouts.com/wp-json/spr-wp-rest/v1/store"
STORE_DETAILS_SOURCE = "sprouts:wp-json/store"
OPERATION_HASHES = {
    "SearchResultsPlacements": "a3368550c13f788bc70a15af0538456e790a0094dbfcacc0a54de54bccf38ed2",
    "Items": "388f200246a7fcc0f10ed9c1bb97952f9046e69c1be3b14ebae5855822cec831",
}
# One `Items` call answers for every id a search keeps, so a search costs two round trips
# instead of four and the batching loop below now yields a single request. Three 20-id calls
# are each a little faster than one 60-id call, but they take three of the retailer's request
# slots to do it, and slots are what a scrape runs out of. Measured against the live
# storefront: 20 ids 0.9s, 40 ids 1.1s, 60 ids 1.8s. The loop is kept so raising MAX_ITEMS
# past what one call answers stays correct.
ITEMS_PER_REQUEST = 60
MAX_ITEMS = 60
_FULFILMENT_RANK = {"instore": 0, "pickup": 1, "delivery": 2}
_CENT = Decimal("0.01")
_MONEY_RE = re.compile(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)")
_UPC_RE = re.compile(r"(\d{8,14})")
_STORE_NUMBER_RE = re.compile(r"\(Store #(\d+)\)")


class SproutsAdapter:
    site_url = SITE_URL
    slug = "sprouts"
    name = "Sprouts Farmers Market"
    # The Instacart storefront's `availability {available, stockLevel}`, per shop.
    stock_reporting: StockReporting = LIVE_STOCK

    # The storefront's guest session cookie belongs to Sprouts alone, so it keeps its own
    # persistent client with its own cookie jar.
    def __init__(self, clients: RetailerClients) -> None:
        self._client = clients.own(self.slug)
        # `www.sprouts.com` needs no session of its own, so it does not get the storefront's
        # jar. `own()` is reserved for a retailer with session state, and cookie flow runs
        # both ways: a WordPress cookie scoped to `.sprouts.com` would otherwise be stored
        # and then sent on every subsequent storefront request, changing behaviour there for
        # a reason nothing in this adapter explains.
        self._details_client = clients.shared()
        self._session_ready = False
        self._session_lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        await self._ensure_session()
        await request_with_retry(
            self._client,
            "POST",
            f"{IDP_URL}/init",
            content="{}",
            headers={"Content-Type": "text/plain;charset=UTF-8"},
        )
        response = await request_with_retry(
            self._client, "GET", f"{IDP_URL}/shops", params={"postal_code": zip_code[:5]}
        )
        response.raise_for_status()
        return parse_shops(response.json())

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        await self._ensure_session()
        search = await self._graphql(
            "SearchResultsPlacements",
            {
                "action": None,
                "query": query,
                "pageViewId": str(uuid.uuid4()),
                "elevatedProductId": None,
                "searchSource": "search",
                "filters": [],
                "disableReformulation": False,
                "disableLlm": False,
                "forceInspiration": False,
                "orderBy": "bestMatch",
                "clusterId": None,
                "includeDebugInfo": False,
                "clusteringStrategy": None,
                "contentManagementSearchParams": {"itemGridColumnCount": 7},
                "shopId": store.external_id,
                "postalCode": store.zip_code or "",
                "zoneId": "1",
                "first": 4,
            },
        )
        item_ids = parse_search_item_ids(search)[:MAX_ITEMS]
        # The item batches are independent lookups: fetch them concurrently, but keep the
        # payloads in request order so de-duplication picks the same winner as before.
        batches = [
            item_ids[start : start + ITEMS_PER_REQUEST]
            for start in range(0, len(item_ids), ITEMS_PER_REQUEST)
        ]
        payloads = await gather_bounded(
            fanout_limit(), [partial(self._items, batch, store) for batch in batches]
        )
        listings: list[ProductListing] = []
        for payload in payloads:
            listings.extend(parse_items(payload, store.external_id))
        return listings

    async def _items(self, item_ids: list[str], store: StoreLocation) -> dict[str, Any]:
        return await self._graphql(
            "Items",
            {
                "ids": item_ids,
                "shopId": store.external_id,
                "zoneId": "1",
                "postalCode": store.zip_code or "",
            },
        )

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        await self._ensure_session()
        items = await self._graphql(
            "Items",
            {
                "ids": [f"items_{_location_id(store)}-{retailer_sku}"],
                "shopId": store.external_id,
                "zoneId": "1",
                "postalCode": store.zip_code or "",
            },
        )
        listings = parse_items(items, store.external_id)
        return listings[0] if listings else None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """Hours, timezone and coordinates from Sprouts' own store record.

        One request per store per `STORE_DETAILS_TTL_SECONDS`, on the shared client rather
        than the storefront's: this host needs no session, and keeping the two jars apart
        stops a cookie travelling in either direction between them.

        A store number nobody serves answers `200` with every field null rather than `404`,
        so the record is checked for a `store_id`, not the status code.
        """
        number = store_number(store)
        if number is None:
            return None
        response = await request_with_retry(
            self._details_client, "GET", f"{STORE_DETAILS_URL}/{number}"
        )
        response.raise_for_status()
        return store_details_from_record(response.json(), store.external_id)

    async def _ensure_session(self) -> None:
        if self._session_ready:
            return
        # Concurrent categories share one adapter: only the first of them fetches the cookie.
        async with self._session_lock:
            if self._session_ready:
                return
            response = await request_with_retry(
                self._client, "GET", STOREFRONT_URL, headers={"Accept": "text/html"}
            )
            response.raise_for_status()
            if "__Host-instacart_sid" not in self._client.cookies:
                raise RuntimeError("Sprouts storefront did not issue a guest session cookie")
            self._session_ready = True

    async def _graphql(self, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await request_with_retry(
            self._client,
            "GET",
            GRAPHQL_URL,
            params={
                "operationName": operation,
                "variables": json.dumps(variables, separators=(",", ":")),
                "extensions": json.dumps(
                    {"persistedQuery": {"version": 1, "sha256Hash": OPERATION_HASHES[operation]}},
                    separators=(",", ":"),
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if errors and not payload.get("data"):
            message = errors[0].get("message", "")
            hint = " (persisted query hash is stale)" if "PersistedQuery" in message else ""
            raise RuntimeError(f"Sprouts GraphQL {operation} failed: {message}{hint}")
        return payload


def parse_shops(payload: dict[str, Any]) -> list[StoreLocation]:
    """One StoreLocation per physical store, preferring its in-store shop, then pickup."""
    best: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for index, shop in enumerate(payload.get("shops") or []):
        code = str(shop.get("location_code") or shop.get("id") or "")
        rank = _FULFILMENT_RANK.get(str(shop.get("fulfillment_option") or ""), 9)
        if not code or not shop.get("id"):
            continue
        current = best.get(code)
        if current is None or rank < current[0]:
            best[code] = (rank, current[1] if current else index, shop)
    stores: list[StoreLocation] = []
    for code, (_rank, _index, shop) in sorted(best.items(), key=lambda entry: entry[1][1]):
        address = shop.get("address") or {}
        stores.append(
            StoreLocation(
                external_id=str(shop["id"]),
                name=f"Sprouts Farmers Market {shop.get('location_name') or shop['id']}",
                address_line1=address.get("street_address") or None,
                city=address.get("city") or None,
                state=address.get("state") or None,
                zip_code=(address.get("postal_code") or "")[:5] or None,
                latitude=None,
                longitude=None,
                # Sprouts' own store number, which `www.sprouts.com` is keyed on. It is the
                # key this loop already grouped by; dropping it is what once forced the
                # hours lookup to read it back out of the display name.
                store_number=code or None,
            )
        )
    return stores


def store_number(store: StoreLocation) -> str | None:
    """Sprouts' own store number, which is what `www.sprouts.com` is keyed on.

    `external_id` cannot serve: that is the Instacart *shop* id (601, 357771), one per
    fulfilment mode, and it means nothing to Sprouts' own site. `parse_shops` carries the
    number in `store_number`, straight from the locator's own `location_code`.

    The name is a fallback, not the source. A `StoreLocation` rebuilt from a database row
    carries no `store_number`, and Instacart states the number inside the `location_name`
    this repo writes into the name ("Daly City (Store #276)"), so it can still be recovered
    there. Reading it *only* from the name is what this stopped doing: it made a display
    label load-bearing, so shortening a branch name would have silently cost every Sprouts
    store its hours, its timezone and its coordinates. A store with neither yields no hours,
    which is the right outcome for a shop whose store this cannot establish.
    """
    if store.store_number:
        return store.store_number.strip() or None
    match = _STORE_NUMBER_RE.search(store.name or "")
    return match.group(1) if match else None


def store_details_from_record(payload: Any, external_id: str) -> StoreDetails | None:
    """One `/wp-json/spr-wp-rest/v1/store/<n>` record as published store details.

    `name` is deliberately not taken: Sprouts calls this store "Daly City", where the row
    already says "Sprouts Farmers Market Daly City (Store #276)", and the shorter one would
    lose both the retailer and the number this lookup depends on.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("store_id") is None:
        return None
    timezone = str(data.get("timezone") or "").strip() or None
    return StoreDetails(
        external_id=external_id,
        name=None,
        address_line1=str(data.get("address") or "").strip() or None,
        city=str(data.get("city") or "").strip() or None,
        state=str(data.get("state") or "").strip() or None,
        zip_code=str(data.get("zip") or "").strip()[:5] or None,
        latitude=_coordinate(data.get("latitude")),
        longitude=_coordinate(data.get("longitude")),
        hours=parse_store_hours(data.get("open_time"), data.get("close_time"), timezone),
        source=STORE_DETAILS_SOURCE,
    )


def parse_store_hours(open_time: Any, close_time: Any, timezone: str | None) -> StoreHours | None:
    """One window, kept every day of the week.

    Sprouts publishes no weekday dimension at all -- a single `open_time`/`close_time` pair
    per store -- so the pattern it states is the same seven times over, and the values really
    are per store: Oakland opens at 6:00AM where Daly City opens at 7:00AM. Holiday closures
    are rendered into the store page by a script rather than published as data, so no dated
    exception is claimed here; a shop shut for Thanksgiving will read as open, exactly as the
    retailer's own JSON does.
    """
    opens = parse_clock_12h(open_time)
    closes = parse_clock_12h(close_time)
    if opens is None or closes is None:
        return None
    window = DayHours(opens, closes)
    return hours_from_weekly(dict.fromkeys(range(7), window), {}, timezone)


def _coordinate(raw: Any) -> float | None:
    """A published latitude or longitude, or nothing. `is_on_earth` checks the range later."""
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_search_item_ids(payload: dict[str, Any]) -> list[str]:
    placements = ((payload.get("data") or {}).get("searchResultsPlacements") or {}).get(
        "placements"
    ) or []
    ids: list[str] = []
    for placement in placements:
        content = placement.get("content") or {}
        if content.get("__typename") == "SearchContentManagementSearchItemGrid":
            ids.extend(str(i) for i in content.get("itemIds") or [])
    return list(dict.fromkeys(ids))


def parse_items(payload: dict[str, Any], store_external_id: str) -> list[ProductListing]:
    listings: dict[str, ProductListing] = {}
    for item in (payload.get("data") or {}).get("items") or []:
        listing = _listing_from_item(item, store_external_id)
        if listing is not None and listing.retailer_sku not in listings:
            listings[listing.retailer_sku] = listing
    return list(listings.values())


def _listing_from_item(item: dict[str, Any], store_external_id: str) -> ProductListing | None:
    product_id = item.get("productId")
    name = item.get("name")
    card = ((item.get("price") or {}).get("viewSection") or {}).get("itemCard") or {}
    price = _money(card.get("priceString"))
    if not product_id or not name or price is None:
        return None
    regular = _money(card.get("fullPriceString")) or price
    quantity = item.get("quantityAttributes") or {}
    by_weight = str(quantity.get("quantityType") or "") == "weight" or "/lb" in str(
        card.get("priceString") or ""
    )
    size_text = None if by_weight else (item.get("size") or None)
    # The storefront answers per shop, so this is the searched store's inventory. Sprouts
    # runs the same Instacart platform as Lucky and Save Mart, so it reads the payload the
    # same way: the product page's own wording decides, not the `stockLevel` token.
    stock_status, state = storefront_availability(item.get("availability"))
    view = item.get("viewSection") or {}
    upc_match = _UPC_RE.search(str(view.get("retailerLookupCodeString") or ""))
    loyalty = _money(_formatted_string(card.get("loyaltyPriceStringFormatted")))
    return ProductListing(
        retailer_sku=str(product_id),
        title=name,
        store_external_id=store_external_id,
        price=price,
        regular_price=max(regular, price),
        loyalty_price=loyalty if loyalty is not None and loyalty < price else None,
        brand=_brand(item.get("brandName")),
        # Shared with the Save Mart Companies banners: the same storefront, the same slug
        # rules, so a payload that is not really a slug falls back to the id in one place.
        product_url=clean_product_url(
            storefront_product_path("sprouts", str(product_id), item.get("evergreenUrl")),
            base_url=SITE_URL,
        ),
        image_url=listing_image_url(view.get("itemImage")),
        gtin=upc_match.group(1) if upc_match else None,
        size_text=size_text,
        price_basis=PER_POUND if by_weight else PACKAGE,
        availability=state,
        stock_status=stock_status,
        source=SEARCH_SOURCE,
        attributes={
            "item_id": str(item.get("id") or ""),
            "size": str(item.get("size") or ""),
            "price_per_unit": str(card.get("pricePerUnitString") or ""),
            "pricing_unit": str(card.get("pricingUnitString") or ""),
            "loyalty_variant": str(card.get("loyaltyProgramVariant") or ""),
        },
    )


def _location_id(store: StoreLocation) -> str:
    # Item ids are "items_<retailerLocationId>-<productId>"; the location id is learned from
    # search results and cached on the adapter via attributes when available. Fallback: shop id.
    return store.external_id


def _money(text: Any) -> Decimal | None:
    match = _MONEY_RE.search(str(text)) if text else None
    if not match:
        return None
    return Decimal(match.group(1).replace(",", "")).quantize(_CENT, ROUND_HALF_UP)


def _formatted_string(formatted: Any) -> str:
    if not isinstance(formatted, dict):
        return ""
    return "".join(str(s.get("content") or "") for s in formatted.get("sections") or [])


def _brand(raw: Any) -> str | None:
    text = str(raw or "").strip()
    return text.title() if text else None
