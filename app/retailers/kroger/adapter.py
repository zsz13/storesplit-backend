"""Kroger adapter built on the official Kroger Developer API (https://developer.kroger.com).

Requires free client credentials (KROGER_CLIENT_ID / KROGER_CLIENT_SECRET). Endpoints:
  * POST /v1/connect/oauth2/token   client_credentials grant, scope product.compact
  * GET  /v1/locations              filter.zipCode.near=<zip>
  * GET  /v1/products               filter.term=<q>&filter.locationId=<id>
  * GET  /v1/products/<productId>   filter.locationId=<id>
Kroger banners (Ralphs, Fred Meyer, King Soopers, ...) all come through the same API.
"""

import asyncio
import logging
import re
import time
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.normalize.availability import (
    IN_STOCK,
    LIVE_STOCK,
    OUT_OF_STOCK,
    UNKNOWN,
    Availability,
    StockReporting,
)
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import (
    AdapterUnavailableError,
    ProductListing,
    StoreLocation,
    gather_offers,
)
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url

log = logging.getLogger("storesplit.retailers.kroger")

BASE_URL = "https://api.kroger.com/v1"
SITE_URL = "https://www.kroger.com"
# Kroger publishes an inventory band, not a count: HIGH and LOW both sell.
_STOCK_LEVELS: dict[str, Availability] = {
    "HIGH": IN_STOCK,
    "LOW": IN_STOCK,
    "TEMPORARILY_OUT_OF_STOCK": OUT_OF_STOCK,
}
PRODUCTS_SOURCE = "kroger:v1/products"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class KrogerAdapter:
    site_url = SITE_URL
    slug = "kroger"
    name = "Kroger"
    # `items[].inventory.stockLevel` per store: HIGH, LOW and TEMPORARILY_OUT_OF_STOCK.
    stock_reporting: StockReporting = LIVE_STOCK

    # Bearer tokens travel per request, so Kroger shares the application client.
    def __init__(self, clients: RetailerClients) -> None:
        settings = get_settings()
        self._client_id = settings.kroger_client_id
        self._client_secret = settings.kroger_client_secret
        self._client = clients.shared()
        self._token: str | None = None
        self._token_expires_at = 0.0
        # Concurrent requests must not each mint their own token.
        self._token_lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        payload = await self._get(
            "/locations", {"filter.zipCode.near": zip_code[:5], "filter.limit": 5}
        )
        return parse_locations(payload)

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        payload = await self._get(
            "/products",
            {"filter.term": query, "filter.locationId": store.external_id, "filter.limit": 50},
        )
        return parse_products(payload, store.external_id)

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        payload = await self._get(
            f"/products/{retailer_sku}", {"filter.locationId": store.external_id}
        )
        data = payload.get("data")
        if not data:
            return None
        listings = parse_products({"data": [data]}, store.external_id)
        return listings[0] if listings else None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    # -- internals -------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()

    async def _access_token(self) -> str:
        if not self.is_configured():
            raise AdapterUnavailableError("Kroger credentials are not configured")
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = await request_with_retry(
                self._client,
                "POST",
                f"{BASE_URL}/connect/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials", "scope": "product.compact"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            body = response.json()
            token = str(body["access_token"])
            self._token = token
            self._token_expires_at = time.monotonic() + max(
                60, int(body.get("expires_in", 1800)) - 60
            )
            return token


def parse_locations(payload: dict[str, Any]) -> list[StoreLocation]:
    stores: list[StoreLocation] = []
    for loc in payload.get("data", []):
        address = loc.get("address") or {}
        geo = loc.get("geolocation") or {}
        if not loc.get("locationId"):
            continue
        stores.append(
            StoreLocation(
                external_id=str(loc["locationId"]),
                name=loc.get("name") or f"{loc.get('chain', 'Kroger')} {loc['locationId']}",
                address_line1=address.get("addressLine1"),
                city=address.get("city"),
                state=address.get("state"),
                zip_code=(address.get("zipCode") or "")[:5] or None,
                latitude=geo.get("latitude"),
                longitude=geo.get("longitude"),
            )
        )
    return stores


def parse_products(payload: dict[str, Any], store_external_id: str) -> list[ProductListing]:
    listings: list[ProductListing] = []
    for product in payload.get("data", []):
        listing = _listing_from_product(product, store_external_id)
        if listing is not None:
            listings.append(listing)
    return listings


def _listing_from_product(product: dict[str, Any], store_external_id: str) -> ProductListing | None:
    product_id = product.get("productId")
    title = product.get("description")
    items = product.get("items") or []
    if not product_id or not title or not items:
        return None
    item = items[0]
    price_info = item.get("price") or {}
    regular = price_info.get("regular")
    if regular is None:
        return None
    promo = price_info.get("promo") or 0
    regular_price = Decimal(str(regular))
    promo_price = Decimal(str(promo)) if promo else None
    # Kroger promo prices need a loyalty card, so the shelf price stays the comparison price
    # and the promo is reported as loyalty_price, like-for-like with other retailers.
    loyalty_price = promo_price if promo_price and promo_price < regular_price else None
    price = regular_price
    # Kroger states the basis outright. Its weight-sold items still carry a `size` ("1 lb"),
    # which every other adapter here drops for a good reason: a package size printed beside a
    # per-pound price is an invitation to divide one by the other, and it is a size the
    # shopper does not get -- the scale at the till decides. Today that division is refused
    # in `listing_quantity`; dropping the string as well means there is nothing to divide.
    by_weight = (item.get("soldBy") or "").upper() == "WEIGHT"
    price_basis = PER_POUND if by_weight else PACKAGE
    size_text = None if by_weight else item.get("size")
    stock = (item.get("inventory") or {}).get("stockLevel")
    upc = product.get("upc")
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return ProductListing(
        retailer_sku=str(product_id),
        title=title,
        store_external_id=store_external_id,
        price=price,
        regular_price=regular_price,
        loyalty_price=loyalty_price,
        brand=product.get("brand") or None,
        product_url=clean_product_url(f"/p/{slug}/{product_id}", base_url=SITE_URL),
        image_url=_front_image(product.get("images")),
        gtin=str(upc) if upc else None,
        size_text=size_text,
        price_basis=price_basis,
        availability=_STOCK_LEVELS.get(str(stock).upper(), UNKNOWN) if stock else UNKNOWN,
        stock_status=stock.lower() if isinstance(stock, str) else None,
        source=PRODUCTS_SOURCE,
        attributes={"categories": ", ".join(product.get("categories") or [])},
    )


def _front_image(images: Any) -> str | None:
    """Kroger nests images two levels deep: `images[].sizes[].url`, one entry per
    perspective. Only the front of the pack is worth showing, and `medium` is the
    thumbnail size; anything the payload does not actually spell as a string is nothing.
    """
    if not isinstance(images, list):
        return None
    for image in images:
        if not isinstance(image, dict) or image.get("perspective") != "front":
            continue
        sizes = image.get("sizes")
        if not isinstance(sizes, list):
            continue
        preferred = [s for s in sizes if isinstance(s, dict) and s.get("size") == "medium"]
        return listing_image_url(preferred, sizes)
    return None
