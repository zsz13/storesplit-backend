"""The retailer adapter contract.

Adapters translate a retailer's public data into these retailer-agnostic records. They own
request construction, parsing, pagination and retailer quirks. They never touch the
database and never normalize categories or units; the scrape service does that.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Literal, Protocol

from app.concurrency import fanout_limit, gather_bounded
from app.normalize.availability import UNKNOWN, Availability, StockReporting
from app.normalize.hours import StoreHours, UnzonedHours
from app.normalize.pricing import PACKAGE, PER_OUNCE, PER_POUND, PriceBasis
from app.normalize.units import QuantityRange

SoldBy = Literal["unit", "weight"]


@dataclass(frozen=True)
class StoreLocation:
    external_id: str
    name: str
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # The retailer's own page for this store, when its locator names one -- Safeway's
    # `localPage`, Trader Joe's `website`. It is carried rather than derived because a slug
    # built by rule is a guess: Raley's taught this repo that splitting a store slug by hand
    # produces "Raley's Reetfairfield" and addresses that never geocode. `fetch_store_details`
    # uses it; an adapter whose locator names no page simply leaves it None.
    details_url: str | None = None
    # The retailer's *own* number for this store, when its locator publishes one beside the
    # id the price endpoints use -- Sprouts' `location_code` (276), next to the Instacart
    # shop id (601) that `external_id` has to hold. It is carried for the same reason
    # `details_url` is: the alternative was reading it back out of the display name, which
    # makes a label load-bearing and loses a store's hours the day somebody shortens it.
    store_number: str | None = None


@dataclass(frozen=True)
class StoreDetails:
    """What a retailer publishes about one of its own stores.

    Produced by the optional `fetch_store_details` capability -- optional because most
    retailers publish no hours anywhere StoreSplit is allowed to read, and a Protocol method
    that eight adapters raise on is a worse contract than one they simply do not have. The
    scrape service probes for it with `getattr`, exactly as it does for `unconfigured_reason`.
    """

    external_id: str
    name: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # The store's public telephone number, digits only with its country code
    # ("+19257548824"), when the retailer states one. Kept because a shopper who finds a
    # shop closed, or an item they cannot see on the shelf, calls it -- and because the
    # number is published beside the address by every retailer that publishes either.
    phone: str | None = None
    hours: StoreHours | None = None
    # Weekday windows the retailer published without naming a zone. Kept apart from `hours`
    # because it is not a schedule yet and must not be mistaken for one: Trader Joe's states
    # a complete week per store and no timezone anywhere, and `normalize/hours.py::
    # hours_from_unzoned` is the only way out of it. The scrape pairs it with a zone
    # established for this store some other way -- in practice the one its own coordinates
    # stand in -- or leaves the store's hours unknown.
    unzoned_hours: UnzonedHours | None = None
    # A Google Maps link or place id the retailer itself published for this store -- Target's
    # `miscellaneous.google_cid`, Safeway's `c_googleCIDURL`. This is the only way a place is
    # identified with no third party involved and no guessing: the business named its own
    # listing. `services/maps.py` validates it before it is ever stored or rendered.
    maps_place_url: str | None = None
    source: str = ""  # which endpoint produced this record


@dataclass(frozen=True)
class ProductListing:
    """One retailer product together with its offer at one store."""

    retailer_sku: str
    title: str
    store_external_id: str
    price: Decimal
    regular_price: Decimal
    brand: str | None = None
    product_url: str | None = None
    image_url: str | None = None
    gtin: str | None = None
    size_text: str | None = None  # retailer's package size string, e.g. "12 ct", "64 fl oz"
    # What `price` is quoted *per*, taken from the retailer's own statement rather than
    # inferred later from a display string. `package` is a total for one package; `lb`/`oz`
    # are rates that are already per unit and must never be divided by a package size again.
    price_basis: PriceBasis = PACKAGE
    # The span a variable-weight package is sold within, exactly as the retailer published it
    # ("2.5-5.25lbs"). Kept beside the price, never folded into it: there is no single size.
    weight_range: QuantityRange | None = None
    # The most the retailer says this item can cost -- Target's `formatted_max_item_price`.
    # Set only when the retailer states it. It is not `price x max weight`: Target's own
    # maximum for a 2.5-5.25 lb tray at $2.59/lb is $12.95, not the $13.60 that product
    # would give, so computing one here would publish a wrong number in an authoritative tone.
    max_total_price: Decimal | None = None
    loyalty_price: Decimal | None = None
    # The normalized state search and baskets reason about; `unknown` unless the retailer
    # said something about *this* store. `stock_status` keeps the retailer's own wording.
    availability: Availability = UNKNOWN
    stock_status: str | None = None
    # The store the retailer echoed back while answering -- Whole Foods' `storeId`, Raley's
    # `currentStoreNumber`. `None` means the retailer stated no store, which is not an error;
    # when it is set, the scrape service refuses a listing whose echo names another store.
    store_context: str | None = None
    currency: str = "USD"
    source: str = ""  # which endpoint produced this record
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def sold_by(self) -> SoldBy:
        """The coarse reading of `price_basis` kept on canonical products.

        `price_basis` is the real field -- it says *which* unit, which is what the arithmetic
        needs. This stays because "is it weighed" is a durable fact about a product rather
        than about one price, and it is what `CanonicalProduct.attributes["sold_by"]` has
        always held; deriving it here keeps that column stable with no second source of truth.
        """
        return "weight" if self.price_basis in (PER_POUND, PER_OUNCE) else "unit"


class RetailerAdapter(Protocol):
    """Adapters are async: every method that talks to a retailer awaits I/O.

    They do not own their HTTP client. `RetailerClients` opens one per process and closes it
    on shutdown, so an adapter instance is cheap and holds no resource to release.
    """

    slug: str
    name: str
    # The retailer's public site. Product URLs must live on this host (see
    # `retailers/urls.py`); adapters resolve relative paths against it and the scrape service
    # re-checks every URL against it before writing.
    site_url: str
    # Does this retailer state per-store stock anywhere StoreSplit reads? A standing fact
    # about the retailer, not about any offer, and required rather than defaulted: a default
    # would answer the question for an adapter whose author never considered it, and the
    # wrong default in either direction is a lie to a shopper. `live` means the retailer
    # publishes inventory and an `unknown` offer is a reading that failed; `not_published`
    # means it never publishes any, so `unknown` is the only state its offers can have and
    # the honest wording is "availability not published", not "stock unknown".
    # It changes no ranking anywhere -- see `normalize/availability.py`.
    stock_reporting: StockReporting

    def is_configured(self) -> bool:
        """False when this adapter cannot run; the scrape then skips it and says why.

        Usually missing credentials (Kroger). It is also how a retailer that needs the
        optional browser layer stays out of an ordinary scrape: Target reports False unless
        `BROWSER_FALLBACK_ENABLED` is set and Playwright is installed.

        An adapter may also define `unconfigured_reason() -> str` saying *why*, which the
        scrape service prints instead of its generic "missing credentials". It is optional
        rather than part of this protocol so that adding it never touches the adapters that
        do not need it.
        """
        ...

    async def find_stores(self, zip_code: str) -> list[StoreLocation]:
        """Stores that serve a ZIP code, nearest first."""
        ...

    async def search_products(self, query: str, store: StoreLocation) -> list[ProductListing]:
        """Search results with prices for one store.

        An adapter whose searches cannot really run in parallel may set
        `max_concurrent_searches: int` -- the number of its `(store, category)` searches the
        scrape service may have in flight at once, clamped to the retailer's request budget.
        Optional, like `unconfigured_reason`, so it never touches an adapter that does not need
        it; absent means the budget. The browser-backed retailers need it: they share one page in
        one browser, so their searches take turns whatever is launched, and the ones queued
        behind the page would otherwise spend the retailer's deadline waiting for it. Target
        declares 1; Walmart shares the constraint and should when it is next touched.
        """
        ...

    async def fetch_product(self, retailer_sku: str, store: StoreLocation) -> ProductListing | None:
        """One product with its offer at one store, or None when not found."""
        ...

    async def fetch_offers(
        self, retailer_sku: str, stores: list[StoreLocation]
    ) -> list[ProductListing]:
        """The product's offers across several stores."""
        ...


# The optional store-details capability. An adapter that can read its retailer's own store
# pages defines `fetch_store_details` with this signature and the scrape service finds it;
# Whole Foods does. Most retailers publish nothing on a surface robots.txt allows, and making
# them all declare a method they would raise from would be a less honest contract, not a
# stricter one -- which is why this is a type rather than a member of `RetailerAdapter`.
type StoreDetailsFetcher = Callable[[StoreLocation], Awaitable[StoreDetails | None]]


class AdapterUnavailableError(RuntimeError):
    """Raised when a retailer cannot be used (missing credentials, blocked, etc.)."""


async def gather_offers(
    fetch_product: Callable[[str, StoreLocation], Awaitable[ProductListing | None]],
    retailer_sku: str,
    stores: list[StoreLocation],
) -> list[ProductListing]:
    """`fetch_offers` for retailers with no multi-store endpoint: one lookup per store.

    The lookups are independent, so they run concurrently under the same per-retailer bound
    the scrape service uses.
    """
    results = await gather_bounded(
        fanout_limit(), [partial(fetch_product, retailer_sku, store) for store in stores]
    )
    return [listing for listing in results if listing is not None]
