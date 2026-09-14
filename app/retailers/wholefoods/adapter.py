"""Whole Foods Market adapter.

Data sources (all plain HTTPS JSON, no browser automation):
  * GET https://www.wholefoodsmarket.com/api/search?text=<q>&store=<storeId>
        store-specific search results with regular and sale prices.
  * GET https://www.wholefoodsmarket.com/api/stores/<storeId>/summary
        store name, address, ZIP and coordinates; used to build the vendored store list.
  * GET https://www.wholefoodsmarket.com/api/wwos/products
        ?offerListingDiscriminator=<store code>&programType=GROCERY&asins=A,B,C
        **the availability the product page itself renders from**, up to 50 ASINs per call.
  * GET https://www.wholefoodsmarket.com/grocery/product/<slug>?store=<storeId>
        product page; `__NEXT_DATA__` carries brand, ASIN and -- the reason this is fetched
        once per store -- `wfmccLocationData.cateringStoreContext.almAttributes
        .offerListingDiscriminator`, the per-store code the availability call needs.
        `/product/<slug>` (no `/grocery`) is the retired path and answers 301.

**Availability, and the two lookalikes that are not it.** Search carries no availability at
all, and one obvious candidate is a decoy:

* `/api/product/<slug>?store=<id>`.isAvailable is *carriage*, not stock: true for every item
  in that store's own search (measured 393/393 at store 10151) and false only for items the
  store does not carry. Listings are built from that same search, so it is true by
  construction. It costs one request per listing and buys nothing. Do not reintroduce it.
* The product page's server-rendered `availability` is always null and its price absent,
  because anonymously it renders for a default store -- `locationCookie` comes back as
  Lamar, Austin TX -- whatever store the item was scraped from. **That is why a Whole Foods
  item stocked and priced at the scraped store still opens as out of stock.** The link is
  the right product either way: the search slug's suffix is the item's ASIN.

`/api/wwos/products` is the real signal, and it does vary within one store: 161 IN_STOCK
against 139 not, over 300 ASINs from store 10151's own search. `IN_STOCK` is trusted;
anything else -- including the `null` that accompanies a missing price and offer listing --
is reported `unknown` rather than `out_of_stock`, because a null is an absent answer and one
reading of it was seen to flap. Availability therefore costs one page fetch per store plus
one batched call per 50 listings, not one call per listing.

**Whole Foods publishes a positive and nothing else, and its page's "Out of Stock" is an
inference, not a report.** `availability` is `"IN_STOCK"` or `null`; no negative word has
appeared in ~400 observations, and none appears in a full browser session with a store
chosen either. What the PDP prints over the null -- "Out of Stock", "Currently not sold in
Stonestown" -- is the page drawing the same conclusion from the same absent answer, and it
is not a stable one: B07YFT8JTH rendered a live "$9.99/lb, Pickup from Stonestown, Add to
Cart" and "Currently not sold in Stonestown / Out of Stock" minutes apart at the same store.
So the absent answer is `unknown` at any number of reads (`resolve_availability`), and the
second read exists only to catch an offer the first one missed.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.normalize.availability import IN_STOCK, LIVE_STOCK, OUT_OF_STOCK, UNKNOWN, StockReporting
from app.normalize.hours import DayHours, StoreHours, hours_from_published_days
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url
from app.retailers.wholefoods.stores import find_stores_for_zip, store_folder, store_from_summary

log = logging.getLogger("storesplit.retailers.wholefoods")

BASE_URL = "https://www.wholefoodsmarket.com"
SEARCH_SOURCE = "wholefoods:api/search"
PDP_SOURCE = "wholefoods:product-page"
AVAILABILITY_SOURCE = "wholefoods:api/wwos/products"
# The endpoint answered for 50 ASINs in one call; keep a batch at that.
ASINS_PER_REQUEST = 50

# Results carry "uom": "lb" when priced per pound; store-counter pseudo-brands below are also
# per pound when the title carries no package size (e.g. "Banana"). Counter brands are not
# real brands and are dropped.
_COUNTER_BRANDS = {"produce", "meat", "seafood"}
_ASIN_RE = re.compile(r"-(b0[a-z0-9]{8})$", re.IGNORECASE)
# The shape the availability endpoint accepts, used to keep anything else out of a batch.
_ASIN_ONLY_RE = re.compile(r"b0[a-z0-9]{8}", re.IGNORECASE)
_SIZE_SUFFIX_RE = re.compile(r",\s*([^,]+)$")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
# The store page keeps its details in an `a-state` island rather than `__NEXT_DATA__`, whose
# pageProps on that page carry nothing but a nonce.
_STORE_STATE_RE = re.compile(
    r'<script type="a-state" data-a-state="\{&quot;key&quot;:&quot;detail-page-state&quot;\}">'
    r"(.*?)</script>",
    re.S,
)
STORE_PAGE_SOURCE = "wholefoods:store-page"


@dataclass(frozen=True)
class StoreContext:
    """Proof that a product page was rendered for the store it was asked about."""

    store_external_id: str
    discriminator: str
    is_default_location: bool


def parse_store_context(html: str) -> StoreContext | None:
    """The store a product page really rendered for, or `None` when it cannot be shown.

    Three fields have to agree. `isDefaultLocation` must be false -- true means Whole Foods
    fell back to its own default and the prices and availability on the page belong to
    somewhere else. `overrideStoreId` and `almAttributes.storeId` must name the same store:
    when they disagree there is no answer to believe. `locationCookie` is not consulted at
    all; it reports "Lamar", Austin TX on every page, San Francisco ones included, and
    reading it was what made a store-scoped page look like a default one.
    """
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        page_props = (json.loads(match.group(1)) or {}).get("props", {}).get("pageProps", {})
    except ValueError:
        return None
    if page_props.get("isDefaultLocation") is not False:
        return None
    alm = ((page_props.get("wfmccLocationData") or {}).get("cateringStoreContext") or {}).get(
        "almAttributes"
    ) or {}
    store_id = str(alm.get("storeId") or "").strip()
    override = str(page_props.get("overrideStoreId") or "").strip()
    discriminator = str(alm.get("offerListingDiscriminator") or "").strip()
    if not store_id or not discriminator or override != store_id:
        return None
    return StoreContext(
        store_external_id=store_id, discriminator=discriminator, is_default_location=False
    )


class WholeFoodsAdapter:
    slug = "wholefoods"
    name = "Whole Foods Market"
    site_url = BASE_URL
    # `live` because `/api/wwos/products` really does state per-store stock -- but only ever
    # a positive. An `unknown` here is therefore exactly what `live` promises it is: a reading
    # that failed, because the endpoint answered without the availability it had last time.
    stock_reporting: StockReporting = LIVE_STOCK

    # Nothing but plain JSON GETs: the shared application client is enough.
    def __init__(self, clients: RetailerClients) -> None:
        self._client = clients.shared()
        # store external id -> the proven context, or None once a lookup has failed for it.
        self._contexts: dict[str, StoreContext | None] = {}
        # One lock per store, because a scrape runs every category for a store at once.
        self._context_locks: dict[str, asyncio.Lock] = {}

    def is_configured(self) -> bool:
        return True

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        return find_stores_for_zip(zip_code)  # vendored directory, no I/O

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        listings = await self._search(query, store)
        return await self._with_availability(listings, store)

    async def _search(self, query: str, store: StoreLocation) -> list[ProductListing]:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}/api/search",
            params={"text": query, "store": store.external_id},
        )
        response.raise_for_status()
        return parse_search_results(response.json(), store.external_id)

    async def _with_availability(
        self, listings: list[ProductListing], store: StoreLocation
    ) -> list[ProductListing]:
        """Fold `/api/wwos/products` for this store into the listings, in two batched passes.

        The first pass answers every listing. The second re-asks only the ones that came back
        with no availability and no offer, because that shape is an absent answer that the
        endpoint returns intermittently even for items it will sell: a re-read turns a fair
        share of them into the live offer they should have had. It can only add a positive.
        Re-asking is cheap -- a different batch, so a fresh answer rather than the same cached
        one, and one call per 50 rather than a page fetch per product.

        Nothing here fails a search. An unproven store or a failed lookup leaves listings
        `unknown`, which is what they already were.
        """
        if not listings:
            return listings
        context = await self._store_context(store, listings[0])
        if context is None:
            return listings
        # Only real ASINs are asked about. A search result whose slug carries no `-b0…`
        # suffix keeps the slug as its SKU, and one malformed value in a batch of fifty can
        # cost the answer for the other forty-nine.
        askable = [
            item.retailer_sku for item in listings if _ASIN_ONLY_RE.fullmatch(item.retailer_sku)
        ]
        first: dict[str, Any] = {}
        for start in range(0, len(askable), ASINS_PER_REQUEST):
            batch = askable[start : start + ASINS_PER_REQUEST]
            first.update(index_wwos(await self._fetch_wwos(batch, context.discriminator)))
        reread = await self._reread_missing_offers(listings, first, context.discriminator)
        resolved = resolve_availability(listings, first, reread)
        # Every one of these was priced and read for a store the page proved it rendered for.
        return [replace(item, store_context=context.store_external_id) for item in resolved]

    async def _reread_missing_offers(
        self, listings: list[ProductListing], first: dict[str, Any], discriminator: str
    ) -> dict[str, Any] | None:
        """A second read of the ASINs that came back with no offer, best-effort."""
        missing = [
            item.retailer_sku.upper()
            for item in listings
            if wwos_signal(first.get(item.retailer_sku.upper())) == "no_offer"
        ]
        if not missing:
            return {}
        reread: dict[str, Any] = {}
        for start in range(0, len(missing), ASINS_PER_REQUEST):
            payload = await self._fetch_wwos(
                missing[start : start + ASINS_PER_REQUEST], discriminator
            )
            # A batch that failed is simply an answer nobody got, and the ASINs in it keep the
            # `unknown` they already had. The rest are kept: this pass can only *add* a
            # positive, so throwing away the batches that did answer -- which is what a whole
            # discard used to do, back when a second silence asserted a negative and symmetry
            # mattered -- would lose proven offers to buy in exchange for nothing.
            if payload is not None:
                reread.update(index_wwos(payload))
        return reread

    async def _fetch_wwos(self, asins: list[str], discriminator: str) -> list[Any] | None:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}/api/wwos/products",
            params={
                "offerListingDiscriminator": discriminator,
                "programType": "GROCERY",
                "asins": ",".join(asins),
            },
            max_retries=1,
        )
        if response.status_code != 200:
            log.warning("wwos_unavailable", extra={"status": response.status_code})
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, list) else None

    async def _store_context(
        self, store: StoreLocation, sample: ProductListing
    ) -> StoreContext | None:
        """The store's proven context, read once per store and cached.

        `offerListingDiscriminator` is published only inside a product page rendered for that
        store, so this costs one HTML fetch the first time a store is searched and nothing
        afterwards -- and that same page is what proves the store. A page that rendered for
        somewhere else yields nothing: its discriminator would key every availability lookup
        for this store to another store's shelves, which is exactly the failure being
        guarded. The listings then stay `unknown`.
        """
        if store.external_id in self._contexts:
            return self._contexts[store.external_id]
        slug = sample.attributes.get("slug")
        if not slug:
            return None
        # A scrape searches every category for a store concurrently, and they all arrive
        # here at once. Reading the cache and filling it on either side of an `await` is not
        # enough: each would start its own fetch, so the page was read once per category,
        # and whichever finished last decided the store's availability for the whole run.
        lock = self._context_locks.setdefault(store.external_id, asyncio.Lock())
        async with lock:
            if store.external_id in self._contexts:  # filled while this task waited
                return self._contexts[store.external_id]
            response = await request_with_retry(
                self._client,
                "GET",
                f"{BASE_URL}/grocery/product/{slug}",
                params={"store": store.external_id},
                headers={"Accept": "text/html"},
                max_retries=1,
            )
            context = parse_store_context(response.text) if response.status_code == 200 else None
            if context is None:
                log.warning("wfm_store_context_unproven", extra={"store": store.external_id})
            elif context.store_external_id != store.external_id:
                log.warning(
                    "wfm_store_context_mismatch",
                    extra={"store": store.external_id, "rendered": context.store_external_id},
                )
                context = None
            self._contexts[store.external_id] = context
            return context

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        # The store-specific price lives in search; the product page adds metadata only.
        for listing in await self.search_products(retailer_sku, store):
            if listing.retailer_sku == retailer_sku:
                return listing
        return None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """The optional store-details capability: address, timezone and opening hours.

        One GET of the store's own page, which the scrape service calls at most once a week
        per store and caches in the database. The page proves which store it is -- its
        `storeCode` -- so a stale or wrong folder yields nothing rather than another store's
        opening times.
        """
        folder = store_folder(store.external_id)
        if not folder:
            return None
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}/stores/{folder}",
            headers={"Accept": "text/html"},
            max_retries=1,
        )
        if response.status_code != 200:
            return None
        details = parse_store_details(response.text)
        if details is None or details.external_id != store.external_id:
            log.warning(
                "wfm_store_page_mismatch",
                extra={"store": store.external_id, "folder": folder},
            )
            return None
        return details

    async def fetch_store_summary(self, store_id: int) -> StoreLocation | None:
        response = await request_with_retry(
            self._client, "GET", f"{BASE_URL}/api/stores/{store_id}/summary", max_retries=1
        )
        if response.status_code != 200:
            return None
        return store_from_summary(response.json())

    async def fetch_product_page(self, slug: str, store: StoreLocation) -> dict[str, Any] | None:
        """Metadata (brand, asin, availability, unit price) from the product page.

        Always store-scoped. Fetched without `?store=`, this page renders for Whole Foods'
        own default location and reports no availability and no price at all, which is what
        once made "the product page's availability is always null" look like a fact about the
        page rather than about how it was being asked for.
        """
        response = await request_with_retry(
            self._client,
            "GET",
            f"{BASE_URL}/grocery/product/{slug}",
            params={"store": store.external_id},
            headers={"Accept": "text/html"},
        )
        if response.status_code != 200:
            return None
        return parse_product_page(response.text)


def parse_search_results(payload: dict[str, Any], store_external_id: str) -> list[ProductListing]:
    listings: list[ProductListing] = []
    for item in payload.get("results", []):
        listing = _listing_from_result(item, store_external_id)
        if listing is not None:
            listings.append(listing)
    return listings


def _listing_from_result(item: dict[str, Any], store_external_id: str) -> ProductListing | None:
    name = item.get("name")
    slug = item.get("slug")
    regular = item.get("regularPrice")
    if not name or not slug or regular is None:
        return None
    sale = item.get("salePrice")
    regular_price = Decimal(str(regular))
    price = Decimal(str(sale)) if sale is not None else regular_price
    brand_raw = (item.get("brand") or "").strip()
    is_counter_item = brand_raw.lower() in _COUNTER_BRANDS
    size_match = _SIZE_SUFFIX_RE.search(name)
    size_text = size_match.group(1).strip() if size_match else None
    priced_per_pound = str(item.get("uom") or "").lower() == "lb" or is_counter_item
    price_basis = PER_POUND if priced_per_pound and size_text is None else PACKAGE
    asin_match = _ASIN_RE.search(slug)
    sku = asin_match.group(1).upper() if asin_match else slug
    return ProductListing(
        retailer_sku=sku,
        title=name,
        store_external_id=store_external_id,
        price=price,
        regular_price=regular_price,
        loyalty_price=None,
        brand=None if is_counter_item or not brand_raw else brand_raw,
        product_url=clean_product_url(f"/grocery/product/{slug}", base_url=BASE_URL),
        image_url=listing_image_url(item.get("imageThumbnail")),
        gtin=None,
        size_text=size_text,
        price_basis=price_basis,
        # Search says nothing about stock; `_with_availability` fills this in from
        # `/api/wwos/products`. Until it answers, `unknown` -- never an optimistic guess.
        availability=UNKNOWN,
        stock_status=None,
        source=SEARCH_SOURCE,
        attributes={"slug": slug, "is_local": str(bool(item.get("isLocal"))).lower()},
    )


# Availability words Whole Foods states outright. `IN_STOCK` is the only positive one seen in
# 574 measured records; the negatives are mapped defensively so that a word the retailer
# starts using is read correctly rather than silently becoming `unknown`. A word on neither
# list never reaches `in_stock` on its own.
_STATED_IN_STOCK = {"IN_STOCK"}
_STATED_OUT_OF_STOCK = {"OUT_OF_STOCK", "NOT_AVAILABLE", "UNAVAILABLE", "OUT_OF_STOCK_ONLINE"}

Signal = Literal["positive", "stated_negative", "no_offer", "ambiguous", "absent"]


def wwos_signal(record: Any) -> Signal:
    """How to read one `/api/wwos/products` record.

    * `positive` -- the retailer states `IN_STOCK`, or offers a live `offerListingId`, which
      is the thing a shopper actually clicks to buy. A *price* is not on this list: being
      listed or priced is not being in stock.
    * `stated_negative` -- the retailer names an unavailable state. Believe it at once.
    * `no_offer` -- no stated availability and no offer at all. **This is an absent answer,
      not a negative one**, and the name says so because calling it "negative" is what once
      made StoreSplit assert it. The product page prints "Out of Stock" over this shape, but
      the page is inferring, not reporting: the same ASIN at the same store comes back with a
      live offer on some reads and this shape on others.
    * `ambiguous` -- something in between, e.g. a price with no way to order and no stated
      state, or a word nobody has mapped.
    * `absent` -- the endpoint did not answer for this ASIN.
    """
    if not isinstance(record, dict):
        return "absent"
    raw = record.get("availability")
    stated = str(raw).strip().upper() if raw is not None else ""
    offer = record.get("offerDetails")
    orderable = bool(isinstance(offer, dict) and offer.get("offerListingId"))
    # The stated word is read first, so an `offerListingId` cannot overrule it. A record
    # carrying both is a retailer contradicting itself about its own shelf, and the same
    # precedence every other retailer gets (`availability_from_flag`: "a retailer that says
    # `available: false` is out of stock whatever its level says") is the safe reading of it.
    # It matters most here: a stated negative is the only route Whole Foods has left to
    # `out_of_stock`, so reading it the other way would let an offer whose own stored wording
    # says `OUT_OF_STOCK` lead a card and enter a basket.
    if stated in _STATED_OUT_OF_STOCK:
        return "stated_negative"
    if stated in _STATED_IN_STOCK or orderable:
        return "positive"
    if not stated and offer is None:
        return "no_offer"
    return "ambiguous"


def index_wwos(payload: list[Any] | None) -> dict[str, Any]:
    """ASIN -> record, for the records a batch actually answered with."""
    if not payload:
        return {}
    return {
        str(record["asin"]).upper(): record
        for record in payload
        if isinstance(record, dict) and record.get("asin")
    }


def resolve_availability(
    listings: list[ProductListing],
    first: dict[str, Any],
    reread: dict[str, Any] | None,
) -> list[ProductListing]:
    """Fold two reads of `/api/wwos/products` into the listings they answered for.

    **Whole Foods publishes a positive and nothing else.** A positive is taken at once, from
    either read; a stated negative would be taken at once too, and in ~400 observations the
    endpoint has never produced one. Everything else -- including a record with no stated
    availability and no offer -- is `unknown`, because that shape is an absent answer.

    A second read therefore exists only to *find* an offer the first read missed, never to
    deny one. Reading the same nothing twice was the bug: measured at store 10717 over 12
    reads of 30 ASINs, 14 ASINs answered with a live offer on some reads and with nothing on
    others (one on 1 read of 12, another on 11 of 12), at the same rate whether the batch
    held 30 ASINs or 1 and whether reads were 1.5s or 15s apart. For an item whose offer
    appears on a fraction `p` of reads, two empty reads "agree" with probability `(1 - p)^2`
    -- 85% at p=0.08 -- so agreement measured the endpoint, not the shelf, and 346 of 869
    Whole Foods offers were `out_of_stock` on the strength of it.
    """
    resolved: list[ProductListing] = []
    for listing in listings:
        sku = listing.retailer_sku.upper()
        record = first.get(sku)
        signal = wwos_signal(record)
        raw = record.get("availability") if isinstance(record, dict) else None
        if signal == "positive":
            resolved.append(
                replace(listing, availability=IN_STOCK, stock_status=f"availability={raw}")
            )
        elif signal == "stated_negative":
            resolved.append(
                replace(listing, availability=OUT_OF_STOCK, stock_status=f"availability={raw}")
            )
        elif signal == "no_offer" and reread is not None:
            resolved.append(_offer_on_reread(listing, reread.get(sku), raw))
        else:
            resolved.append(
                replace(listing, availability=UNKNOWN, stock_status=_unknown_status(signal, raw))
            )
    return resolved


def _offer_on_reread(listing: ProductListing, second: Any, raw: Any) -> ProductListing:
    """The re-read of an ASIN the first read returned no offer for.

    It can say what the first read failed to -- the retailer's own positive, or a negative it
    actually states -- and nothing else. A second silence is still silence.
    """
    signal = wwos_signal(second)
    second_raw = second.get("availability") if isinstance(second, dict) else None
    if signal == "positive":
        # The first read simply had no answer for this item. The second one does. Marked as
        # such: it is the only evidence in the database of what the extra request per 50 buys,
        # and without it nobody can tell later whether to keep paying for it.
        return replace(
            listing, availability=IN_STOCK, stock_status=f"availability={second_raw} on reread"
        )
    if signal == "stated_negative":
        return replace(
            listing, availability=OUT_OF_STOCK, stock_status=f"availability={second_raw}"
        )
    # Whatever the re-read did say, in preference to the first read's silence: an unmapped
    # word here is the only warning that Whole Foods has started using a vocabulary nobody
    # has read yet, and `_STATED_*` is what would need extending.
    if second_raw is not None:
        return replace(listing, availability=UNKNOWN, stock_status=f"availability={second_raw}")
    return replace(listing, availability=UNKNOWN, stock_status=_unknown_status("no_offer", raw))


def _unknown_status(signal: Signal, raw: Any) -> str | None:
    if signal == "absent":
        return None
    if signal == "no_offer":
        return f"availability={raw} no offer"
    return f"availability={raw}"


def parse_product_page(html: str) -> dict[str, Any] | None:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    data = json.loads(match.group(1))
    aapi = data.get("props", {}).get("pageProps", {}).get("aapiData")
    if not aapi:
        return None
    offer = (aapi.get("offerDetails") or {}).get("price") or {}
    unit = (aapi.get("offerDetails") or {}).get("unitPrice") or {}
    return {
        "asin": aapi.get("asin"),
        "name": aapi.get("name"),
        "brand": aapi.get("brandName"),
        "availability": aapi.get("availability"),
        "price": offer.get("priceAmount"),
        "regular_price": offer.get("basisPriceAmount") or offer.get("priceAmount"),
        "unit_price": unit.get("priceAmount"),
        "unit_price_unit": unit.get("baseUnit"),
        "images": aapi.get("productImages") or [],
    }


def parse_store_details(html: str) -> StoreDetails | None:
    """Name, address, timezone and opening hours from a store page.

    The page publishes `operationalDailyHours` as absolute UTC windows per date. A shopper
    reads a wall clock, so each window is converted into the store's own timezone -- which
    also settles DST without a rule of our own -- and recorded both against its date and as
    that weekday's usual pattern. The dates cover about a week ahead and are what a holiday
    is published as; the weekly pattern is what remains true after they run out.
    """
    match = _STORE_STATE_RE.search(html)
    if not match:
        return None
    try:
        location = (json.loads(match.group(1)) or {}).get("location") or {}
    except ValueError:
        return None
    store_code = str(location.get("storeCode") or "").strip()
    if not store_code:
        return None
    address = location.get("address") or {}
    geocode = location.get("geocode") or {}
    lines = address.get("addressLines") or []
    postal = str(address.get("postalCode") or address.get("ZIP_CODE") or "").strip()
    name = str(location.get("locationName") or "").strip()
    return StoreDetails(
        external_id=store_code,
        name=f"Whole Foods {name}" if name else None,
        address_line1=str(lines[0]).strip() if lines else None,
        city=address.get("city"),
        state=address.get("state"),
        zip_code=postal[:5] or None,
        latitude=_float(geocode.get("latitude")),
        longitude=_float(geocode.get("longitude")),
        hours=_hours_from_daily(location.get("operationalDailyHours"), _timezone(location)),
        source=STORE_PAGE_SOURCE,
    )


def _timezone(location: dict[str, Any]) -> str | None:
    for facet in location.get("locationFacets") or []:
        zone = (facet or {}).get("timeZone")
        if zone:
            return str(zone)
    return None


def _hours_from_daily(daily: Any, timezone: str | None) -> StoreHours | None:
    """UTC windows -> local wall clock, per date and per weekday.

    Whole Foods publishes absolute instants, so each window is converted into the store's own
    timezone -- which settles DST without a rule of our own -- and recorded against its date.
    Turning those dates into a standing weekly pattern is the shared, careful step in
    `normalize/hours.py`: the page publishes about a week, so every weekday appears once, and
    promoting each of them would make one holiday closure that weekday's usual hours.
    """
    if not isinstance(daily, list) or not timezone:
        return None
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    published: dict[date, DayHours] = {}
    for entry in daily:
        if not isinstance(entry, dict):
            continue
        read = _published_day(entry, zone)
        if read is not None:
            published[read[0]] = read[1]
    return hours_from_published_days(published, timezone)


def _published_day(entry: dict[str, Any], zone: ZoneInfo) -> tuple[date, DayHours] | None:
    """One published day as (local date, window). A day with no window is a day it is shut."""
    windows = [w for w in (entry.get("operatingHours") or []) if isinstance(w, dict)]
    opens = _instant(windows[0].get("startTime")) if windows else None
    closes = _instant(windows[-1].get("endTime")) if windows else None
    if opens is not None and closes is not None:
        local_open = opens.astimezone(zone)
        return local_open.date(), DayHours(
            local_open.strftime("%H:%M"), closes.astimezone(zone).strftime("%H:%M")
        )
    if windows:
        return None  # a window we could not read says nothing either way
    marker = _instant(entry.get("date"))
    return (marker.astimezone(zone).date(), DayHours(None, None)) if marker else None


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _float(raw: Any) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
