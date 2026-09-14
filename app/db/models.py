"""Database schema.

Column types are kept portable (String/Numeric/JSON/DateTime) so the same models run on
PostgreSQL in Docker and on SQLite in fast unit tests.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.normalize.availability import UNKNOWN
from app.normalize.pricing import PACKAGE


def utcnow() -> datetime:
    return datetime.now(UTC)


# How much of a retailer's own stock wording `offers.stock_status` holds. Named because the
# ingest bounds what it writes to it, and the two must not drift: the wording is quotable
# evidence for a verdict, so a value too long for the column would otherwise fail the insert
# and take a whole batch of offers with it. Wide enough that every wording a registered
# adapter can build fits whole -- Whole Foods' documented `OUT_OF_STOCK_ONLINE` already makes
# `availability=OUT_OF_STOCK_ONLINE`, 32 characters -- so the bound is a backstop and not a
# thing that fires. A truncated diagnostic is a token no payload contained.
STOCK_STATUS_MAX = 60


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Retailer(Base):
    __tablename__ = "retailers"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    stores: Mapped[list["Store"]] = relationship(back_populates="retailer")


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("retailer_id", "external_id", name="uq_store_retailer_external"),
        Index("ix_stores_zip_code", "zip_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_id: Mapped[int] = mapped_column(ForeignKey("retailers.id"))
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(50))
    zip_code: Mapped[str | None] = mapped_column(String(10))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    # The store's own telephone number as the retailer publishes it, normalized to E.164
    # ("+19257548824"). NULL where the retailer states none; it is never inferred from an
    # area code or a sibling store.
    phone: Mapped[str | None] = mapped_column(String(32))
    # IANA name, from the retailer. Opening hours mean nothing without it: "closes at 22:00"
    # is a wall clock at the store, not an instant, and reading it in the server's zone puts
    # a California store eight hours out.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # `{"weekly": {"0": {"opens": "08:00", "closes": "22:00"}, ...}, "dates": {...}}`, shaped
    # by `app/normalize/hours.py`. NULL means the retailer publishes none on a surface this
    # is allowed to read -- Raley's, for one -- and the UI says so rather than inventing any.
    hours: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    hours_source: Mapped[str | None] = mapped_column(String(100))
    hours_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A Google Maps link to this *business*, not to the ground it stands on. It is resolved
    # once and kept, never built per request: identifying a place is a lookup, and a search
    # page renders dozens of offers across a handful of stores.
    #
    # NULL is the ordinary state and means no specific place has been verified for this
    # store; `services/maps.py` then falls back to an address search naming the retailer.
    # A wrong place is far worse than a search: "285 Winston Dr" sends a shopper to the
    # right doorway, while the wrong Target sends them across the city with full confidence.
    maps_place_url: Mapped[str | None] = mapped_column(String(500))
    # The place's identifier where there is one -- Target publishes `google_cid`, the Places
    # API returns a `place_id`. Kept beside the URL so a link can be rebuilt without a second
    # lookup, and so it is visible which stores were resolved rather than searched for.
    maps_place_id: Mapped[str | None] = mapped_column(String(128))
    # Which surface produced it, e.g. `target:sl-page/google_cid`. A link with no provenance
    # cannot be audited, and this column is what `scripts/audit_store_maps.py` reports on.
    maps_source: Mapped[str | None] = mapped_column(String(100))
    maps_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ZIP codes whose scrapes discovered this store (retailer locators may cross ZIP prefixes).
    served_zip_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    retailer: Mapped[Retailer] = relationship(back_populates="stores")


class CanonicalProduct(Base):
    """A comparable product: brand + normalized name + one specific package size."""

    __tablename__ = "canonical_products"
    __table_args__ = (
        Index("ix_canonical_products_category", "category"),
        Index("ix_canonical_products_gtin", "gtin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50))
    brand: Mapped[str | None] = mapped_column(String(100))
    normalized_name: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    quantity_unit: Mapped[str | None] = mapped_column(String(20))
    count: Mapped[int | None] = mapped_column(Integer)
    gtin: Mapped[str | None] = mapped_column(String(20))
    comparison_unit: Mapped[str] = mapped_column(String(20))
    comparison_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    retailer_products: Mapped[list["RetailerProduct"]] = relationship(
        back_populates="canonical_product"
    )


class RetailerProduct(Base):
    __tablename__ = "retailer_products"
    __table_args__ = (
        UniqueConstraint("retailer_id", "retailer_sku", name="uq_retailer_product_sku"),
        Index("ix_retailer_products_canonical", "canonical_product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_id: Mapped[int] = mapped_column(ForeignKey("retailers.id"))
    canonical_product_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_products.id"))
    retailer_sku: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    brand_raw: Mapped[str | None] = mapped_column(String(100))
    product_url: Mapped[str | None] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(String(500))
    gtin: Mapped[str | None] = mapped_column(String(20))
    size_text: Mapped[str | None] = mapped_column(String(50))
    # The span a variable-weight package is sold within, as the retailer published it
    # ("2.5-5.25lbs"). All three are NULL together for a fixed package, which is most
    # products. They are kept beside the price rather than folded into it: a tray with no
    # single weight has no single size, and the upper end of its range is exactly the number
    # that used to be divided into an already-per-pound price.
    min_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    max_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    weight_unit: Mapped[str | None] = mapped_column(String(20))
    match_status: Mapped[str] = mapped_column(String(20), default="new")
    match_confidence: Mapped[float | None]
    match_candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    scrape_source: Mapped[str] = mapped_column(String(100))
    last_scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    retailer: Mapped[Retailer] = relationship()
    canonical_product: Mapped[CanonicalProduct | None] = relationship(
        back_populates="retailer_products"
    )
    offers: Mapped[list["Offer"]] = relationship(back_populates="retailer_product")


class Offer(Base):
    """The current offer for one retailer product at one store."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("retailer_product_id", "store_id", name="uq_offer_product_store"),
        Index("ix_offers_store", "store_id"),
        Index("ix_offers_availability", "availability"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_product_id: Mapped[int] = mapped_column(ForeignKey("retailer_products.id"))
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    regular_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    loyalty_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # What `price` is quoted *per*: `package` (a total for one package), `lb`, `oz` or
    # `each`. Stored rather than re-derived, because the answer is a statement the retailer
    # made at scrape time and nothing downstream can recover it from the number alone --
    # `2.59` is a fair price for a tray of chicken and a fair rate for a pound of it, and
    # reading the wrong one is a factor-of-five error in the direction of looking cheapest.
    price_basis: Mapped[str] = mapped_column(String(10), default=PACKAGE, server_default=PACKAGE)
    # The most the retailer says this offer can come to, where it publishes one (Target's
    # `formatted_max_item_price`). Never computed from the price and a weight.
    max_total_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Normalized to in_stock / out_of_stock / unknown at ingest; `stock_status` keeps the
    # retailer's own wording next to it so a surprising state can be traced to the payload.
    # An offer whose retailer said nothing is `unknown`, never `in_stock`.
    availability: Mapped[str] = mapped_column(String(20), default=UNKNOWN, server_default=UNKNOWN)
    stock_status: Mapped[str | None] = mapped_column(String(STOCK_STATUS_MAX))
    # The store the retailer itself said it was answering for -- Whole Foods' `storeId`,
    # Raley's `currentStoreNumber`. The scrape refuses a listing whose echo disagrees with
    # the store it asked about, so this column is proof rather than decoration.
    store_context: Mapped[str | None] = mapped_column(String(64))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit_price_unit: Mapped[str | None] = mapped_column(String(20))
    scrape_source: Mapped[str] = mapped_column(String(100))
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    retailer_product: Mapped[RetailerProduct] = relationship(back_populates="offers")
    store: Mapped[Store] = relationship()


class PriceHistory(Base):
    """What one retailer product cost at one store, each time the price was seen to change.

    Append-only, and never rewritten: `scraped_at` is when the collection that observed this
    price ran, so a later refresh adds a row or adds nothing. A row is written only when a
    scrape produced a price that differs from the newest row for the same
    (retailer product, store) -- see `services/scraper.record_price_history` -- so a series
    is a list of *changes*, and a price holds from its own row until the next one. The
    identity is the pair, never the canonical product: two stores of the same retailer are
    two series, and merging them would invent a price nobody charged.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        # One series is (retailer product, store) read in time order, which is exactly this
        # index: the equality columns first, then the range column the window filters on.
        Index("ix_price_history_series", "retailer_product_id", "store_id", "scraped_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_product_id: Mapped[int] = mapped_column(ForeignKey("retailer_products.id"))
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    regular_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    loyalty_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # The same two labels `offers` carries, copied rather than joined to: a history row is
    # read years after the offer that produced it has been overwritten or deleted, and
    # `2.59` on its own cannot be read back into "per pound" or plotted against "$/lb".
    # They are part of the change test too -- a pack that shrinks from 16 oz to 12 oz at the
    # same $3.99 is a real price change and used to record nothing.
    #
    # **Nullable, and unlike `offers` it does not default to `package`.** On an offer,
    # "nobody said" really is a package total -- that is what a retailer publishes unless it
    # says otherwise, and the adapter is there to have asked. On a history row backfilled
    # years later there was nobody to ask, and `package` is not an absence: a reader renders
    # it as "$2.59 for the pack", which over a per-pound rate is exactly the sentence this
    # schema exists to stop. NULL says "not recorded", which is the truth and is not
    # recoverable once a default has overwritten it. Every row a scrape writes has a basis.
    price_basis: Mapped[str | None] = mapped_column(String(10))
    unit_price_unit: Mapped[str | None] = mapped_column(String(20))
    scrape_source: Mapped[str] = mapped_column(String(100))
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_slug: Mapped[str] = mapped_column(String(50))
    zip_code: Mapped[str] = mapped_column(String(10))
    categories: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="running")
    products_seen: Mapped[int] = mapped_column(Integer, default=0)
    offers_written: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
