"""Safeway (Albertsons) adapter.

www.safeway.com allows everything but account pages in robots.txt, and publishes its product
sitemaps for crawlers. Two of its own JSON gateways answer plain HTTPS with the subscription
key its web bundle ships as a public constant (the same kind of constant as the Trader Joe's
locator app key):
  * GET /abs/pub/xapi/storeresolver/all?zipcode=<zip>
        stores serving a ZIP, nearest first, with address and distance.
  * GET /abs/pub/xapi/v1/aisles/similar-products?storeid=<id>&bpn=<seed>&includeOffer=true
        the shelf neighbours of a seed product, priced for that store. This is the only
        store-parameterised product listing Safeway still answers: keyword search
        (`pgmsearch/v1/search/products`), the aisle listing (`v1/aisles/products`) and the
        `/shop/search-results.html` and `/shop/aisles/*.html` pages are all null-routed by
        Imperva, in a real browser as well as over plain HTTP.
  * GET /shop/pd/-/<pid>
        the product page, which redirects to the canonical slug and embeds the same product
        record in its Next.js RSC payload. Used for fetch_product of an unseen id; its price
        is the store Safeway's edge resolves, so store-specific offers come from the seed
        endpoint instead.

Because search is unavailable, `search_products` works from vendored seed products
(`seeds.json`, refreshed by scripts/discover_safeway_seeds.py): each category has a handful of
product ids whose shelf is that category's shelf, and the adapter unions their shelf
neighbours, keeping only the docs still on the seed's shelf. Prices are the store's shelf
prices; no loyalty (Just for U) pricing is exposed anonymously.
"""

import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache, partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.concurrency import fanout_limit, gather_bounded
from app.normalize.availability import LIVE_STOCK, UNKNOWN, StockReporting, normalize_availability
from app.normalize.hours import DayHours, StoreHours, hours_from_weekly, parse_wall_clock
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation, gather_offers
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.urls import clean_product_url

log = logging.getLogger("storesplit.retailers.safeway")

SITE_URL = "https://www.safeway.com"
XAPI_URL = f"{SITE_URL}/abs/pub/xapi"
# Safeway's per-store pages. `robots.txt` there allows them, and the resolver links to
# each one by name, so nothing about this host is discovered by guesswork.
LOCAL_HOST = "local.safeway.com"
STORE_PAGE_SOURCE = "safeway:local-page/yext-profile"
# Public constants from the site's own JS bundle; each gateway route has its own key.
STORE_KEY = "7bad9afbb87043b28519c4443106db06"
AISLES_KEY = "e914eec9448c4d5eb672debf5011cf8f"
SEARCH_SOURCE = "safeway:xapi/aisles/similar-products"
PRODUCT_SOURCE = "safeway:product-page"
SEEDS_PATH = Path(__file__).with_name("seeds.json")
# The gateways answer only for a request that looks like it came from the site.
SITE_HEADERS = {"Referer": f"{SITE_URL}/"}
ROWS_PER_SEED = 20
_CENT = Decimal("0.01")
_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_LABEL_RE = re.compile(r"^(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z. ]+)$")
# unitOfMeasure / dispUnitOfMeasure -> token the quantity parser understands
_UOM_TEXT = {
    "CT": "ct",
    "EA": "ct",
    "EACH": "ct",
    "OZ": "oz",
    "FZ": "fl oz",
    "FO": "fl oz",
    "LB": "lb",
    "GA": "gal",
    "GAL": "gal",
    "HG": "half_gal",
    "QT": "qt",
    "PT": "pt",
    "ML": "ml",
    "LT": "l",
    "GR": "g",
    "KG": "kg",
}


class SafewayAdapter:
    site_url = SITE_URL
    slug = "safeway"
    name = "Safeway"
    # `inventoryAvailable` per store on every search result.
    stock_reporting: StockReporting = LIVE_STOCK

    # Subscription keys and the Referer travel per request, so Safeway shares the
    # application client.
    def __init__(self, clients: RetailerClients) -> None:
        self._client = clients.shared()
        self._seed_by_sku: dict[str, str] = {}

    def is_configured(self) -> bool:
        return bool(seeds())

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{XAPI_URL}/storeresolver/all",
            params={"zipcode": zip_code[:5]},
            headers={**SITE_HEADERS, "ocp-apim-subscription-key": STORE_KEY},
        )
        response.raise_for_status()
        return parse_stores(response.json())

    async def fetch_store_details(self, store: StoreLocation) -> StoreDetails | None:
        """The optional store-details capability, read from the page the resolver linked to.

        One GET per store, at most once a week. The page is refused unless its own profile
        names the store that was asked for: a redirected or stale `localPage` is another
        shop's opening times, and Safeway has two within a mile of each other downtown.
        """
        if not store.details_url:
            return None
        response = await request_with_retry(
            self._client,
            "GET",
            store.details_url,
            headers={"Accept": "text/html", **SITE_HEADERS},
            max_retries=1,
        )
        if response.status_code != 200:
            return None
        details = parse_store_details(response.text)
        if details is None or details.external_id != store.external_id:
            log.warning(
                "safeway_store_page_mismatch",
                extra={
                    "store": store.external_id,
                    "url": store.details_url,
                    "found": details.external_id if details else None,
                },
            )
            return None
        return details

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        # One request per seed shelf; the seeds are independent, so they run concurrently.
        # Their payloads are consumed in seed order, so the first seed still wins a tie.
        category_seeds = seeds().get(query, [])
        payloads = await gather_bounded(
            fanout_limit(),
            [
                partial(self._similar_products, seed["pid"], store.external_id)
                for seed in category_seeds
            ],
        )
        listings: dict[str, ProductListing] = {}
        for seed, payload in zip(category_seeds, payloads, strict=True):
            for listing in parse_similar_products(payload, store.external_id, seed["shelf"]):
                if listing.retailer_sku not in listings:
                    listings[listing.retailer_sku] = listing
                    self._seed_by_sku[listing.retailer_sku] = seed["pid"]
        return list(listings.values())

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        # A seed remembered from an earlier search gives this store's price; without one the
        # product page still identifies the product, priced for Safeway's edge-resolved store.
        seed = self._seed_by_sku.get(retailer_sku)
        if seed is not None:
            payload = await self._similar_products(seed, store.external_id)
            for listing in parse_similar_products(payload, store.external_id, None):
                if listing.retailer_sku == retailer_sku:
                    return listing
        html = await fetch_product_page(self._client, retailer_sku)
        if html is None:
            return None
        return parse_product_page(html, store.external_id)

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        return await gather_offers(self.fetch_product, retailer_sku, stores)

    async def _similar_products(self, seed_pid: str, store_external_id: str) -> dict[str, Any]:
        return await similar_products(self._client, seed_pid, store_external_id)


async def similar_products(
    client: httpx.AsyncClient, seed_pid: str, store_external_id: str
) -> dict[str, Any]:
    """The seed product's shelf neighbours, priced for one store."""
    page_url = f"{SITE_URL}/shop/pd/-/{seed_pid}"
    response = await request_with_retry(
        client,
        "GET",
        f"{XAPI_URL}/v1/aisles/similar-products",
        params={
            "storeid": store_external_id,
            "bpn": seed_pid,
            "exclude-bpn": seed_pid,
            "url": page_url,
            "pageurl": page_url,
            "request-id": "1",
            "start": "0",
            "rows": str(ROWS_PER_SEED),
            "facet": "true",
            "sort": "false",
            "includeOffer": "true",
            "banner": "safeway",
            "channel": "instore",
            "featured": "false",
            "disableTracking": "true",
        },
        headers={**SITE_HEADERS, "ocp-apim-subscription-key": AISLES_KEY},
    )
    response.raise_for_status()
    return response.json()


async def fetch_product_page(client: httpx.AsyncClient, retailer_sku: str) -> str | None:
    """The canonical product page for a product id.

    `/shop/pd/-/<pid>` answers 308 with the canonical slug in Location, but that redirect's
    body is labelled gzip while being plain text, so httpx cannot decode it. The redirect is
    therefore streamed and discarded, and only the canonical URL is fetched for real.
    """
    request = client.build_request(
        "GET",
        f"{SITE_URL}/shop/pd/-/{retailer_sku}",
        headers={**SITE_HEADERS, "Accept": "text/html"},
    )
    try:
        probe = await client.send(request, stream=True, follow_redirects=False)
    except httpx.HTTPError as exc:
        log.warning("safeway_product_probe_failed", extra={"sku": retailer_sku, "error": str(exc)})
        return None
    location = probe.headers.get("location")
    await probe.aclose()
    if probe.status_code == 200:
        location = str(request.url)
    elif not location:
        return None
    else:
        # The redirect is followed by hand, so the target is checked by hand: only the
        # canonical product URL on this site, never wherever a Location header points.
        location = str(request.url.join(location))
        if not location.startswith(f"{SITE_URL}/"):
            log.warning(
                "safeway_redirect_off_site", extra={"sku": retailer_sku, "location": location}
            )
            return None
    response = await request_with_retry(
        client,
        "GET",
        location,
        headers={**SITE_HEADERS, "Accept": "text/html"},
        max_retries=1,
    )
    return response.text if response.status_code == 200 else None


@lru_cache
def seeds() -> dict[str, list[dict[str, str]]]:
    """query -> seed products ({pid, shelf}) whose shelf neighbours are that category."""
    if not SEEDS_PATH.exists():
        return {}
    return json.loads(SEEDS_PATH.read_text())


def parse_stores(payload: dict[str, Any]) -> list[StoreLocation]:
    stores: list[StoreLocation] = []
    for record in ((payload.get("instore") or {}).get("stores")) or []:
        location_id = record.get("locationId")
        if location_id is None:
            continue
        address = record.get("address") or {}
        city = address.get("city") or None
        stores.append(
            StoreLocation(
                external_id=str(location_id),
                name=f"Safeway {city or location_id}",
                address_line1=address.get("line1") or None,
                city=city,
                state=address.get("state") or None,
                zip_code=str(address.get("zipcode") or "")[:5] or None,
                # The resolver names this store's own page. It is carried rather than built
                # from the address: the slug is Safeway's, and a rule that reproduces
                # "2300-16th-st-unit-203" correctly today is a rule that breaks silently.
                details_url=_local_page(record.get("localPage")),
            )
        )
    return stores


def _local_page(raw: Any) -> str | None:
    """`localPage` when it is a page on Safeway's own store-page host, else None."""
    text = str(raw or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme != "https" or (parts.hostname or "").lower() != LOCAL_HOST:
        return None
    return text


def parse_similar_products(
    payload: dict[str, Any], store_external_id: str, shelf: str | None
) -> list[ProductListing]:
    """Docs from one seed's shelf; `shelf` (e.g. "Eggs|1_11_3_1") drops off-shelf neighbours."""
    docs = ((payload.get("response") or {}).get("docs")) or []
    listings: list[ProductListing] = []
    for doc in docs:
        if shelf is not None and str(doc.get("shelfNameWithId") or "") != shelf:
            continue
        listing = _listing_from_doc(doc, store_external_id, SEARCH_SOURCE)
        if listing is not None:
            listings.append(listing)
    return listings


def parse_product_page(html: str, store_external_id: str) -> ProductListing | None:
    """The product record embedded in the page's Next.js RSC flight chunks."""
    docs = _flight_docs(html)
    if not docs:
        return None
    return _listing_from_doc(docs[0], store_external_id, PRODUCT_SOURCE)


def _flight_docs(html: str) -> list[dict[str, Any]]:
    text = "".join(json.loads(chunk) for chunk in _FLIGHT_RE.findall(html))
    marker = text.find('"docs":[')
    if marker < 0:
        return []
    start = text.index("[", marker)
    end = _balanced_end(text, start)
    if end is None:
        return []
    try:
        docs = json.loads(text[start:end])
    except ValueError:
        return []
    return docs if isinstance(docs, list) else []


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the JSON array/object that starts at `start`."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _listing_from_doc(
    doc: dict[str, Any], store_external_id: str, source: str
) -> ProductListing | None:
    pid = doc.get("pid") or doc.get("id")
    title = str(doc.get("name") or "").strip()
    price = _decimal(doc.get("price"))
    if not pid or not title or price is None:
        return None
    base = _decimal(doc.get("basePrice")) or price
    by_weight = str(doc.get("sellByWeight") or "").upper() == "W"
    size_text = None if by_weight else _size_text(doc)
    # `or ""` would collapse a real 0/False into "no answer"; only an absent key is unknown.
    raw_inventory = doc.get("inventoryAvailable")
    inventory = "" if raw_inventory is None else str(raw_inventory).strip()
    availability = normalize_availability(inventory) if inventory else UNKNOWN
    upc = str(doc.get("upc") or "").strip() or None
    return ProductListing(
        retailer_sku=str(pid),
        title=title,
        store_external_id=store_external_id,
        price=price.quantize(_CENT, ROUND_HALF_UP),
        regular_price=max(base, price).quantize(_CENT, ROUND_HALF_UP),
        loyalty_price=None,
        brand=None,  # the catalogue record carries no brand field; naming rules infer it
        product_url=clean_product_url(f"/shop/pd/-/{pid}", base_url=SITE_URL),
        image_url=listing_image_url(doc.get("imageUrl")),
        gtin=upc,
        size_text=size_text,
        price_basis=PER_POUND if by_weight else PACKAGE,
        availability=availability,
        stock_status=f"inventoryAvailable={inventory}" if inventory else None,
        source=source,
        attributes={
            "shelf": str(doc.get("shelfNameWithId") or ""),
            "aisle": str(doc.get("aisleName") or ""),
            "price_per": str(doc.get("pricePer") or ""),
            "division": str(doc.get("rogCd") or ""),
        },
    )


def _size_text(doc: dict[str, Any]) -> str | None:
    """ "12 ct" from the display label, else from dispItemSizeQty + dispUnitOfMeasure."""
    for label in doc.get("labels") or []:
        match = _LABEL_RE.match(str(label.get("labelName") or "").strip())
        if match:
            size = _size_from(_decimal(match.group("qty")), match.group("unit"))
            if size is not None:
                return size
    return _size_from(
        _decimal(doc.get("dispItemSizeQty")),
        str(doc.get("dispUnitOfMeasure") or doc.get("unitOfMeasure") or ""),
    )


def _size_from(quantity: Decimal | None, raw_unit: str) -> str | None:
    """Safeway's own unit token as something app.normalize.units understands."""
    if quantity is None or quantity <= 0:
        return None
    unit = _UOM_TEXT.get(raw_unit.strip().upper())
    if unit is None:
        return None
    if unit == "half_gal":  # Safeway writes half gallons as "1 hg"
        quantity, unit = quantity / 2, "gal"
    return f"{quantity.normalize():f} {unit}"


def _decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None


# ------------------------------------------------------------------ the store's own page

# The store page is a Yext-built site and hands its whole profile to its own JavaScript.
# That object -- not the rendered HTML around it -- is what is read here: the microdata says
# the same things in a form that changes whenever the page's markup does.
_YEXT_PROFILE_RE = re.compile(r"Yext\.Profile\s*=\s*\{")
_WEEKDAYS = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
}


def parse_store_details(html: str) -> StoreDetails | None:
    """Address, coordinates, timezone, hours and Safeway's own Google listing.

    Safeway publishes more about a store here than anywhere else it can be asked. The store
    resolver the scrape uses names no coordinates at all, which is why Safeway stores were
    placed at their ZIP's centroid and could only ever be pointed at by address; this page
    carries `geocodedCoordinate`, an IANA `timezone`, a real weekly pattern with dated
    holiday exceptions, and `googlePlaceId` -- a Google place id from the business itself.
    """
    profile = _yext_profile(html)
    if profile is None:
        return None
    store_id = str((profile.get("meta") or {}).get("id") or "").strip()
    if not store_id:
        return None
    address = profile.get("address") or {}
    point = profile.get("geocodedCoordinate") or profile.get("yextDisplayCoordinate") or {}
    city = address.get("city") or None
    timezone = str(profile.get("timezone") or "").strip() or None
    return StoreDetails(
        external_id=store_id,
        name=f"Safeway {city}" if city else None,
        address_line1=address.get("line1") or None,
        city=city,
        state=address.get("region") or None,
        zip_code=str(address.get("postalCode") or "")[:5] or None,
        latitude=_coordinate(point.get("lat")),
        longitude=_coordinate(point.get("long")),
        hours=_hours_from_profile(profile.get("hours"), timezone),
        # The place id is preferred over the ready-made CID link: it is the identifier, and
        # `services/maps.py` builds Google's own documented URL around it.
        maps_place_url=str(profile.get("googlePlaceId") or profile.get("c_googleCIDURL") or "")
        or None,
        source=STORE_PAGE_SOURCE,
    )


def _yext_profile(html: str) -> dict[str, Any] | None:
    match = _YEXT_PROFILE_RE.search(html)
    if match is None:
        return None
    start = match.end() - 1
    depth = 0
    index = start
    in_string = False
    while index < len(html):
        char = html[index]
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
                    try:
                        profile = json.loads(html[start : index + 1])
                    except ValueError:
                        return None
                    return profile if isinstance(profile, dict) else None
        index += 1
    return None


def _hours_from_profile(hours: Any, timezone: str | None) -> StoreHours | None:
    """`normalHours` is the standing week; `holidayHours` are the dated exceptions.

    Safeway states which is which, so neither is inferred: a weekday keeps its window and a
    published holiday overrides only its own date. `isClosed` is a day the store does not
    open, which is a fact worth showing rather than a gap.
    """
    if not isinstance(hours, dict) or not timezone:
        return None
    weekly: dict[int, DayHours] = {}
    for entry in hours.get("normalHours") or []:
        if not isinstance(entry, dict):
            continue
        weekday = _WEEKDAYS.get(str(entry.get("day") or "").upper())
        if weekday is None:
            continue
        weekly[weekday] = _window(entry)
    dates: dict[str, DayHours] = {}
    for entry in hours.get("holidayHours") or []:
        if not isinstance(entry, dict):
            continue
        day = _holiday_date(entry.get("date"))
        if day is not None:
            dates[day] = _window(entry)
    return hours_from_weekly(weekly, dates, timezone)


def _window(entry: dict[str, Any]) -> DayHours:
    """One day's window from a Yext interval. Yext writes 600 for 06:00 and 2300 for 23:00."""
    intervals = [i for i in (entry.get("intervals") or []) if isinstance(i, dict)]
    if entry.get("isClosed") is True or not intervals:
        return DayHours(None, None)
    opens = _yext_clock(intervals[0].get("start"))
    closes = _yext_clock(intervals[-1].get("end"))
    return DayHours(opens, closes) if opens and closes else DayHours(None, None)


def _yext_clock(raw: Any) -> str | None:
    """A Yext time-of-day integer as a wall clock. 600 -> "06:00", 2300 -> "23:00".

    **2400 is midnight at the end of the day, and it is how a shop that never closes is
    written**: Yext gives a 24-hour store the interval `{"start": 0, "end": 2400}`. Read
    literally that is "24:00", which is not a time `time.fromisoformat` accepts, so it came
    back `None` and the day was recorded as one the store does not open at all -- a Safeway
    that is open around the clock rendered as "Closed today", which is the worst direction
    for this to be wrong in. It maps to "00:00", and a window that closes at or before it
    opens is already read as running past midnight (`normalize/hours.py::_open_window`), so a
    00:00-00:00 day is the whole day.
    """
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 2400:
        return None
    if raw == 2400:
        return "00:00"
    text = f"{raw // 100:02d}:{raw % 100:02d}"
    return text if parse_wall_clock(text) else None


def _holiday_date(raw: Any) -> str | None:
    """Yext writes a holiday date as "20260907"; the schedule keys dates as ISO."""
    text = str(raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _coordinate(raw: Any) -> float | None:
    return float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None
