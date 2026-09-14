"""Walmart, read through the browser layer, and honest about what it does not say.

Like Target, Walmart answers an ordinary HTTP client with a PerimeterX interstitial, so it is
reached through `app/retailers/browser.py` -- preferably attached to a Chrome that is already
running, whose session a person has verified once by hand.

**Only robots-allowed pages.** `www.walmart.com/robots.txt` disallows `/search` and `/api/`,
so nothing here searches; categories are browsed through `/cp/<slug>/<id>` pages, which are
not disallowed, and product pages live under `/ip/`, which is not either. The category paths
are vendored in `categories.json`, taken from Walmart's own published category sitemap.

Everything read comes from `__NEXT_DATA__`, the JSON Walmart's page renders itself from --
its own first-party data, not scraped text.

**What its availability really means, which is less than it looks.** A browse page reports
`availabilityStatus: IN_STOCK` for very nearly everything on it, for a simple reason: it does
not list what it has not got. Out-of-stock items are absent rather than marked, so "it is on
the shelf page" and "it is in stock" are close to the same statement, and reading that field
alone would be the exact mistake of treating a search result as stock.

Two things rescue it. `fulfillmentSummary[].storeId` ties the row to a *particular* store, so
this is that store's answer and not a national one. And `canAddToCart` genuinely varies --
across a captured shelf of 41 it was false for five, every one of them a marketplace listing
(powdered egg tins, pickled quail eggs) that the store cannot actually sell you. So an offer
is `in_stock` only when the status, the cart flag and the store all agree, and `unknown`
whenever they do not.

**This adapter observes no out-of-stock.** It cannot: those rows are missing, not marked. An
item that sells out simply stops appearing, and the scrape's own expiry -- which deletes what
a run did not confirm -- is what removes the offer. The product page *does* carry
`availabilityStatus`, but in the one captured case its only fulfilment option was shipping,
so it reported whether Walmart would post the item rather than whether the shop had it; that
is why nothing here reads a product page for stock.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.normalize.availability import IN_STOCK, LIVE_STOCK, UNKNOWN, Availability, StockReporting
from app.normalize.pricing import PACKAGE, PER_OUNCE, PER_POUND, PriceBasis
from app.retailers.base import ProductListing, StoreLocation, gather_offers
from app.retailers.browser import BrowserSession
from app.retailers.clients import RetailerClients
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url

log = logging.getLogger("storesplit.retailers.walmart")

SITE_URL = "https://www.walmart.com"
SLUG = "walmart"
CATEGORIES_PATH = Path(__file__).with_name("categories.json")
SEARCH_SOURCE = "walmart:next_data/searchResult"
_CENT = Decimal("0.01")
# A Walmart `UNIT_PRICE` line reads "$2.59/lb" or "12.4 \u00a2/oz". Two things have to be
# read out of it, and the old rule read neither: *which* unit it is quoted in, and whether
# the amount in it is the same one being charged. A fixed package carries this line too --
# a 32 oz jar at $8.00 shows "25.0 \u00a2/oz" beside it -- so its presence says nothing
# about the basis, and treating it as a per-pound flag published $8.00 a pound for a jar.
_UNIT_PRICE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?:\u00a2|c)?\s*/\s*(?P<unit>lb|pound|oz|ounce)\b",
    re.IGNORECASE,
)
_UNIT_BASIS: dict[str, PriceBasis] = {
    "lb": PER_POUND,
    "pound": PER_POUND,
    "oz": PER_OUNCE,
    "ounce": PER_OUNCE,
}


def categories() -> dict[str, str]:
    return json.loads(CATEGORIES_PATH.read_text())


class WalmartAdapter:
    slug = SLUG
    name = "Walmart"
    # Unregistered: its browse pages omit what they lack rather than marking it.
    stock_reporting: StockReporting = LIVE_STOCK
    site_url = SITE_URL

    def __init__(self, clients: RetailerClients) -> None:
        self._browser: BrowserSession = clients.browser()

    def is_configured(self) -> bool:
        return self._browser.is_configured()

    def unconfigured_reason(self) -> str:
        return (
            "the browser fallback is off; set BROWSER_FALLBACK_ENABLED=true and "
            "uv sync --extra browser, then verify the session once with "
            "scripts/probe_browser_retailer.py --wait"
        )

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        """The store this browser session is shopping, named by its own pages.

        Walmart picks a store from the session, so there is one per profile. Rather than
        infer it, this reads the store id the category pages attribute their fulfilment to.
        """
        path = next(iter(categories().values()))
        page = await self._browser.visit(SLUG, f"{SITE_URL}{path}", settle_ms=8000)
        payload = await _next_data(page)
        store_id = store_id_from_browse(payload)
        if store_id is None:
            log.warning("walmart_store_unknown", extra={"zip_code": zip_code})
            return []
        return [
            StoreLocation(
                external_id=store_id,
                name=f"Walmart {store_id}",
                zip_code=zip_code[:5],
            )
        ]

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        path = categories().get(query)
        if path is None:
            return []
        page = await self._browser.visit(SLUG, f"{SITE_URL}{path}", settle_ms=9000)
        payload = await _next_data(page)
        return parse_browse(payload, store.external_id, SEARCH_SOURCE)

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        for query in categories():
            for listing in await self.search_products(query, store):
                if listing.retailer_sku == retailer_sku:
                    return listing
        return None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)


async def _next_data(page: Any) -> dict[str, Any]:
    raw = await page.evaluate(
        "() => {const e = document.querySelector('#__NEXT_DATA__');"
        " return e ? e.textContent : null}"
    )
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


# --------------------------------------------------------------------------- parsing


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stacks = (
        (((payload.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get(
            "searchResult"
        )
        or {}
    ).get("itemStacks") or []
    return [item for stack in stacks for item in (stack.get("items") or [])]


# Marketplace rows carry `storeId: "0"` -- delivery by a seller, not a shop that has it.
_NOT_A_STORE = frozenset({"", "0", "None", "null"})


def real_store_ids(item: dict[str, Any]) -> set[str]:
    """The actual shops a row is fulfilled from, ignoring the marketplace placeholder."""
    return {
        str(summary.get("storeId"))
        for summary in item.get("fulfillmentSummary") or []
        if str(summary.get("storeId") or "") not in _NOT_A_STORE
    }


def store_id_from_browse(payload: dict[str, Any]) -> str | None:
    """The store the page attributed its fulfilment to, if the real rows agree on one."""
    found: set[str] = set()
    for item in _items(payload):
        found |= real_store_ids(item)
    return found.pop() if len(found) == 1 else None


def parse_browse(
    payload: dict[str, Any], store_external_id: str, source: str
) -> list[ProductListing]:
    listings: dict[str, ProductListing] = {}
    for item in _items(payload):
        listing = _listing(item, store_external_id, source)
        if listing is not None and listing.retailer_sku not in listings:
            listings[listing.retailer_sku] = listing
    return list(listings.values())


def _listing(item: dict[str, Any], store_external_id: str, source: str) -> ProductListing | None:
    sku = item.get("usItemId")
    title = item.get("name")
    price = _current_price(item)
    if not sku or not title or price is None:
        return None
    stock_status, availability = browse_availability(item, store_external_id)
    unit_price = _unit_price_text(item)
    price_basis = _price_basis(price, unit_price)
    return ProductListing(
        retailer_sku=str(sku),
        title=str(title),
        store_external_id=store_external_id,
        price=price,
        regular_price=price,
        loyalty_price=None,
        brand=str(item["brand"]).strip() if item.get("brand") else None,
        # Walmart's own link for the row; the tracking query is dropped, the path is theirs.
        product_url=_canonical(item.get("canonicalUrl")),
        image_url=listing_image_url(item.get("image")),
        gtin=None,  # the browse row carries no UPC; the product page does
        size_text=None,
        price_basis=price_basis,
        availability=availability,
        stock_status=stock_status,
        source=source,
        attributes={"unit_price": unit_price or ""},
    )


def browse_availability(
    item: dict[str, Any], store_external_id: str
) -> tuple[str | None, Availability]:
    """`in_stock` only where the status, the cart flag and the store all agree.

    None of the three is trustworthy alone. The status is near-constant because a browse page
    omits what it has not got; the cart flag is about whether this store can sell it, and is
    false for marketplace listings that are nonetheless "in stock" somewhere; the store id
    says whose answer this even is. Together they are a reasonable claim. Apart, they are not,
    so anything short of agreement is `unknown` -- never `out_of_stock`, because a browse page
    does not report absence, it just omits it.
    """
    status = item.get("availabilityStatusV2") or {}
    value = str(status.get("value") or "").strip().upper()
    display = status.get("display")
    raw = str(display) if display else (value or None)

    stores = real_store_ids(item)
    if stores and str(store_external_id) not in stores:
        return raw, UNKNOWN  # another store's answer is not this store's
    if value != "IN_STOCK":
        return raw, UNKNOWN
    if item.get("canAddToCart") is not True:
        # Listed and priced, but this store cannot sell it -- a marketplace row.
        return f"{raw} (not sellable here)" if raw else "not sellable here", UNKNOWN
    return raw, IN_STOCK


def _current_price(item: dict[str, Any]) -> Decimal | None:
    lines = ((item.get("priceInfo") or {}).get("priceDetails") or {}).get("priceLines") or []
    for line in lines:
        if line.get("lineType") != "CURRENT_PRICE":
            continue
        for value in line.get("values") or []:
            money = _money(value.get("value"))
            if money is not None:
                return money
    return None


def _price_basis(price: Decimal, unit_price: str | None) -> PriceBasis:
    """Whether the amount being charged *is* the unit price, and in which unit.

    Walmart does not state a basis, so this reads the one thing that distinguishes a rate
    from a total: for a weighed item the shelf price and the unit price are the same number
    (a tray at "$2.59" priced "$2.59/lb"), while for a package they differ by the package
    size. A `UNIT_PRICE` line on its own proves nothing -- every fixed package has one.
    """
    match = _UNIT_PRICE.search(unit_price or "")
    if match is None:
        return PACKAGE
    try:
        amount = Decimal(match.group("amount"))
    except ArithmeticError:
        return PACKAGE
    if "\u00a2" in (unit_price or "") or "¢" in (unit_price or ""):
        amount /= 100  # a cents-per-ounce line, e.g. "25.0 ¢/oz"
    if amount.quantize(_CENT) != price.quantize(_CENT):
        return PACKAGE
    return _UNIT_BASIS.get(match.group("unit").lower(), PACKAGE)


def _unit_price_text(item: dict[str, Any]) -> str | None:
    lines = ((item.get("priceInfo") or {}).get("priceDetails") or {}).get("priceLines") or []
    for line in lines:
        if line.get("lineType") != "UNIT_PRICE":
            continue
        for value in line.get("values") or []:
            if value.get("value"):
                return str(value["value"])
    return None


def _canonical(raw: object) -> str | None:
    """Walmart's own product path, with its tracking query dropped."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = urlsplit(raw.strip())
    without_query = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return clean_product_url(without_query, base_url=SITE_URL)


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return amount.quantize(_CENT, ROUND_HALF_UP) if amount > 0 else None
