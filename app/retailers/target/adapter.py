"""Target, read through the browser layer because nothing else reaches it.

Target answers an ordinary HTTP client with nothing useful: `redsky.target.com` disallows
everything in its own `robots.txt`, and the storefront sits behind PerimeterX, which serves a
CAPTCHA block to any client it does not like -- including, often, a real browser on its first
visit. So this adapter is the one place in the repo that needs
`app/retailers/browser.py`: a persistent, headed Chrome the person running the scrape has
verified by hand at least once, whose profile then carries that verification forward.

**Only pages `www.target.com/robots.txt` leaves open are visited.** Keyword search is
disallowed there (`/s?`, `/shop/`, `/pl/`), so a category is not searched for -- it is
browsed, through the category pages under `/c/`, which are not disallowed. The mapping from
StoreSplit's staples to Target's own category paths is vendored in `categories.json` and was
read out of Target's published taxonomy sitemap, not guessed. Product pages under `/p/` are
open too, but nothing here needs one: a category page carries the whole shelf.

Nothing is scraped out of rendered HTML. The page fetches its own data to render itself, and
this reads those payloads:

  * `plp_search_v2`                      the shelf: tcin, title, brand, price, unit price,
                                         and the page's own `buy_url` for each product
  * `product_summary_with_fulfillment_v1`  per-store stock, loaded as the shelf scrolls
  * `store_location_v1`                  which store this session is actually shopping

**Availability comes from the store, never from shipping.** The fulfillment payload carries
both, and they disagree constantly: an item can be `OUT_OF_STOCK` for delivery while the
shop down the road has ten on the shelf. StoreSplit compares shelf prices, so only
`store_options[]` for *this* store counts, and `shipping_options` is ignored -- reading it
would be the same mistake as reading Lucky's stale `stockLevel`.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.normalize.availability import (
    IN_STOCK,
    LIVE_STOCK,
    OUT_OF_STOCK,
    UNKNOWN,
    Availability,
    StockReporting,
)
from app.normalize.hours import (
    DayHours,
    StoreHours,
    hours_from_published_days,
    parse_wall_clock,
)
from app.normalize.pricing import PACKAGE, PER_OUNCE, PER_POUND, PriceBasis
from app.normalize.units import QuantityRange, parse_quantity_range
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.browser import BrowserSession
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url
from app.retailers.zipmatch import distance_miles, zip_centroid

log = logging.getLogger("storesplit.retailers.target")

SITE_URL = "https://www.target.com"
SLUG = "target"
CATEGORIES_PATH = Path(__file__).with_name("categories.json")
SEARCH_SOURCE = "target:redsky/plp_search_v2"
_CENT = Decimal("0.01")
# How far a Target store may be from the ZIP's centroid and still be treated as serving it.
# The browser session shops one store at a time -- whichever a human last chose in it -- so
# this is a sanity check, not a locator: a store on the wrong side of the bay is not an
# answer to "what does this ZIP pay", and silently pricing it would be worse than no offer.
MAX_STORE_MILES = 25.0


def categories() -> dict[str, str]:
    """Each supported staple's path on Target, read from the vendored catalogue."""
    return json.loads(CATEGORIES_PATH.read_text())


def _redsky(url: str) -> bool:
    return "redsky.target.com" in url


class TargetAdapter:
    slug = SLUG
    name = "Target"
    # Per-store stock that varies the way real stock does (milk: 26 in / 3 out / 1 unknown).
    stock_reporting: StockReporting = LIVE_STOCK
    site_url = SITE_URL
    # One browser page, so one search at a time -- and said out loud rather than left for the
    # scrape service to discover by piling seven tasks onto a lock. They would take turns
    # regardless; the difference is that six of them no longer spend the retailer's deadline
    # queueing, and none is cancelled with a page load half-done underneath the others.
    max_concurrent_searches = 1

    def __init__(self, clients: RetailerClients) -> None:
        # The browser is the pool's, not this adapter's: it is opened once per process and
        # closed with everything else, so the verified profile is shared rather than rebuilt.
        self._browser: BrowserSession = clients.browser()
        # The shelf needs a browser; the store page does not. `/sl/` is outside the
        # challenge and inside robots.txt, so reading it with the shared HTTP client keeps
        # a weekly store-details fetch from costing a page load in the verified session.
        self._client = clients.shared()

    def is_configured(self) -> bool:
        """Configured only when the browser fallback is switched on and installed."""
        return self._browser.is_configured()

    def unconfigured_reason(self) -> str:
        return (
            "the browser fallback is off; set BROWSER_FALLBACK_ENABLED=true and "
            "uv sync --extra browser, then verify the session once with "
            "scripts/probe_browser_retailer.py --wait"
        )

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        """The store this browser session is shopping, if it is near enough to the ZIP.

        Target picks a store from the session, not from a URL, so there is one store per
        profile and it is whichever a person last chose in that window -- or, after the browser
        has restarted, whichever it re-picks from the location it still remembers, because the
        cookie naming the store is session-scoped and does not survive. Rather than pretend
        otherwise, this reads the store the site says it is using and refuses it when it is not
        plausibly serving the ZIP asked for.

        It reads it off a category page, which is also the page a search wants, so this load and
        that search are the same load: the browser layer keeps what a page's data calls returned
        and hands it to the next caller the capture satisfies. Asking for the *first* category
        rather than a page of its own is what makes that overlap available at all.
        """
        page_path = next(iter(categories().values()))
        captured = await self._browser.load_and_read(
            SLUG,
            f"{SITE_URL}{page_path}",
            enough=lambda seen: any("store_location_v1" in url for url, _ in seen),
            settle_ms=8000,
            capture=_redsky,
        )
        store = parse_store(captured)
        if store is None:
            log.warning("target_store_unknown", extra={"zip_code": zip_code})
            return []
        distance = _distance_miles(zip_code, store)
        if distance is not None and distance > MAX_STORE_MILES:
            log.warning(
                "target_store_too_far",
                extra={
                    "zip_code": zip_code,
                    "store": store.external_id,
                    "store_zip": store.zip_code,
                    "miles": round(distance, 1),
                    "hint": "choose a nearer store once in the browser profile",
                },
            )
            return []
        return [store]

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        """One category page, browsed the way a shopper would, then read from its own data."""
        path = categories().get(query)
        if path is None:
            return []
        # Load and read as one operation. Categories are scraped concurrently and a retailer
        # has one browser page, so navigating and then reading separately lets one category
        # steer the page out from under another -- which is exactly how a seven-category
        # Target scrape came back with nothing while a one-category scrape came back full.
        #
        # Stock arrives a screenful at a time, so the shelf is read until every product on it
        # has some, and not one scroll further. Scrolling is the only interaction: one page
        # load per category, no clicks, no re-navigation, no second visit.
        captured = await self._browser.load_and_read(
            SLUG,
            f"{SITE_URL}{path}",
            enough=stock_covers_shelf,
            settle_ms=8000,
            capture=_redsky,
        )
        return parse_category(captured, store.external_id, SEARCH_SOURCE)

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        """Target is browsed by shelf; a single product is found on the shelf it sits on."""
        for query in categories():
            for listing in await self.search_products(query, store):
                if listing.retailer_sku == retailer_sku:
                    return listing
        return None

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """Hours, timezone and Target's own Google listing, from the store's own page.

        `/sl/<slug>/<store_id>` is the one Target surface this needs that a plain HTTPS
        client can read: `robots.txt` leaves it open (only `/store-locator/
        search-results-print` is disallowed) and it is not behind the PerimeterX challenge
        that guards the shelf, so no browser is spent on it. The page server-renders the
        store record it draws itself from, and that record carries what nothing else here
        publishes: `rolling_operating_hours` (fourteen days of local wall clock),
        `geographic_specifications.iso_time_zone_code`, and
        `miscellaneous.google_cid` -- Target's link to its own Google Maps listing, which is
        a place identified by the business itself rather than resolved on its behalf.

        Called at most once a week per store by the scrape service, never on a search.
        """
        url = store.details_url or store_page_url(store.external_id, store.name)
        response = await request_with_retry(
            self._client, "GET", url, headers={"Accept": "text/html"}, max_retries=1
        )
        if response.status_code != 200:
            return None
        details = parse_store_details(response.text)
        if details is None or details.external_id != store.external_id:
            # A wrong or redirected page is another store's opening times. Refusing it is the
            # same rule Whole Foods applies to its own `storeCode`.
            log.warning(
                "target_store_page_mismatch",
                extra={
                    "store": store.external_id,
                    "url": url,
                    "found": details.external_id if details else None,
                },
            )
            return None
        return details


# --------------------------------------------------------------------------- parsing


def stock_covers_shelf(captured: list[tuple[str, Any]]) -> bool:
    """Has every product the shelf listed had its stock loaded yet?

    This is what decides when to stop scrolling. Coverage, not a step count: a short category
    is finished after one screen, and a long one is not read past its end.
    """
    shelf = {
        str(product.get("tcin"))
        for url, body in captured
        if "plp_search_v2" in url
        for product in (((body or {}).get("data") or {}).get("search") or {}).get("products") or []
        if product.get("tcin")
    }
    if not shelf:
        return False  # the shelf itself has not arrived; there is nothing to cover
    return shelf <= set(_fulfillment_by_tcin(captured))


def parse_store(captured: list[tuple[str, Any]]) -> StoreLocation | None:
    """The store the session is shopping, from the page's own `store_location_v1` call."""
    for url, body in captured:
        if "store_location_v1" not in url:
            continue
        store = ((body or {}).get("data") or {}).get("store") or {}
        store_id = store.get("store_id")
        if not store_id:
            continue
        address = store.get("mailing_address") or {}
        return StoreLocation(
            external_id=str(store_id),
            name=str(store.get("location_name") or f"Target {store_id}"),
            address_line1=address.get("address_line1"),
            city=address.get("city"),
            state=address.get("region"),
            zip_code=(str(address.get("postal_code") or "") or None or "")[:5] or None,
            latitude=_float(address.get("latitude")),
            longitude=_float(address.get("longitude")),
            details_url=store_page_url(store_id, store.get("location_name")),
        )
    return None


def store_page_url(store_id: Any, location_name: Any) -> str:
    """`/sl/<slug>/<store_id>` -- Target's own store page, which robots.txt leaves open.

    The slug is cosmetic: Target serves the page from the id and redirects a wrong slug to
    the right one, which is why it can be built here without guessing at anything that
    matters. Only the id identifies the store, and the page is refused later unless its own
    `store_id` matches.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(location_name or "store").lower()).strip("-")
    return f"{SITE_URL}/sl/{slug or 'store'}/{store_id}"


def parse_category(
    captured: list[tuple[str, Any]], store_external_id: str, source: str
) -> list[ProductListing]:
    """The shelf and its stock, joined on tcin.

    The two calls arrive separately -- the shelf once, stock a screenful at a time -- so
    stock is collected from every fulfillment payload the page made before joining.

    Every listing carries the store the page said it was shopping, read from the same capture
    (`store_location_v1`), as its `store_context`. Target picks a store from the session rather
    than from the URL, and the cookie naming it does not survive a browser restart -- so the
    session can be shopping a different store than the one this search was asked about, and
    `ingest_listing` is the thing that already refuses a price whose echo names another store.
    Without the echo that guard has nothing to compare, and a real price is attached to the
    wrong shelf: worse than no price, because it looks right.
    """
    fulfillment = _fulfillment_by_tcin(captured)
    answered_for = parse_store(captured)
    listings: dict[str, ProductListing] = {}
    for url, body in captured:
        if "plp_search_v2" not in url:
            continue
        for product in (((body or {}).get("data") or {}).get("search") or {}).get("products") or []:
            listing = _listing(
                product,
                fulfillment,
                store_external_id,
                source,
                answered_for.external_id if answered_for is not None else None,
            )
            if listing is not None and listing.retailer_sku not in listings:
                listings[listing.retailer_sku] = listing
    return list(listings.values())


def _fulfillment_by_tcin(captured: list[tuple[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for url, body in captured:
        if "product_summary_with_fulfillment" not in url:
            continue
        for summary in ((body or {}).get("data") or {}).get("product_summaries") or []:
            tcin = summary.get("tcin")
            block = summary.get("fulfillment")
            if tcin and isinstance(block, dict):
                found[str(tcin)] = block
    return found


def _listing(
    product: dict[str, Any],
    fulfillment: dict[str, dict[str, Any]],
    store_external_id: str,
    source: str,
    answered_for: str | None = None,
) -> ProductListing | None:
    tcin = product.get("tcin")
    item = product.get("item") or {}
    description = item.get("product_description") or {}
    # Target's titles arrive HTML-escaped ("Eggland&#39;s Best"); stored raw they would show
    # the entity to the shopper and break brand matching.
    title = html.unescape(str(description.get("title") or "")).strip()
    price = product.get("price") or {}
    current = _money(price.get("current_retail"))
    if not tcin or not title or current is None:
        return None
    regular = _money(price.get("reg_retail")) or current
    brand = (item.get("primary_brand") or {}).get("name")
    enrichment = item.get("enrichment") or {}
    # `buy_url` is Target's own link for the product, so no URL is invented here.
    product_url = clean_product_url(enrichment.get("buy_url"), base_url=SITE_URL)
    image = enrichment.get("image_info")
    stock_status, availability = store_availability(fulfillment.get(str(tcin)), store_external_id)
    semantics = price_semantics(price, description, title)
    return ProductListing(
        retailer_sku=str(tcin),
        title=title,
        store_external_id=store_external_id,
        price=current,
        regular_price=max(regular, current),
        loyalty_price=None,
        brand=str(brand).strip() if brand else None,
        product_url=product_url,
        image_url=listing_image_url(image),
        gtin=None,  # the shelf payload carries a DPCI, not a UPC
        size_text=semantics.size_text,
        price_basis=semantics.basis,
        weight_range=semantics.weight_range,
        max_total_price=semantics.max_total_price,
        availability=availability,
        stock_status=stock_status,
        source=source,
        store_context=answered_for,
        attributes={"dpci": str(item.get("dpci") or "")},
    )


def store_availability(
    fulfillment: dict[str, Any] | None, store_external_id: str
) -> tuple[str | None, Availability]:
    """Stock at *this* store, from Target's own fulfillment block.

    Deliberately blind to `shipping_options`. That field says whether Target will post the
    item, and it is not a stock signal at all: across a captured shelf it read `OUT_OF_STOCK`
    for every product, including ones with ten on the shelf at the store being priced.
    Reading it would repeat the Lucky mistake in a new payload.

    Two store-level fields are used, and they must agree. `in_store_only` is the shelf --
    preferred over `order_pickup`, which reports `UNAVAILABLE` for items that are on the
    shelf but not pickable -- and `location_available_to_promise_quantity` is the count
    behind it. Observed together they track exactly (`IN_STOCK` at 9-10, `LIMITED_STOCK` at
    1, `OUT_OF_STOCK` and `NOT_SOLD_IN_STORE` at 0), so where they *disagree* something is
    stale and the honest answer is `unknown` rather than a guess in either direction.

    A product the page never loaded stock for is `unknown`: absence is not presence.
    """
    if not isinstance(fulfillment, dict):
        return None, UNKNOWN
    if fulfillment.get("sold_out") is True:
        return "sold_out", OUT_OF_STOCK
    for option in fulfillment.get("store_options") or []:
        if str(option.get("location_id") or "") != str(store_external_id):
            continue
        quantity = option.get("location_available_to_promise_quantity")
        counted = float(quantity) if isinstance(quantity, int | float) else None
        status = None
        for key in ("in_store_only", "order_pickup"):
            candidate = (option.get(key) or {}).get("availability_status")
            if isinstance(candidate, str) and candidate.strip():
                status = candidate.strip()
                break
        if status is None:
            if counted is None:
                return None, UNKNOWN
            return f"quantity={counted:g}", IN_STOCK if counted > 0 else OUT_OF_STOCK
        state = _STORE_STATUS.get(status.upper(), UNKNOWN)
        raw = status if counted is None else f"{status} (quantity={counted:g})"
        if state == IN_STOCK and counted is not None and counted <= 0:
            # The word says buyable and the count says none left. Do not pick a winner.
            return raw, UNKNOWN
        return raw, state
    return None, UNKNOWN


_STORE_STATUS: dict[str, Availability] = {
    "IN_STOCK": IN_STOCK,
    "LIMITED_STOCK": IN_STOCK,
    "OUT_OF_STOCK": OUT_OF_STOCK,
    "UNAVAILABLE": OUT_OF_STOCK,
    "NOT_SOLD_IN_STORE": OUT_OF_STOCK,
}
# `formatted_unit_price_suffix` as Target writes it: "/lb", "/ounce", "/count".
_SUFFIX_UNITS: dict[str, PriceBasis] = {
    "lb": PER_POUND,
    "lbs": PER_POUND,
    "pound": PER_POUND,
    "pounds": PER_POUND,
    "oz": PER_OUNCE,
    "ounce": PER_OUNCE,
    "ounces": PER_OUNCE,
}
_UNIT_SUFFIX = re.compile(r"^\s*/\s*([a-z]+)", re.IGNORECASE)
# Target puts the basis in the product's own name: "... - 2.5-5.25lbs - price per lb".
_TITLE_BASIS = re.compile(r"\bpriced?\s+per\s+(lb|lbs|pound|pounds|oz|ounce|ounces)\b", re.I)
# "$12.95 max price" -- the headline is a ceiling, so `current_retail` beneath it is a rate.
_MAX_PRICE = re.compile(r"\bmax(?:imum)?\s+price\b", re.IGNORECASE)
_PACKAGE_QUANTITY = re.compile(r"Package Quantity:</B>\s*([\d.]+)")


@dataclass(frozen=True)
class PriceSemantics:
    """What Target's `price` block means, read once rather than re-inferred downstream."""

    basis: PriceBasis
    size_text: str | None
    weight_range: QuantityRange | None
    max_total_price: Decimal | None


def price_semantics(
    price: dict[str, Any], description: dict[str, Any], title: str
) -> PriceSemantics:
    """Decide whether `current_retail` is a package total or a rate, and read what surrounds it.

    Target states this three ways for a variable-weight item, and they agree. For TCIN
    86676070 at store 3264 the payload is::

        "formatted_current_price": "$12.95", "formatted_current_price_suffix": "max price",
        "formatted_max_item_price": "$12.95",
        "formatted_unit_price": "$2.59",     "formatted_unit_price_suffix": "/lb",
        "current_retail": 2.59,              "reg_retail": 2.59

    so `current_retail` is **$2.59 per pound**, not $2.59 for the tray and not $12.95. Any of
    the three is enough on its own: the title's own "price per lb", a headline that admits it
    is only a ceiling, or a unit price that is the same number as `current_retail` under a
    weight suffix. A fixed package disagrees on the last of these -- the eggs on the same
    shelf read `current_retail: 5.89` against `"$0.49" "/count"` -- which is what keeps this
    from reading every product as a rate.

    `formatted_max_item_price` is copied through, never derived. Target's own ceiling for that
    2.5-5.25 lb tray is $12.95, which is $2.59 x 5.00; multiplying by the 5.25 lb the title
    publishes would print $13.60 with exactly as much apparent authority and be wrong.
    """
    unit_suffix = str(price.get("formatted_unit_price_suffix") or "")
    suffix_match = _UNIT_SUFFIX.match(unit_suffix)
    suffix_basis = _SUFFIX_UNITS.get(suffix_match.group(1).lower()) if suffix_match else None

    title_match = _TITLE_BASIS.search(title)
    title_basis = _SUFFIX_UNITS.get(title_match.group(1).lower()) if title_match else None

    headline_suffix = str(price.get("formatted_current_price_suffix") or "")
    headline_is_a_ceiling = bool(_MAX_PRICE.search(headline_suffix))
    unit_price = _money(price.get("formatted_unit_price"))
    current = _money(price.get("current_retail"))
    rate_and_amount_agree = unit_price is not None and current is not None and unit_price == current

    basis: PriceBasis = PACKAGE
    if title_basis is not None:
        basis = title_basis
    elif suffix_basis is not None and (headline_is_a_ceiling or rate_and_amount_agree):
        basis = suffix_basis

    if basis == PACKAGE:
        for bullet in description.get("bullet_descriptions") or []:
            match = _PACKAGE_QUANTITY.search(str(bullet))
            if match:
                return PriceSemantics(PACKAGE, f"{match.group(1)} ct", None, None)
        return PriceSemantics(PACKAGE, None, None, None)

    # A rate: the package has no single size, so it is given none rather than the upper end
    # of its range, which is the number that would be divided into the rate.
    maximum = _money(price.get("formatted_max_item_price"))
    if maximum is None and headline_is_a_ceiling:
        maximum = _money(price.get("formatted_current_price"))
    # The range is read in the unit the price is quoted in: a title that names a portion
    # size before the pack weight ("4-6 oz Portions, 2.5-5.25 lbs") would otherwise
    # publish the portion as the pack's weight range.
    return PriceSemantics(basis, None, parse_quantity_range(title, basis), maximum)


def _money(value: Any) -> Decimal | None:
    """A number or one of Target's formatted strings ("$12.95") as money, else None.

    The formatted fields are read as well as the raw ones because they are the only place
    some facts are stated: `formatted_max_item_price` has no numeric twin in the payload.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lstrip("$").replace(",", "")
    try:
        amount = Decimal(text)
    except (ArithmeticError, ValueError):
        return None
    # `json.loads` accepts bare `NaN` and `Infinity`, and comparing or quantizing either
    # raises -- outside the block above, where it would take down the whole shelf parse
    # rather than the one listing that carried it.
    if not amount.is_finite():
        return None
    return amount.quantize(_CENT, ROUND_HALF_UP) if amount > 0 else None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _distance_miles(zip_code: str, store: StoreLocation) -> float | None:
    centroid = zip_centroid(zip_code)
    if centroid is None or store.latitude is None or store.longitude is None:
        return None
    return distance_miles(centroid[0], centroid[1], store.latitude, store.longitude)


# ------------------------------------------------------------------- the store's own page

STORE_PAGE_SOURCE = "target:sl-page/store"
# The page's data is a JSON string inside its own script payload, so every quote inside it is
# escaped. Rather than guess how many times, unescape until the key is plainly readable.
_STORE_ID_RE = re.compile(r'"store_id"\s*:\s*"')
_MAX_UNESCAPE_PASSES = 4


def parse_store_details(html: str) -> StoreDetails | None:
    """Address, coordinates, timezone, opening hours and the Google listing Target published.

    The store record is server-rendered into the page as an escaped JSON string; it is found
    by its own `store_id` key and read out by balancing braces, which is cheaper and far less
    brittle than trying to recover the whole framework payload around it.
    """
    record = _store_record(html)
    if record is None:
        return None
    store_id = str(record.get("store_id") or "").strip()
    if not store_id:
        return None
    address = record.get("mailing_address") or {}
    geographic = record.get("geographic_specifications") or {}
    timezone = str(geographic.get("iso_time_zone_code") or "").strip() or None
    name = str(record.get("location_name") or "").strip()
    return StoreDetails(
        external_id=store_id,
        name=f"Target {name}" if name else None,
        address_line1=address.get("address_line1"),
        city=address.get("city"),
        state=address.get("region"),
        zip_code=(str(address.get("postal_code") or "")[:5] or None),
        latitude=_float(geographic.get("latitude")),
        longitude=_float(geographic.get("longitude")),
        hours=_hours_from_rolling(record.get("rolling_operating_hours"), timezone),
        maps_place_url=str((record.get("miscellaneous") or {}).get("google_cid") or "") or None,
        source=STORE_PAGE_SOURCE,
    )


def _store_record(html: str) -> dict[str, Any] | None:
    """The full store record, out of the several objects on the page that name a store.

    The page mentions its store more than once -- a breadcrumb, a header, a picker -- and all
    but one of those are a bare `{store_id, location_name, state}`. So every candidate is
    read and the richest is taken: the one that actually carries the address block, which is
    the record the page renders its details from. Taking the first match instead is how this
    silently returned a store with no hours, no coordinates and no Maps link.
    """
    text = html
    for _ in range(_MAX_UNESCAPE_PASSES):
        if _STORE_ID_RE.search(text):
            break
        text = text.replace('\\"', '"').replace("\\\\", "\\")
    best: dict[str, Any] | None = None
    for match in _STORE_ID_RE.finditer(text):
        start = text.rfind("{", 0, match.start())
        end = _matching_brace(text, start) if start >= 0 else -1
        if end <= 0:
            continue
        try:
            record = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(record, dict) and (best is None or _richness(record) > _richness(best)):
            best = record
    return best


_DETAIL_KEYS = (
    "mailing_address",
    "geographic_specifications",
    "rolling_operating_hours",
    "miscellaneous",
)


def _richness(record: dict[str, Any]) -> int:
    """How much of a store record this object actually is."""
    return sum(1 for key in _DETAIL_KEYS if isinstance(record.get(key), dict))


def _matching_brace(text: str, start: int) -> int:
    """Index just past the `}` that closes the `{` at `start`, or -1. String-aware."""
    depth = 0
    index = start
    in_string = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            in_string = not in_string
        elif not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    return -1


def _hours_from_rolling(rolling: Any, timezone: str | None) -> StoreHours | None:
    """`rolling_operating_hours.main_hours.days` -> a schedule.

    Target publishes fourteen dated days of local wall clock for the *store* and separate
    `capability_hours` for each counter inside it (Starbucks, the pharmacy). Only
    `main_hours` is read: a shopper comparing grocery prices is asking when the shop is open,
    and a cafe that shuts two hours earlier is not that answer.

    Fourteen days means every weekday appears twice, so the weekly pattern is generalised the
    careful way (`hours_from_published_days`) and a one-off holiday stays a dated exception.
    """
    if not isinstance(rolling, dict):
        return None
    days = ((rolling.get("main_hours") or {}).get("days")) or []
    if not isinstance(days, list):
        return None
    published: dict[date, DayHours] = {}
    for entry in days:
        if not isinstance(entry, dict):
            continue
        read = _published_day(entry)
        if read is not None:
            published[read[0]] = read[1]
    return hours_from_published_days(published, timezone)


def _published_day(entry: dict[str, Any]) -> tuple[date, DayHours] | None:
    """One dated day as (date, window). `is_open: false` is a day the store does not open."""
    try:
        day = date.fromisoformat(str(entry.get("date") or ""))
    except ValueError:
        return None
    windows = [w for w in (entry.get("hours") or []) if isinstance(w, dict)]
    if entry.get("is_open") is False or not windows:
        return day, DayHours(None, None)
    opens = _wall_clock(windows[0].get("begin_time"))
    closes = _wall_clock(windows[-1].get("end_time"))
    if opens is None or closes is None:
        return day, DayHours(None, None)
    return day, DayHours(opens, closes)


def _wall_clock(raw: Any) -> str | None:
    """Target writes "08:00:00"; the schedule holds "HH:MM"."""
    parsed = parse_wall_clock(str(raw or "")[:5])
    return parsed.strftime("%H:%M") if parsed else None
