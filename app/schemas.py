"""API request/response models."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.normalize.availability import IN_STOCK, LIVE_STOCK, Availability, StockReporting
from app.normalize.hours import HoursState
from app.normalize.pricing import PACKAGE, PriceBasis

# What a caller may ask to see. "all" is the only value that lets an out-of-stock offer
# through, so no ranked surface shows one unless it was asked for by name.
AvailabilityFilter = Literal["in_stock", "out_of_stock", "unknown", "all"]
DEFAULT_AVAILABILITY: AvailabilityFilter = IN_STOCK


class HoursTodayOut(BaseModel):
    """What is true at this store right now, decided in the store's own timezone.

    `unknown` is a real answer, not a gap: several retailers publish no hours on any surface
    StoreSplit is allowed to read, and a shopper is better served by "Hours not published" than
    by a plausible-looking opening time nobody stated.
    """

    state: HoursState = "unknown"
    opens_at: str | None = None  # local wall clock, "08:00"
    closes_at: str | None = None
    opens_day: str | None = None  # "today", "tomorrow", or a weekday name
    # True only when the retailer published *this* date as one the store does not open at
    # all. A different sentence from "closed right now": "Closed today" ends the question,
    # where "Closed - opens 8:00 AM" answers it. A weekday nobody published is neither.
    closed_all_day: bool = False
    # The next opening as an absolute instant, for a store that is shut and has one. It is
    # what "everything near you is closed, here is what opens first" has to sort by:
    # `opens_at` is the wall clock to *print*, and two stores in different zones can print
    # the same one without meaning the same moment. Null while the store is open.
    next_open_at: datetime | None = None


class StoreOut(BaseModel):
    id: int
    retailer_slug: str
    retailer_name: str
    # The host this retailer's product pages live on, so a client can check a link belongs to
    # the retailer without keeping its own copy of the adapter hosts.
    retailer_host: str | None = None
    # Whether this retailer states per-store stock at all. It is what lets a client tell the
    # two kinds of `unknown` apart and word them differently: a retailer that publishes
    # inventory and gave an unreadable answer ("stock not confirmed") is not the same as one
    # that publishes none at all ("availability not published -- check in store"). It is a
    # fact about the retailer, so it travels with the store rather than with each offer, and
    # it changes no ranking: `unknown` is out of the comparison either way.
    stock_reporting: StockReporting = LIVE_STOCK
    name: str
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    # A Google Maps link to this exact store, built from coordinates or a street address the
    # retailer published, and absent when neither is known. Never a guessed location.
    maps_url: str | None = None
    hours_today: HoursTodayOut = HoursTodayOut()


class OfferOut(BaseModel):
    id: int
    store: StoreOut
    retailer_product_id: int
    retailer_sku: str
    title: str
    product_url: str | None = None
    image_url: str | None = None
    # The amount, and what it is an amount *of*. `price` is meaningless without
    # `price_basis`: `2.59` is a tray of chicken at `package` and a pound of it at `lb`, and
    # a client that assumes the first renders "$2.59 for the pack" over a per-pound rate.
    price: Decimal
    price_basis: PriceBasis = PACKAGE
    regular_price: Decimal
    loyalty_price: Decimal | None = None
    # What a variable-weight item can come to at most, where the retailer publishes it.
    # Absent means the retailer did not say; it is never derived from price x weight.
    max_total_price: Decimal | None = None
    # The weight span the retailer published for this package ("2.5-5.25 lb"). Null together
    # for a fixed package. A client shows the range instead of a single package price,
    # because there is no single package price to show.
    min_weight: Decimal | None = None
    max_weight: Decimal | None = None
    weight_unit: str | None = None
    currency: str
    # Closed on the wire as well as in the UI: a new state has to be added deliberately in
    # both places rather than silently rendering as an unlabelled offer.
    availability: Availability
    stock_status: str | None = None  # the retailer's own wording, for display and debugging
    # The store the retailer echoed back when it priced this offer; see `Offer.store_context`.
    store_context: str | None = None
    unit_price: Decimal | None = None
    unit_price_unit: str | None = None
    scraped_at: datetime
    is_cheapest_for_product: bool = False
    is_cheapest_overall: bool = False


class ProductOut(BaseModel):
    id: int
    category: str
    brand: str | None = None
    normalized_name: str
    quantity: Decimal | None = None
    quantity_unit: str | None = None
    count: int | None = None
    gtin: str | None = None
    comparison_unit: str
    comparison_quantity: Decimal | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    # A picture for the card: the best offer's, else the first offer that has one. Retailer
    # images are validated at ingest, so a value here is a real https image URL.
    image_url: str | None = None
    best_offer_id: int | None = None
    # The offer a collapsed card shows. Populated only when the product's leading offer is
    # `in_stock`: an unknown or out-of-stock price is not an answer to "what does this cost
    # today", so it never becomes the headline, in any filter.
    best_offer: OfferOut | None = None
    # What the card summarizes before it is expanded.
    offer_count: int = 0
    in_stock_offer_count: int = 0
    retailer_count: int = 0
    store_count: int = 0
    # The spread across the offers a shopper could act on, for "from $X" without expanding.
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    offers: list[OfferOut] = Field(default_factory=list)


class FreshnessOut(BaseModel):
    """How old these prices are, and what is being done about it.

    Stale data is answered with, not hidden: the search returns what it has and revalidates
    behind the answer, so `is_stale` means "a refresh is warranted", never "do not show
    this". `refreshing` is true when one is in flight -- started by this request, by an
    earlier one, or by somebody pressing the refresh button.
    """

    last_updated_at: datetime | None = None
    age_seconds: float | None = None
    ttl_seconds: int
    is_stale: bool = False
    refreshing: bool = False
    refresh_started_at: datetime | None = None
    refresh_finished_at: datetime | None = None
    # The one cooldown: automatic and manual refreshes share it, and the API enforces it.
    cooldown_seconds: int = 0
    # Deliberately relative, not an absolute timestamp: the client anchors it to its own
    # clock and counts down, so a skew between browser and server cannot show a wrong time.
    refresh_available_in_seconds: int = 0
    can_refresh: bool = True
    # Set when the last refresh failed or finished with failed retailers. The results shown
    # are still the last valid ones; this is a footnote, not a replacement for them.
    last_error: str | None = None


class PageOut(BaseModel):
    """Pagination by canonical product. An offer is never a page unit: paging by offer would
    cut a product's own offers across two pages and make the comparison meaningless."""

    page: int
    page_size: int
    total_products: int
    total_pages: int
    has_next: bool
    has_previous: bool


class SearchResponse(BaseModel):
    query: str
    zip_code: str
    category: str | None = None
    category_label: str | None = None
    comparison_unit: str | None = None
    availability: AvailabilityFilter = DEFAULT_AVAILABILITY
    # Whether the caller asked to compare only stores that are not shut right now. It is
    # echoed rather than assumed so a client renders what the server actually applied.
    #
    # `stores` is **not** narrowed by it, deliberately: it is the set of stores this ZIP
    # means, and a client needs the shut ones to say "everything near you is closed, Trader
    # Joe's opens at 8:00 AM". What the filter narrows is which stores' offers are compared.
    open_now: bool = False
    # Offers this query matched before the availability filter was applied. Zero means
    # nothing has been collected here yet; a non-zero value with no products means the
    # filter hid them all -- two situations that need opposite advice.
    offers_before_filter: int = 0
    stores: list[StoreOut]
    products: list[ProductOut]
    # Products whose only offers come from retailers that publish no stock at all -- Trader
    # Joe's sells nothing online and says nothing about its shelves, so every offer it has is
    # `unknown`. Filtering them out of the default view was right (they are not confirmed
    # buyable, and must never win a badge or a basket) but hiding them entirely was not: the
    # price is real, the link is real, and a shopper comparing eggs wants to know Trader
    # Joe's sells them. They are returned *beside* the confirmed results, never mixed in, so
    # a client can show them under their own heading and label them honestly.
    # Only populated for the default in-stock view; an explicit filter gets what it asked for.
    unknown_products: list[ProductOut] = []
    cheapest_offer_id: int | None = None
    last_updated_at: datetime | None = None
    freshness: FreshnessOut | None = None
    page: PageOut | None = None


class ProductOffersResponse(BaseModel):
    product: ProductOut
    availability: AvailabilityFilter = "all"


class PriceObservationOut(BaseModel):
    """One price, as it stood at one moment, at one store.

    Both figures are present because they answer different questions and neither can be
    recovered from the other. `unit_price` is the normalized comparison price and is what a
    chart plots; `price` is the amount the retailer quotes and `price_basis` says what that
    amount buys -- a package, a pound, an ounce or one of something.
    """

    scraped_at: datetime
    price: Decimal
    regular_price: Decimal
    loyalty_price: Decimal | None = None
    unit_price: Decimal | None = None
    # Null means the row predates the column and its offer is gone, so nobody recorded what
    # the amount was quoted per. It is not `package`: a client prints that as "for the pack",
    # and over a per-pound rate that is the one sentence this API must never produce. A
    # client shows the amount alone when this is null.
    price_basis: PriceBasis | None = None
    unit_price_unit: str | None = None
    # True for the one observation that predates the requested window. It is returned so a
    # line has a value to start from -- a price that last moved before the window still has
    # to be drawn across it -- and flagged so a client does not present it as something that
    # happened inside the range the shopper asked for.
    before_window: bool = False


class PriceSeriesOut(BaseModel):
    """One retailer's own SKU at one physical store: the only thing that has a price series.

    Never a canonical product and never a retailer: two branches of one chain price
    differently, and a line averaging them would show a price nobody was charged.
    """

    retailer_product_id: int
    retailer_sku: str
    retailer_slug: str
    retailer_name: str
    store_id: int
    store_name: str
    store_city: str | None = None
    # The price being charged now, and when it was last confirmed. Null when the offer has
    # been expired: the product is no longer listed at this store, and its last known price
    # must not be drawn forward to today.
    current: PriceObservationOut | None = None
    # Whether that current price is one a shopper can act on, and whether this retailer
    # states stock at all -- the same two facts every other surface uses to decide its
    # wording. They travel with the series because the rest of the app refuses to *lead*
    # with an offer nobody has confirmed, and a chart that quietly did would be the one
    # screen where an unbuyable price is the headline. Null when there is no current price.
    availability: Availability | None = None
    stock_reporting: StockReporting = LIVE_STOCK
    # Oldest first. Each price holds until the next observation, so these are steps, not
    # samples of a continuous quantity.
    points: list[PriceObservationOut] = []
    # Set when this series had more observations in the window than one response returns,
    # so a client does not describe its first point as the earliest price collected.
    truncated: bool = False


class PriceHistoryResponse(BaseModel):
    product_id: int
    product_name: str
    category: str
    # What this product is compared per -- "lb", "egg", "gal". The y axis's title, and the
    # unit for any observation written before history rows carried their own label.
    comparison_unit: str | None = None
    currency: str = "USD"
    days: int
    since: datetime
    now: datetime
    series: list[PriceSeriesOut] = []


# A US ZIP, optionally with its +4. Anything else is not a place, and letting it through
# would make every distinct string a distinct refresh key -- an unbounded, uncooled key
# space in front of an endpoint that collects from ten real retailers.
ZIP_PATTERN = r"^\d{5}(-\d{4})?$"


class RefreshRequest(BaseModel):
    """Ask for the prices behind one search to be collected again.

    `query` is the search the shopper is looking at; it resolves to a category so the
    refresh scrapes only what is on screen. Without one -- or with a query that matches no
    staple -- the refresh covers every category, which is what an empty ZIP needs.
    """

    zip_code: str = Field(min_length=5, max_length=10, pattern=ZIP_PATTERN)
    query: str | None = Field(default=None, max_length=100)


class RefreshResponse(BaseModel):
    """The outcome of asking, and the freshness picture either way.

    `already_running` and `cooling_down` are answers, not errors: the request was correct and
    the API declined to start a second scrape. Nothing is collected in either case, which is
    the enforcement -- a reload or a second tab cannot shorten the cooldown.
    """

    state: Literal["started", "already_running", "cooling_down"]
    zip_code: str
    category: str | None = None
    category_label: str | None = None
    freshness: FreshnessOut


class ScrapeRequest(BaseModel):
    zip_code: str = Field(default="94105", min_length=5, max_length=10, pattern=ZIP_PATTERN)
    retailers: list[str] | None = Field(default=None, description="Adapter slugs; default all")
    categories: list[str] | None = Field(default=None, description="Category keys; default all")


class ScrapeRunOut(BaseModel):
    id: int
    retailer_slug: str
    zip_code: str
    categories: list[str]
    status: str
    products_seen: int
    offers_written: int
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ScrapeResponse(BaseModel):
    runs: list[ScrapeRunOut]


class BasketItemIn(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(default="count", max_length=20)


class BasketRequest(BaseModel):
    zip_code: str = Field(min_length=5, max_length=10, pattern=ZIP_PATTERN)
    items: list[BasketItemIn] = Field(min_length=1, max_length=30)
    # A basket recommends where to shop, so it considers only offers a shopper can buy.
    availability: AvailabilityFilter = DEFAULT_AVAILABILITY
    # And, when asked, only stores that are not shut right now. A basket is a trip, and a
    # trip to a closed shop is not a saving.
    open_now: bool = False


class BasketLineOut(BaseModel):
    query: str
    category: str
    requested_quantity: Decimal
    requested_unit: str
    needed_quantity: Decimal
    comparison_unit: str
    product_id: int
    product_name: str
    brand: str | None = None
    offer: OfferOut
    packs: int
    line_total: Decimal


class StoreBasketOut(BaseModel):
    store: StoreOut
    total: Decimal
    covers_all_items: bool
    missing_items: list[str]
    lines: list[BasketLineOut]


class SplitBasketOut(BaseModel):
    total: Decimal
    stores: list[StoreOut]
    lines: list[BasketLineOut]


class BasketItemResult(BaseModel):
    query: str
    category: str
    category_label: str
    needed_quantity: Decimal
    comparison_unit: str
    matching_products: int
    cheapest: BasketLineOut | None = None
    options: list[BasketLineOut] = Field(
        default_factory=list
    )  # best line per store, cheapest first


class BasketResponse(BaseModel):
    zip_code: str
    availability: AvailabilityFilter = DEFAULT_AVAILABILITY
    # Echoed like the search's, and with the same rule: `stores` stays the ZIP's whole set
    # so a client can name what is shut and when it opens, while the comparison itself ran
    # over the stores this left in.
    open_now: bool = False
    stores: list[StoreOut]
    items: list[BasketItemResult]
    single_store_options: list[StoreBasketOut]
    cheapest_single_store: StoreBasketOut | None = None
    cheapest_split: SplitBasketOut | None = None
    savings: Decimal | None = None
    savings_percent: Decimal | None = None
    last_updated_at: datetime | None = None
    oldest_updated_at: datetime | None = None


class ZipLookupOut(BaseModel):
    """The ZIP whose Census centroid is **nearest** to a pair of coordinates.

    Nearest centroid, not the ZCTA the point lies inside: this is the ZIP whose centroid
    store distances are then measured from, so it is the point a shopper's stores should be
    ranked around. The Ferry Building stands in 94111 and resolves to 94105.

    `distance_miles` is the gap between the shopper's point and the ZCTA's internal point,
    not an error bar on the ZIP itself. It is published because it is the one number that
    says how confident the answer is: a hundred yards is a shopper standing in the ZIP, and
    forty miles is the nearest postcode to somewhere that has none.
    """

    zip_code: str
    latitude: float
    longitude: float
    distance_miles: float
