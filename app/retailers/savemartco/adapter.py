"""Lucky Supermarkets and Save Mart adapters.

Both Save Mart Companies banners are the same Instacart storefront with a different host, so
the acquisition lives in `storefront.py` and each adapter is just its banner's coordinates.
Package size, which the retired Swiftly search never carried, comes back on every item.

A store here is assembled from three surfaces, because no one of them describes a shop:
the storefront says which shop serves a ZIP and which *location* it sells from, the
storefront's pickup picker turns that location into a street address and the banner's own
store number, and the banner's website turns that number into the store's name, its exact
address and point, its timezone and its week. The first two are one host, the third another.
"""

import logging
from functools import partial

from app.concurrency import fanout_limit, gather_bounded
from app.normalize.availability import LIVE_STOCK, StockReporting
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.savemartco.storefront import (
    ITEMS_PER_REQUEST,
    LUCKY_BANNER,
    SAVEMART_BANNER,
    Banner,
    PickupLocation,
    Shop,
    StorefrontClient,
    fetch_store_record,
    parse_items,
    store_details_from_record,
    store_from_shop,
)

log = logging.getLogger("storesplit.retailers.savemartco")


class _SaveMartCoAdapter:
    """One banner of the Save Mart Companies storefront."""

    slug: str
    name: str
    site_url: str
    # Both banners run on the Instacart storefront, which states `availability {available,
    # stockLevel}` per shop -- the same reading `instacart_storefront.py` gives Sprouts.
    stock_reporting: StockReporting = LIVE_STOCK
    banner: Banner

    def __init__(self, clients: RetailerClients) -> None:
        # Each banner keeps its own persistent client: the guest session cookie is per host.
        self._storefront = StorefrontClient(self.banner, clients.own(self.slug))
        # The banner's own website needs no session, and must not share the storefront's jar:
        # a cookie set on `luckysupermarkets.com` would travel to `shop.luckysupermarkets.com`
        # on every subsequent request. Same reasoning as Sprouts' details client.
        self._details_client = clients.shared()
        self._zip_by_store: dict[str, str] = {}
        # Store identity -> the shop id its prices are addressed to. A shop is a fulfilment
        # mode of a store, so the row is keyed on the store and the queries go to the shop;
        # this is what joins the two, for the run that discovered them.
        self._shop_by_store: dict[str, str] = {}
        # One read of a store's own page per run, shared by `find_stores` (which needs the
        # name) and `fetch_store_details` (which needs the hours). `None` records an attempt
        # that found nothing, so a retired store number is not asked for twice in one run.
        self._records: dict[str, dict[str, object] | None] = {}

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        """The store this banner serves the ZIP from, or nothing where it has none there.

        One store, because `DefaultShop` names one: this banner's locator is not a directory
        and asking it for a ZIP asks which shop serves that ZIP. What has changed is *which*
        store that answer describes -- the physical one, named and placed, rather than a shop
        id with a ZIP attached.

        Everything after the shop lookup is enrichment, and none of it may cost the ZIP its
        prices. A pickup picker that fails leaves the store keyed on the location the
        storefront already named; a banner website that fails leaves it with the picker's
        address. Both failing leaves exactly what this returned before any of this existed.
        """
        zip5 = zip_code.strip()[:5]
        shop = await self._storefront.default_shop(zip5)
        if shop is None:
            return []
        location = await self._physical_store(shop, zip5)
        details = await self._published_details(shop.store_id, location)
        store = store_from_shop(shop, location, details, self.banner, zip5)
        self._zip_by_store[store.external_id] = zip5
        self._shop_by_store[store.external_id] = shop.shop_id
        return [store]

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """Hours, timezone, phone and the exact address, from the banner's own store page.

        The record was already read by `find_stores`, which needs the store's name from it,
        so this costs no request of its own in an ordinary scrape -- and still reads one when
        called on its own.
        """
        if store.store_number is None:
            return None
        record = await self._store_record(store.store_number)
        if record is None:
            return None
        return store_details_from_record(
            record, store.external_id, self._details_source, banner=self.banner
        )

    async def _physical_store(self, shop: Shop, zip_code: str) -> PickupLocation | None:
        """Which of the banner's stores this shop sells from, with its address.

        The pickup picker lists the banner's stores near a ZIP; the one whose id is the
        shop's `retailerLocationId` is this shop's store. Nothing is matched on distance or
        on a name: the storefront stated the id, and the picker publishes the same ids.
        """
        if shop.retailer_id is None or shop.location_id is None:
            return None
        try:
            locations = await self._storefront.pickup_locations(shop.retailer_id, zip_code)
        except Exception as exc:  # a street address is never worth a ZIP's prices
            log.warning(
                "savemartco_pickup_locations_failed",
                extra={"retailer": self.slug, "zip_code": zip_code, "error": str(exc)},
            )
            return None
        found = next((s for s in locations if s.location_id == shop.location_id), None)
        if found is None:
            log.info(
                "savemartco_location_not_listed",
                extra={"retailer": self.slug, "location": shop.location_id},
            )
        return found

    async def _published_details(
        self, external_id: str, location: PickupLocation | None
    ) -> StoreDetails | None:
        """What the banner's own site says about this store, or nothing if it did not say."""
        if location is None or location.store_number is None:
            return None
        try:
            record = await self._store_record(location.store_number)
        except Exception as exc:
            log.warning(
                "savemartco_store_page_failed",
                extra={
                    "retailer": self.slug,
                    "store_number": location.store_number,
                    "error": str(exc),
                },
            )
            return None
        if record is None:
            return None
        return store_details_from_record(
            record, external_id, self._details_source, banner=self.banner
        )

    async def _store_record(self, store_number: str) -> dict[str, object] | None:
        if store_number not in self._records:
            self._records[store_number] = await fetch_store_record(
                self._details_client, self.banner, store_number
            )
        return self._records[store_number]

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        zip_code, shop_id = self._zip_of(store), self._shop_of(store)
        item_ids = await self._storefront.search_item_ids(query, shop_id, zip_code)
        # Independent item batches: concurrent, but consumed in request order.
        batches = [
            item_ids[start : start + ITEMS_PER_REQUEST]
            for start in range(0, len(item_ids), ITEMS_PER_REQUEST)
        ]
        payloads = await gather_bounded(
            fanout_limit(),
            [partial(self._storefront.items, batch, shop_id, zip_code) for batch in batches],
        )
        listings: list[ProductListing] = []
        for payload in payloads:
            listings.extend(
                parse_items(payload, store.external_id, self._source, banner=self.banner)
            )
        return listings

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        # Item ids are "items_<retailerLocationId>-<productId>" and the location id is only
        # learned from a search, so a product is fetched by searching for its own id.
        zip_code, shop_id = self._zip_of(store), self._shop_of(store)
        item_ids = await self._storefront.search_item_ids(retailer_sku, shop_id, zip_code)
        if not item_ids:
            return None
        payload = await self._storefront.items(item_ids[:ITEMS_PER_REQUEST], shop_id, zip_code)
        for listing in parse_items(payload, store.external_id, self._source, banner=self.banner):
            if listing.retailer_sku == retailer_sku:
                return listing
        return None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    @property
    def _source(self) -> str:
        return f"{self.slug}:graphql/Items"

    @property
    def _details_source(self) -> str:
        return f"{self.slug}:stores/storeDetailsV2"

    def _zip_of(self, store: StoreLocation) -> str:
        return store.zip_code or self._zip_by_store.get(store.external_id, "")

    def _shop_of(self, store: StoreLocation) -> str:
        """The shop id a price query for this store is addressed to.

        A store row is the physical location, so its `external_id` is not a shop id; the
        mapping is made when the store is discovered. The fallback is `external_id` itself,
        which is what a store whose payload named no location already is.
        """
        return self._shop_by_store.get(store.external_id, store.external_id)


class LuckyAdapter(_SaveMartCoAdapter):
    slug = "lucky"
    name = "Lucky Supermarkets"
    site_url = LUCKY_BANNER.site_url
    banner = LUCKY_BANNER


class SaveMartAdapter(_SaveMartCoAdapter):
    slug = "savemart"
    name = "Save Mart"
    site_url = SAVEMART_BANNER.site_url
    banner = SAVEMART_BANNER
