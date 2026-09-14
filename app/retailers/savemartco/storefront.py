"""The Instacart white-label storefront that Lucky and Save Mart run on.

Both Save Mart Companies banners left Swiftly for Instacart's storefront platform, so their
shops answer the same plain HTTPS GraphQL as any other storefront on it, with the guest
session cookie the storefront hands every visitor:
  * GET  https://<host>/store/<slug>/storefront          sets `__Host-instacart_sid`
  * GET  https://<host>/graphql?operationName=DefaultShop&variables={postalCode,coordinates}
        the shop serving a ZIP (404 where the banner has no store there).
  * GET  .../graphql?operationName=SearchResultsPlacements                keyword -> item ids
  * GET  .../graphql?operationName=Items&variables={ids,shopId,zoneId,postalCode}
        title, brand, package size, shelf price and the struck-through regular price.

Unlike Sprouts' storefront these banners do not enable the `idp/v1` shop locator (init 403s,
shops 401s), hence `DefaultShop` plus the ZIP centroids the repo already vendors. The
persisted-query hashes are platform-wide constants from the storefront's JS bundle, identical
to the ones the Sprouts adapter pins; a stale hash fails loudly.

**Which physical store a shop is, is published -- just not by `DefaultShop`.** That query
names a `retailerLocationId` beside the shop id (and every item id it later answers with is
literally `items_<retailerLocationId>-<productId>`), and the storefront's own pickup picker
turns that id into a street address and the banner's own store number:
  * GET  https://<host>/v3/retailers/<retailerId>/pickup_locations?zip_code=<zip>
        `{id, location_code, name, address{address_line_1, city, state, zip_code, lat, lon}}`
        for every store of the banner near that ZIP, nearest first. Same host, same guest
        cookie (401 without it); `retailerId` comes from `DefaultShop`, so nothing is pinned.
A shop is one *fulfilment mode* of a store and a store has several, which is why the store
row is keyed on the location and not on the shop -- see `Shop`.

**Hours, the exact name and the zone come from the banner's own website**, not from the
storefront, exactly as Sprouts' do:
  * GET  https://<banner site>/stores/<store number>?_data=routes/stores.$storeId._index
        the Remix loader behind luckysupermarkets.com/stores/212 -- `storeDetailsV2` with
        `displayName`, `location{address1, city, state, postalCode, latitude, longitude,
        timezone}`, `phoneNumbers` and `hours{weekly, special}`. No cookie, no key, and
        `luckysupermarkets.com` / `savemart.com` publish a two-line permissive robots.txt
        (`Allow: /`, `Disallow: /cgi-bin/`) that is nothing to do with the storefront's.
The store number is the `location_code` the pickup picker already published, so no slug is
rebuilt and no number is read back out of a display name.

Neither banner exposes a UPC anonymously, so listings carry no GTIN.
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.normalize.availability import Availability
from app.normalize.hours import (
    DayHours,
    StoreHours,
    hours_from_weekly,
    parse_clock_24h,
)
from app.normalize.phone import e164_us
from app.normalize.pricing import PACKAGE, PER_POUND
from app.retailers.base import ProductListing, StoreDetails, StoreLocation
from app.retailers.http import request_with_retry
from app.retailers.images import listing_image_url
from app.retailers.instacart_storefront import (
    storefront_availability,
    storefront_product_path,
)
from app.retailers.urls import clean_product_url
from app.retailers.zipmatch import zip_centroid

log = logging.getLogger("storesplit.retailers.savemartco")

OPERATION_HASHES = {
    "DefaultShop": "d389a8d33d63801f1ce5c4929fb181dd10c57b49c3a2dcb6a6baa44212e8e069",
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
_CENT = Decimal("0.01")
_MONEY_RE = re.compile(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)")
_PER_UNIT_RE = re.compile(r"/\s*(lb|pound|oz)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Banner:
    """One Save Mart Companies banner: its storefront, and the website it publishes stores on.

    Two different sites, deliberately named separately. `host` is the Instacart storefront
    the prices come from; `store_site` is the banner's own website, which is where the store
    over the door is described -- its name, its street, its zone and its opening hours -- and
    which the storefront states none of. They are neither the same host nor the same
    robots.txt, and conflating them is what once made "this retailer publishes no hours" look
    like a fact about the company rather than about one of its two sites.
    """

    host: str
    store_slug: str
    retailer_name: str
    store_site: str

    @property
    def site_url(self) -> str:
        return f"https://{self.host}"

    def store_page_url(self, store_number: str) -> str:
        """The banner's own page for one of its stores, by the number it publishes it under.

        Built here rather than taken from a payload because the banner names no such link
        anywhere, and it is built from `location_code` -- the store number the storefront
        itself states -- so the only thing assumed is the route, which is the same on all
        three Save Mart Companies banners.
        """
        return f"{self.store_site}/stores/{store_number}"

    def product_url(
        self, product_id: str, canonical: object, evergreen: object = None
    ) -> str | None:
        """This item's page on the banner, preferring the slug the storefront itself uses.

        Three sources, best first:

        * `productCanonicalUrl` is an *object* -- `{id, canonicalUrl, __typename}` -- whose
          `canonicalUrl` is null for almost every item on these banners. Only the nested
          string is a URL; the object itself never is.
        * `evergreenUrl`, the storefront's own slug (see `storefront_product_path`, which
          is shared with Sprouts so one banner's hardening reaches all three).
        * the product id path, which is a real page and redirects to the slug above.
        """
        nested = canonical.get("canonicalUrl") if isinstance(canonical, dict) else None
        from_payload = clean_product_url(nested, base_url=self.site_url)
        if from_payload is not None:
            return from_payload
        return clean_product_url(
            storefront_product_path(self.store_slug, product_id, evergreen),
            base_url=self.site_url,
        )


LUCKY_BANNER = Banner(
    host="shop.luckysupermarkets.com",
    store_slug="lucky-supermarkets",
    retailer_name="Lucky Supermarkets",
    store_site="https://luckysupermarkets.com",
)
SAVEMART_BANNER = Banner(
    host="shop.savemart.com",
    store_slug="savemart",
    retailer_name="Save Mart",
    store_site="https://savemart.com",
)


@dataclass(frozen=True)
class Shop:
    """The storefront shop serving a ZIP, and the physical store it sells from.

    `shop_id` is the id every price query is addressed to. `location_id` is the retailer
    *location* behind it, and the two are not interchangeable: a shop is one fulfilment mode
    of a store, so one store has several shop ids and a store row keyed on a shop id becomes
    two rows for one supermarket the day a different mode answers. StoreSplit therefore keys
    the row on the location and addresses the price queries to the shop -- Sprouts, which
    keys on the shop, has four rows for two Bay Area stores to show for it.

    `retailer_id` is Instacart's id for the banner itself (Lucky 542, Save Mart 294). It is
    read off this payload rather than pinned as a constant, because the one place it is
    needed -- the pickup-location lookup -- is reached only after this query has answered.
    """

    shop_id: str
    retailer_id: str | None = None
    location_id: str | None = None

    @property
    def store_id(self) -> str:
        """The identity a store row takes: the physical location, or the shop as a fallback.

        The fallback is what a payload that names no location leaves, and it is the old
        behaviour rather than a new guess -- prices still flow, and the store is simply not
        merged with anything.
        """
        return self.location_id or self.shop_id


@dataclass(frozen=True)
class PickupLocation:
    """One physical store of a banner, as its own storefront's pickup picker describes it."""

    location_id: str
    # The banner's own store number (Lucky 212, Save Mart 781) -- `location_code`. It is the
    # key the banner's *website* is addressed by, so it is what turns a shop into hours.
    store_number: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class StorefrontClient:
    """One banner's storefront: session, shop lookup, search and item lookup."""

    def __init__(self, banner: Banner, client: httpx.AsyncClient) -> None:
        self.banner = banner
        self.host = banner.host
        self.store_slug = banner.store_slug
        self.site_url = banner.site_url
        # The banner's guest session cookie lives in this client's jar, which is why each
        # banner gets its own persistent client rather than the shared one.
        self._client = client
        self._session_ready = False
        self._session_lock = asyncio.Lock()

    async def ensure_session(self) -> None:
        if self._session_ready:
            return
        # Concurrent categories share one storefront: only the first fetches the cookie.
        async with self._session_lock:
            if self._session_ready:
                return
            response = await request_with_retry(
                self._client,
                "GET",
                f"{self.site_url}/store/{self.store_slug}/storefront",
                headers={"Accept": "text/html"},
            )
            response.raise_for_status()
            if "__Host-instacart_sid" not in self._client.cookies:
                raise RuntimeError(f"{self.host} did not issue a guest session cookie")
            self._session_ready = True

    async def default_shop(self, zip_code: str) -> Shop | None:
        """The shop serving a ZIP, or None where this banner has no store there."""
        await self.ensure_session()
        latitude, longitude = zip_centroid(zip_code) or (0.0, 0.0)
        payload = await self.graphql(
            "DefaultShop",
            {
                "postalCode": zip_code[:5],
                "coordinates": {"latitude": latitude, "longitude": longitude},
            },
            allow_errors=True,
        )
        return parse_default_shop(payload)

    async def pickup_locations(self, retailer_id: str, zip_code: str) -> list[PickupLocation]:
        """This banner's physical stores near a ZIP, nearest first, with their addresses.

        The storefront's own pickup picker, on the same host and behind the same guest cookie
        the price queries already hold (it answers 401 without one). It is the only anonymous
        surface that states what a `retailerLocationId` *is*: a street address and the
        banner's own store number.
        """
        await self.ensure_session()
        response = await request_with_retry(
            self._client,
            "GET",
            f"{self.site_url}/v3/retailers/{retailer_id}/pickup_locations",
            params={"zip_code": zip_code[:5]},
        )
        response.raise_for_status()
        return parse_pickup_locations(response.json())

    async def search_item_ids(self, query: str, shop_id: str, zip_code: str) -> list[str]:
        await self.ensure_session()
        payload = await self.graphql(
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
                "shopId": shop_id,
                "postalCode": zip_code,
                "zoneId": "1",
                "first": 4,
            },
        )
        return parse_search_item_ids(payload)[:MAX_ITEMS]

    async def items(self, item_ids: list[str], shop_id: str, zip_code: str) -> dict[str, Any]:
        await self.ensure_session()
        return await self.graphql(
            "Items",
            {"ids": item_ids, "shopId": shop_id, "zoneId": "1", "postalCode": zip_code},
        )

    async def graphql(
        self, operation: str, variables: dict[str, Any], *, allow_errors: bool = False
    ) -> dict[str, Any]:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{self.site_url}/graphql",
            params={
                "operationName": operation,
                "variables": json.dumps(variables, separators=(",", ":")),
                "extensions": json.dumps(
                    {"persistedQuery": {"version": 1, "sha256Hash": OPERATION_HASHES[operation]}},
                    separators=(",", ":"),
                ),
            },
        )
        if response.status_code == 404 and allow_errors:
            # The banner has no shop for these coordinates at all.
            return {}
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if errors and not payload.get("data") and not allow_errors:
            message = errors[0].get("message", "")
            hint = " (persisted query hash is stale)" if "PersistedQuery" in message else ""
            raise RuntimeError(f"{self.host} GraphQL {operation} failed: {message}{hint}")
        return payload


def parse_default_shop(payload: dict[str, Any]) -> Shop | None:
    """`DefaultShop` as the shop and the physical store behind it."""
    shop = (payload.get("data") or {}).get("defaultShop") or {}
    shop_id = str(shop.get("id") or "")
    if not shop_id:
        return None
    return Shop(
        shop_id=shop_id,
        retailer_id=str(shop.get("retailerId") or "") or None,
        location_id=str(shop.get("retailerLocationId") or "") or None,
    )


def parse_pickup_locations(payload: Any) -> list[PickupLocation]:
    """The pickup picker's stores, nearest first, one record per physical location.

    Deduplicated on the location id: the payload lists a store once, but the id is the
    identity a store row is keyed on, and a list that could hand back two of them would put
    the duplicate straight into the database this exists to prevent.
    """
    rows = payload.get("pickup_locations") if isinstance(payload, dict) else None
    found: dict[str, PickupLocation] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        location_id = str(row.get("id") or "")
        if not location_id or location_id in found:
            continue
        published = row.get("address")
        address: dict[str, Any] = published if isinstance(published, dict) else {}
        found[location_id] = PickupLocation(
            location_id=location_id,
            store_number=_store_number(row.get("location_code")),
            address_line1=str(address.get("address_line_1") or "").strip() or None,
            city=str(address.get("city") or "").strip() or None,
            state=str(address.get("state") or "").strip() or None,
            zip_code=str(address.get("zip_code") or "").strip()[:5] or None,
            latitude=_coordinate(address.get("lat")),
            longitude=_coordinate(address.get("lon")),
        )
    return list(found.values())


# The banner's own store number as it appears in a URL path. Digits only, because that is
# what all three banners publish and because this value is pasted into a path: a
# `location_code` of "../../admin" is not a store number, and refusing it here is cheaper
# than discovering later which of two sites the request went to.
_STORE_NUMBER_RE = re.compile(r"^[0-9]{1,10}$")
# The Remix route behind `<banner site>/stores/<n>`. Asking for it by name is what turns the
# page into its own JSON loader -- the same record the HTML is rendered from, without the
# 300 KB of markup around it.
STORE_ROUTE = "routes/stores.$storeId._index"
# Weekday names as the banners write them, to `date.weekday()`.
_WEEKDAYS = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
}
# A day the banner states as `OPEN_24_HOURS`, which it publishes with no clock at all.
# "00:00" to "00:00" is midnight to midnight: `hours_today` reads a close at or before its
# open as running into the next day, so this is a day with no shut moment in it.
OPEN_ALL_DAY = DayHours("00:00", "00:00")
_NON_DIGITS = re.compile(r"\D+")


async def fetch_store_record(
    client: httpx.AsyncClient, banner: Banner, store_number: str
) -> dict[str, Any] | None:
    """The banner's own record for one store, or None when it serves none under that number.

    A separate site from the storefront and a separate client: cookies are scoped to a
    domain and a `.luckysupermarkets.com` cookie set here would be sent on every subsequent
    *storefront* request, changing behaviour there for a reason nothing in this adapter
    explains. This is the shared client for the same reason Sprouts' details fetch is.
    """
    if not _STORE_NUMBER_RE.fullmatch(store_number):
        log.warning("savemartco_bad_store_number", extra={"store_number": store_number})
        return None
    response = await request_with_retry(
        client, "GET", banner.store_page_url(store_number), params={"_data": STORE_ROUTE}
    )
    if response.status_code == 404:
        # A number nobody serves. The banners do 404 it, unlike Sprouts' site -- but the
        # record is checked below all the same, because a `200` carrying a null store is
        # the shape this platform answers a retired store with.
        return None
    response.raise_for_status()
    payload = response.json()
    record = payload.get("storeDetailsV2") if isinstance(payload, dict) else None
    if not isinstance(record, dict) or not record.get("storeId"):
        return None
    return record


def store_details_from_record(
    record: dict[str, Any], external_id: str, source: str, *, banner: Banner
) -> StoreDetails | None:
    """One `storeDetailsV2` record as published store details.

    Everything a shopper is told about a Lucky or Save Mart store comes from here: the name
    over the door, the street it is on, the point it stands at, the zone its clock is kept
    in and the week it keeps. The storefront the prices come from states none of it.
    """
    if not record.get("storeId"):
        return None
    published = record.get("location")
    location: dict[str, Any] = published if isinstance(published, dict) else {}
    timezone = str(location.get("timezone") or "").strip() or None
    return StoreDetails(
        external_id=external_id,
        name=store_display_name(banner, record.get("displayName")),
        address_line1=_titled(location.get("address1")),
        city=_titled(location.get("city")),
        state=str(location.get("state") or "").strip().upper() or None,
        zip_code=str(location.get("postalCode") or "").strip()[:5] or None,
        latitude=_coordinate(location.get("latitude")),
        longitude=_coordinate(location.get("longitude")),
        phone=_phone(record.get("phoneNumbers")),
        hours=parse_store_hours(record.get("hours"), timezone),
        source=source,
    )


def parse_store_hours(hours: Any, timezone: str | None) -> StoreHours | None:
    """A `hours{weekly, special}` block as a schedule, in the zone the record states.

    A real weekly pattern -- seven `{day, daily}` entries per store -- so nothing is
    generalised from a published fortnight the way Whole Foods' and Target's calendars have
    to be. `hours_from_weekly` returns None without a timezone, which is the rule that keeps
    Trader Joe's hours out of the product: a wall clock with no zone is not a fact about a
    store. These banners publish one (`America/Los_Angeles` on all 189 stores of the three of
    them), which is the whole difference.
    """
    weekly: dict[int, DayHours] = {}
    block = hours if isinstance(hours, dict) else {}
    for entry in block.get("weekly") or []:
        if not isinstance(entry, dict):
            continue
        weekday = _WEEKDAYS.get(str(entry.get("day") or "").strip().upper())
        window = _day_window(entry.get("daily"))
        if weekday is not None and window is not None:
            weekly[weekday] = window
    return hours_from_weekly(weekly, parse_special_days(block.get("special")), timezone)


def parse_special_days(special: Any) -> dict[str, DayHours]:
    """Dated exceptions -- a holiday closure, a short Christmas Eve -- keyed by ISO date.

    The platform declares `hours.special` on every store and has it empty on every one of
    them (189 stores across Lucky, Save Mart and FoodMaxx, checked 2026-09-12), so the shape
    of an entry has never been observed. What is read here is therefore only what is already
    known: the same `daily` block the weekly entries carry, beside a date that parses as an
    ISO date. An entry without both is skipped and logged rather than guessed at, so the day
    falls back to the store's standing hours -- the answer the banner's own site gives today.
    Reading it wrong would tell a shopper a shut store is open, which is the one outcome
    worth less than saying nothing.
    """
    dates: dict[str, DayHours] = {}
    for entry in special or []:
        if not isinstance(entry, dict):
            continue
        day = _iso_date(entry.get("date")) or _iso_date(entry.get("day"))
        window = _day_window(entry.get("daily"))
        if day is None or window is None:
            log.warning("savemartco_unread_special_hours", extra={"entry": str(entry)[:200]})
            continue
        dates[day] = window
    return dates


def _day_window(daily: Any) -> DayHours | None:
    """One day of the banner's calendar, or None where it says something nobody has read.

    Three states are understood. `OPEN` carries a clock; `OPEN_24_HOURS` carries none at all
    and means the day has no shut moment; `CLOSED` is a stated closure, which is a fact worth
    publishing -- `hours_today` says "Closed today" for it rather than counting to an opening
    that never comes. Anything else is left out of the week entirely and logged, because a
    day absent from the pattern reads as unknown, where a guessed one reads as a promise.
    """
    if not isinstance(daily, dict):
        return None
    kind = str(daily.get("type") or "").strip().upper()
    if kind == "OPEN_24_HOURS":
        return OPEN_ALL_DAY
    if kind == "CLOSED":
        return DayHours(None, None)
    if kind != "OPEN":
        log.warning("savemartco_unknown_hours_type", extra={"hours_type": kind})
        return None
    window = daily.get("open")
    if not isinstance(window, dict):
        return None
    opens = parse_clock_24h(window.get("open"))
    closes = parse_clock_24h(window.get("close"))
    if opens is None or closes is None:
        return None
    return DayHours(opens, closes)


def store_display_name(banner: Banner, branch: Any) -> str | None:
    """ "CONTRA LOMA" -> "Lucky Supermarkets - Contra Loma".

    The banner publishes the branch alone and in capitals, which is a heading on its own
    store page and a shout in a list of nine retailers. The retailer leads because a store
    line is read beside other retailers' and because `services/maps.py` searches with this
    string: "Contra Loma, 3190 Contra Loma Blvd" names no supermarket, and Google resolves
    the street.
    """
    text = str(branch or "").strip()
    return f"{banner.retailer_name} - {text.title()}" if text else None


def store_from_shop(
    shop: Shop,
    location: PickupLocation | None,
    details: StoreDetails | None,
    banner: Banner,
    zip_code: str,
) -> StoreLocation:
    """One physical store, from the three surfaces that each know part of it.

    `external_id` is the *location*, not the shop: see `Shop`. The name and the address come
    from the banner's own site where it answered and from the pickup picker where it did not,
    so a store still has a street and a point when only the storefront could be reached -- a
    map link and a distance ranking are worth having on the week the other site is down.
    """
    published = details or StoreDetails(external_id=shop.store_id)
    store_number = location.store_number if location else None
    fallback = f"{banner.retailer_name} - {store_number}" if store_number else None
    known = location or PickupLocation(location_id=shop.store_id)
    return StoreLocation(
        external_id=shop.store_id,
        name=published.name or fallback or f"{banner.retailer_name} {shop.store_id}",
        address_line1=published.address_line1 or known.address_line1,
        city=published.city or known.city,
        state=published.state or known.state,
        zip_code=published.zip_code or known.zip_code or zip_code[:5],
        latitude=published.latitude if published.latitude is not None else known.latitude,
        longitude=published.longitude if published.longitude is not None else known.longitude,
        details_url=banner.store_page_url(store_number) if store_number else None,
        store_number=store_number,
    )


def _store_number(raw: Any) -> str | None:
    text = str(raw or "").strip()
    return text if text and _STORE_NUMBER_RE.fullmatch(text) else None


def _iso_date(raw: Any) -> str | None:
    """A published date as "YYYY-MM-DD", or nothing at all.

    Parsing is the validation: it is what tells a date apart from the weekday name the
    weekly entries put under the same key names.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _phone(numbers: Any) -> str | None:
    """The store's main number in E.164, or nothing.

    The record publishes `[{"value": "19257548824", "description": "Main"}]`: eleven digits,
    country code included, no punctuation. Ten digits take the +1 these banners are wholly
    within; anything else is left alone rather than padded into a number that would dial
    somewhere. The `Main` line is preferred where a store lists several.
    """
    entries = [n for n in (numbers or []) if isinstance(n, dict) and n.get("value")]
    if not entries:
        return None
    main = next((n for n in entries if str(n.get("description") or "").lower() == "main"), None)
    # Picking which line is the shop's is retailer knowledge and stays here; reading the
    # number itself is normalization and does not.
    return e164_us((main or entries[0]).get("value"))


def _titled(raw: Any) -> str | None:
    """A line the banner publishes in capitals, cased for a sentence a shopper reads."""
    text = str(raw or "").strip()
    return text.title() if text else None


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
            ids.extend(str(item_id) for item_id in content.get("itemIds") or [])
    return list(dict.fromkeys(ids))


def parse_items(
    payload: dict[str, Any], store_external_id: str, source: str, *, banner: Banner
) -> list[ProductListing]:
    listings: dict[str, ProductListing] = {}
    for item in (payload.get("data") or {}).get("items") or []:
        listing = _listing_from_item(item, store_external_id, source, banner)
        if listing is not None and listing.retailer_sku not in listings:
            listings[listing.retailer_sku] = listing
    return list(listings.values())


def _listing_from_item(
    item: dict[str, Any], store_external_id: str, source: str, banner: Banner
) -> ProductListing | None:
    product_id = item.get("productId")
    title = item.get("name")
    view = ((item.get("price") or {}).get("viewSection")) or {}
    price = _money(view.get("priceString"))
    if not product_id or not title or price is None:
        return None
    regular = _money(view.get("fullPriceString")) or price
    quantity = item.get("quantityAttributes") or {}
    by_weight = str(quantity.get("quantityType") or "") == "weight" or bool(
        _PER_UNIT_RE.search(str(view.get("priceString") or ""))
    )
    stock_status, state = _availability(item)
    item_view = item.get("viewSection") or {}
    return ProductListing(
        retailer_sku=str(product_id),
        title=str(title),
        store_external_id=store_external_id,
        price=price,
        regular_price=max(regular, price),
        loyalty_price=None,
        brand=_brand(item.get("brandName")),
        product_url=banner.product_url(
            str(product_id), item.get("productCanonicalUrl"), item.get("evergreenUrl")
        ),
        image_url=listing_image_url(item_view.get("itemImage")),
        gtin=None,  # not exposed to guests on these banners
        size_text=None if by_weight else (item.get("size") or None),
        price_basis=PER_POUND if by_weight else PACKAGE,
        availability=state,
        stock_status=stock_status,
        source=source,
        attributes={
            "item_id": str(item.get("id") or ""),
            "size": str(item.get("size") or ""),
            "offer": str(((view.get("badge") or {}).get("offerLabelString")) or ""),
        },
    )


def _availability(item: dict[str, Any]) -> tuple[str | None, Availability]:
    """The shop-specific inventory the `Items` query answered with, and its raw wording.

    These banners answer per shop, so this is the searched store's inventory and not a
    default store's. What that inventory *means* is `instacart_storefront`'s job: the
    platform's `stockLevel` token goes stale and its `lowStock` is the page's "Likely out
    of stock", so the shopper-visible label decides. Reading the token alone is what showed
    product 19830961 as buyable while its own product page said it was not.
    """
    return storefront_availability(item.get("availability"))


def _money(text: Any) -> Decimal | None:
    match = _MONEY_RE.search(str(text)) if text else None
    if not match:
        return None
    return Decimal(match.group(1).replace(",", "")).quantize(_CENT, ROUND_HALF_UP)


def _brand(raw: Any) -> str | None:
    text = str(raw or "").strip()
    return text.title() if text else None
