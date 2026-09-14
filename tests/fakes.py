"""An in-memory retailer adapter for service and API tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import partial

import httpx
from app.concurrency import fanout_limit, gather_bounded
from app.normalize.units import QuantityRange
from app.retailers.base import ProductListing, StoreDetails, StoreLocation
from app.retailers.http import request_with_retry


class Tracker:
    """Counts overlapping calls so a test can prove work really ran concurrently."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.total = 0

    @asynccontextmanager
    async def track(self) -> AsyncIterator[None]:
        self.in_flight += 1
        self.total += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            yield
        finally:
            self.in_flight -= 1


FAKE_SITE_URL = "https://example.test"


def listing(
    sku: str,
    title: str,
    store: str,
    price: str,
    *,
    brand: str | None = None,
    size_text: str | None = None,
    regular_price: str | None = None,
    loyalty_price: str | None = None,
    gtin: str | None = None,
    price_basis: str = "package",
    weight_range: QuantityRange | None = None,
    max_total_price: str | None = None,
    availability: str = "in_stock",
) -> ProductListing:
    return ProductListing(
        retailer_sku=sku,
        title=title,
        store_external_id=store,
        price=Decimal(price),
        regular_price=Decimal(regular_price or price),
        loyalty_price=Decimal(loyalty_price) if loyalty_price else None,
        brand=brand,
        product_url=f"{FAKE_SITE_URL}/{sku}",
        gtin=gtin,
        size_text=size_text,
        price_basis=price_basis,  # type: ignore[arg-type]
        weight_range=weight_range,
        max_total_price=Decimal(max_total_price) if max_total_price else None,
        availability=availability,  # type: ignore[arg-type]
        source="fake:test",
    )


class FakeAdapter:
    # Product URLs are checked against the adapter's own site before they are stored.
    site_url = FAKE_SITE_URL

    def __init__(
        self,
        slug: str,
        stores: list[StoreLocation],
        catalog: dict[str, dict[str, list[ProductListing]]],
        configured: bool = True,
        *,
        delay: float = 0.0,
        tracker: Tracker | None = None,
        store_details: StoreDetails | None = None,
    ) -> None:
        """catalog[query][store_external_id] -> listings"""
        self.slug = slug
        self.name = slug.title()
        self._stores = stores
        self._catalog = catalog
        self._configured = configured
        self.calls: list[tuple[str, str]] = []
        self._store_details = store_details
        self.detail_calls = 0
        if store_details is not None:
            # Bound only when there is something to publish, so an adapter without the
            # capability really does not have the attribute -- which is what the scrape
            # service probes for.
            self.fetch_store_details = self._fetch_store_details
        self.fail_with: Exception | None = None
        self.delay = delay
        self.tracker = tracker or Tracker()
        self.own_tracker = Tracker()

    def is_configured(self) -> bool:
        return self._configured

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        return [s for s in self._stores if s.zip_code and s.zip_code[:3] == zip_code[:3]]

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        self.calls.append((query, store.external_id))
        async with self.tracker.track(), self.own_tracker.track():
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_with is not None:
                raise self.fail_with
            return self._catalog.get(query, {}).get(store.external_id, [])

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        for listings in self._catalog.values():
            for item in listings.get(store.external_id, []):
                if item.retailer_sku == retailer_sku:
                    return item
        return None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return [o for s in stores if (o := await self.fetch_product(retailer_sku, s))]

    async def _fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        self.detail_calls += 1
        return self._store_details


STORE_A = StoreLocation("A1", "Alpha Market Downtown", city="San Francisco", zip_code="94105")
STORE_B = StoreLocation("B1", "Beta Foods SoMa", city="San Francisco", zip_code="94107")


def two_retailers() -> tuple[FakeAdapter, FakeAdapter]:
    """Two retailers with overlapping staples and deliberately different prices."""
    alpha = FakeAdapter(
        "alpha",
        [STORE_A],
        {
            "eggs": {
                "A1": [
                    listing(
                        "a-eggs-12", "Large Grade A Eggs, 12 CT", "A1", "4.99", brand="Farm Co"
                    ),
                    listing(
                        "a-eggs-18", "Large Grade A Eggs, 18 CT", "A1", "6.49", brand="Farm Co"
                    ),
                    listing("a-kimchi", "Kimchi, 16 OZ", "A1", "8.99", brand="Ferment Inc"),
                ]
            },
            "chicken breast": {
                "A1": [
                    listing(
                        "a-chx",
                        "Boneless Skinless Chicken Breast",
                        "A1",
                        "6.99",
                        price_basis="lb",
                    )
                ]
            },
            "milk": {
                "A1": [listing("a-milk", "Whole Milk, 1 GL", "A1", "5.49", brand="Dairy Best")]
            },
        },
    )
    beta = FakeAdapter(
        "beta",
        [STORE_B],
        {
            "eggs": {
                "B1": [
                    listing(
                        "b-eggs-12",
                        "Grade A Large Eggs",
                        "B1",
                        "3.99",
                        brand="Farm Co",
                        size_text="12 ct",
                        gtin="0001111060903",
                    ),
                    listing(
                        "b-eggs-liq",
                        "Liquid Egg Whites",
                        "B1",
                        "4.49",
                        brand="Farm Co",
                        size_text="16 oz",
                    ),
                ]
            },
            "chicken breast": {
                "B1": [
                    listing(
                        "b-chx",
                        "Boneless Skinless Chicken Breast Value Pack",
                        "B1",
                        "22.50",
                        size_text="3 lb",
                    )
                ]
            },
            "milk": {
                "B1": [
                    listing(
                        "b-milk",
                        "Whole Milk",
                        "B1",
                        "3.29",
                        brand="Dairy Best",
                        size_text="1/2 gal",
                    )
                ]
            },
        },
    )
    return alpha, beta


class NestedFakeAdapter:
    """A retailer whose search fans out again, the way Raley's, Sprouts and Safeway do.

    Its leaves go through `request_with_retry`, so it exercises the real per-retailer request
    budget rather than a counter of its own.
    """

    slug = "nested"
    name = "Nested"
    site_url = "https://nested.test"

    def __init__(self, tracker: Tracker, pages: int = 4, delay: float = 0.02) -> None:
        self.tracker = tracker
        self.pages = pages

        async def handler(request: httpx.Request) -> httpx.Response:
            async with tracker.track():
                await asyncio.sleep(delay)
            return httpx.Response(200, json={"ok": True})

        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def aclose(self) -> None:
        await self._client.aclose()

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        return [STORE_A, STORE_B]

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        await gather_bounded(
            fanout_limit(), [partial(self._page, page) for page in range(self.pages)]
        )
        return []

    async def _page(self, page: int) -> None:
        await request_with_retry(self._client, "GET", f"https://nested.test/{page}")

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> None:
        return None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return []
