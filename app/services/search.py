"""Product search: canonical products with their current offers near a ZIP code.

The unit of a result is a **canonical product**, never an offer. One product is one card and
one page slot however many stores carry it, and its offers -- every retailer, every store --
are grouped inside it. Paging by offer would cut a product's own offers across two pages and
make the comparison it exists for meaningless.

The page is built with a fixed number of statements, whatever the result size:

1. one aggregate for how fresh the data is and how many offers exist before filtering,
2. one `COUNT` over the grouped products, for the page count,
3. one grouped, ordered, `LIMIT`-ed query for this page's product ids,
4. one query for those products' offers, with stores, retailers and retailer products
   eagerly loaded,
5. one for the single cheapest offer across the whole result, so the "Cheapest" badge means
   cheapest for the search rather than cheapest on the page you happen to be reading.

It used to load every offer for the category at every nearby store, group them in Python and
throw most of them away, and count by materializing rows only to call `len()` on them.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import CanonicalProduct, Offer, RetailerProduct, Store
from app.normalize.availability import IN_STOCK, UNKNOWN, as_availability, availability_rank
from app.normalize.categories import CATEGORIES, Category, category_for_query
from app.normalize.pricing import normalize_basis
from app.retailers.clients import RetailerClients
from app.schemas import (
    DEFAULT_AVAILABILITY,
    AvailabilityFilter,
    OfferOut,
    PageOut,
    ProductOffersResponse,
    ProductOut,
    SearchResponse,
)
from app.services.freshness import freshness_out
from app.services.refresh import RefreshStatus, refresh_key, registry
from app.services.stores import open_now_stores, store_out, stores_near

# How many unknown-stock products ride along under the confirmed results. Enough to show a
# shopper that Trader Joe's sells the thing and what it charges; not so many that a section
# nobody can act on outgrows the comparison itself.
UNKNOWN_SECTION_LIMIT = 12
# Products per page. Twenty compact cards is about two screens; sixty is the ceiling so one
# request cannot ask for the whole catalogue.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 60


@dataclass(frozen=True)
class RefreshContext:
    """What a search needs to be able to revalidate behind its own answer.

    Held by the API layer and passed in, rather than reached for here: a search called from
    a test or a script has no HTTP client pool and must not start one.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    clients: RetailerClients


def offer_sort_key(offer: Offer) -> tuple:
    """Buyable first, then cheapest.

    The first offer in this order is the one that gets the "cheapest" badge, so an offer the
    shopper cannot buy must not lead it: a $1.99 carton that is out of stock is not a better
    answer than a $4.99 one on the shelf. Unit price decides within a state; price is the
    fallback so offers without a unit price still order.
    """
    return (
        availability_rank(offer.availability),
        offer.unit_price is None,
        offer.unit_price or offer.price,
        offer.price,
        offer.id,
    )


def offer_out(offer: Offer) -> OfferOut:
    rp = offer.retailer_product
    return OfferOut(
        id=offer.id,
        store=store_out(offer.store),
        retailer_product_id=rp.id,
        retailer_sku=rp.retailer_sku,
        title=rp.title,
        product_url=rp.product_url,
        image_url=rp.image_url,
        price=offer.price,
        price_basis=normalize_basis(offer.price_basis),
        regular_price=offer.regular_price,
        loyalty_price=offer.loyalty_price,
        max_total_price=offer.max_total_price,
        min_weight=rp.min_weight,
        max_weight=rp.max_weight,
        weight_unit=rp.weight_unit,
        currency=offer.currency,
        availability=as_availability(offer.availability),
        stock_status=offer.stock_status,
        unit_price=offer.unit_price,
        unit_price_unit=offer.unit_price_unit,
        scraped_at=offer.scraped_at,
    )


def _product_out(product: CanonicalProduct, offers: list[Offer]) -> ProductOut:
    ordered = sorted(offers, key=offer_sort_key)
    outs = [offer_out(o) for o in ordered]
    # "Best for this product" is a recommendation, so it is only ever given to an offer the
    # shopper can actually buy. A product whose offers are all unknown or out of stock gets
    # no badge and no `best_offer_id` at all -- an honest absence rather than a pick from a
    # field of things that might not be there.
    buyable = bool(outs) and outs[0].availability == IN_STOCK
    if buyable:
        outs[0].is_cheapest_for_product = True
    best = outs[0] if buyable else None
    in_stock = [o for o in outs if o.availability == IN_STOCK]
    # The headline range describes what can be bought. With nothing buyable there is no
    # honest range to quote, so the card falls back to the whole spread and stays unbadged.
    priced = in_stock or outs
    return ProductOut(
        id=product.id,
        category=product.category,
        brand=product.brand,
        normalized_name=product.normalized_name,
        quantity=product.quantity,
        quantity_unit=product.quantity_unit,
        count=product.count,
        gtin=product.gtin,
        comparison_unit=product.comparison_unit,
        comparison_quantity=product.comparison_quantity,
        attributes=product.attributes or {},
        image_url=_product_image(best, outs),
        best_offer_id=best.id if best else None,
        best_offer=best,
        offer_count=len(outs),
        in_stock_offer_count=len(in_stock),
        retailer_count=len({o.store.retailer_slug for o in outs}),
        store_count=len({o.store.id for o in outs}),
        price_low=min((o.price for o in priced), default=None),
        price_high=max((o.price for o in priced), default=None),
        offers=outs,
    )


def _product_image(best: OfferOut | None, outs: list[OfferOut]) -> str | None:
    """The card's picture: the best offer's, else the first offer that has one.

    Retailers photograph the same carton differently and some publish no image at all, so a
    product is only pictureless when none of its offers carries one. Values are validated at
    ingest, so anything here is a real https image URL.
    """
    if best is not None and best.image_url:
        return best.image_url
    return next((o.image_url for o in outs if o.image_url), None)


def _offers_query(store_ids: list[int], availability: AvailabilityFilter):
    """Offers at these stores, restricted to one availability state unless asked for all.

    The filter belongs in the query, not in the caller: cheapest flags, ordering and the
    result limit are all computed from what comes back, so an offer that is filtered out
    must never have been ranked in the first place.
    """
    stmt = (
        select(Offer)
        .join(Offer.retailer_product)
        .join(RetailerProduct.canonical_product)
        .where(Offer.store_id.in_(store_ids))
        .options(
            selectinload(Offer.store).selectinload(Store.retailer),
            selectinload(Offer.retailer_product).selectinload(RetailerProduct.canonical_product),
        )
    )
    if availability != "all":
        stmt = stmt.where(Offer.availability == availability)
    return stmt


async def offers_for_category(
    db: AsyncSession,
    category_key: str,
    store_ids: list[int],
    availability: AvailabilityFilter = DEFAULT_AVAILABILITY,
) -> list[Offer]:
    if not store_ids:
        return []
    stmt = _offers_query(store_ids, availability).where(CanonicalProduct.category == category_key)
    return list(await db.scalars(stmt))


# -- the grouped query the page is built from ----------------------------------------------


def _match(
    stmt: Select,
    store_ids: list[int],
    availability: AvailabilityFilter,
    query: str,
    category: Category | None,
) -> Select:
    """The `WHERE` every statement on this page shares: which stores, which staple, which
    availability. One owner, so a count and its page can never disagree about what matched."""
    stmt = stmt.where(Offer.store_id.in_(store_ids))
    if availability != "all":
        stmt = stmt.where(Offer.availability == availability)
    if category is not None:
        return stmt.where(CanonicalProduct.category == category.key)
    pattern = f"%{query.strip().lower()}%"
    return stmt.where(
        CanonicalProduct.normalized_name.like(pattern) | RetailerProduct.title.ilike(pattern)
    )


def _from_offers(*columns) -> Select:
    return (
        select(*columns)
        .select_from(Offer)
        .join(Offer.retailer_product)
        .join(RetailerProduct.canonical_product)
    )


# What an offer costs for ranking: unit price where the package size is known, price
# otherwise, so an offer without a parseable size still orders instead of sorting last.
_EFFECTIVE = func.coalesce(Offer.unit_price, Offer.price)
# The cheapest thing a shopper could actually buy for this product. NULL when nothing is
# in stock, which is exactly the products that must rank below the ones that are.
_BEST_IN_STOCK = func.min(case((Offer.availability == IN_STOCK, _EFFECTIVE), else_=None))
_BEST_ANY = func.min(_EFFECTIVE)


def _ranked_products(
    store_ids: list[int], availability: AvailabilityFilter, query: str, category: Category | None
) -> Select:
    """Canonical products for this search, buyable-cheapest first.

    Ordering happens in SQL over the aggregate, so `LIMIT`/`OFFSET` can be trusted: the page
    is the page of the real ranking, not of whatever rows the database returned first.
    `_BEST_IN_STOCK IS NULL` sorts false before true on both SQLite and PostgreSQL, which
    puts every product with something on a shelf above every product without.
    """
    stmt = _from_offers(CanonicalProduct.id)
    stmt = _match(stmt, store_ids, availability, query, category)
    return stmt.group_by(CanonicalProduct.id).order_by(
        _BEST_IN_STOCK.is_(None),
        _BEST_IN_STOCK,
        _BEST_ANY,
        CanonicalProduct.id,
    )


async def _count_products(
    db: AsyncSession,
    store_ids: list[int],
    availability: AvailabilityFilter,
    query: str,
    category: Category | None,
) -> int:
    grouped = _match(
        _from_offers(CanonicalProduct.id), store_ids, availability, query, category
    ).group_by(CanonicalProduct.id)
    return await db.scalar(select(func.count()).select_from(grouped.subquery())) or 0


async def _load_page(
    db: AsyncSession,
    product_ids: list[int],
    store_ids: list[int],
    availability: AvailabilityFilter,
) -> list[ProductOut]:
    """This page's products with their offers, in the ranking's order.

    One query for the offers, with the store, its retailer and the retailer product eagerly
    loaded, so rendering a page of twenty products touches the database a fixed number of
    times rather than once per offer.
    """
    if not product_ids:
        return []
    stmt = _offers_query(store_ids, availability).where(
        RetailerProduct.canonical_product_id.in_(product_ids)
    )
    offers = list(await db.scalars(stmt))

    grouped: dict[int, list[Offer]] = {pid: [] for pid in product_ids}
    products: dict[int, CanonicalProduct] = {}
    for offer in offers:
        product = offer.retailer_product.canonical_product
        if product is None or product.id not in grouped:
            continue
        products[product.id] = product
        grouped[product.id].append(offer)
    # The SQL ranking decides the order; re-sorting here would let the page disagree with
    # the page count and make a product appear on two pages or on none.
    return [_product_out(products[pid], grouped[pid]) for pid in product_ids if pid in products]


async def _cheapest_offer_id(
    db: AsyncSession,
    store_ids: list[int],
    availability: AvailabilityFilter,
    query: str,
    category: Category | None,
) -> int | None:
    """The single cheapest buyable offer across the whole search, not just this page.

    A page-local minimum would badge one offer "Cheapest" on page one and a dearer one on
    page two, both truthfully and both wrong. Only `in_stock` is eligible, in every filter.
    """
    stmt = _match(_from_offers(Offer.id), store_ids, availability, query, category)
    stmt = stmt.where(Offer.availability == IN_STOCK)
    return await db.scalar(
        stmt.order_by(Offer.unit_price.is_(None), _EFFECTIVE, Offer.price, Offer.id).limit(1)
    )


async def _unknown_products(
    db: AsyncSession,
    query: str,
    category: Category | None,
    store_ids: list[int],
    limit: int,
) -> list[ProductOut]:
    """The same search again, over products whose stock no retailer published.

    Kept apart from the confirmed results on purpose. These are real products at real prices
    with real links -- Trader Joe's genuinely sells eggs -- but nobody has said they are on
    the shelf today, so they are shown under their own heading, carry no "best" badge, and
    are never what a basket is built from unless somebody asks for them by name.

    A product with *any* confirmed offer is excluded in SQL rather than by subtracting the
    page that happens to be on screen. Excluding only the current page would put a product
    ranked past position twenty into both lists at once -- unbadged and captioned "cannot
    confirm these are on the shelf" here, and showing a confirmed in-stock price four pages
    later. The section means "nobody publishes stock for this", which is a fact about the
    product, not about which page you are reading.
    """
    confirmed = (
        _match(_from_offers(CanonicalProduct.id), store_ids, IN_STOCK, query, category)
        .group_by(CanonicalProduct.id)
        .subquery()
    )
    ranked = (
        _ranked_products(store_ids, UNKNOWN, query, category)
        .where(CanonicalProduct.id.not_in(select(confirmed.c.id)))
        .limit(limit)
    )
    product_ids = list(await db.scalars(ranked))
    return await _load_page(db, product_ids, store_ids, UNKNOWN)


# -- freshness -----------------------------------------------------------------------------


async def _revalidate(
    db: AsyncSession,
    context: RefreshContext | None,
    zip_code: str,
    category: Category | None,
    is_stale: bool,
) -> RefreshStatus | None:
    """Start a background refresh when the data behind this answer has gone stale.

    Stale-while-revalidate: the caller already has its results and is not waiting on this.
    A query that matched no staple is not refreshed automatically -- there is no category to
    scope a scrape to, and scraping all seven because somebody typed a word would be a
    minute of retailer traffic nobody asked for. The manual button still covers that case.
    """
    settings = get_settings()
    if context is None or not settings.search_auto_refresh or not is_stale or category is None:
        return None
    _, status = await registry.ensure_refresh(
        db, context.sessionmaker, context.clients, refresh_key(zip_code, category.key)
    )
    return status


async def search_products(
    db: AsyncSession,
    query: str,
    zip_code: str,
    limit: int | None = None,
    availability: AvailabilityFilter = DEFAULT_AVAILABILITY,
    open_now: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    refresh_context: RefreshContext | None = None,
) -> SearchResponse:
    """One page of canonical products near a ZIP, with every offer grouped inside each.

    `limit` is the older whole-result cap and still works: it sets the page size.
    """
    if limit is not None:
        page_size = limit
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    stores = await stores_near(db, zip_code)
    # "Open now" narrows which stores the comparison runs over, and nothing else. Every
    # query below is already scoped by `store_ids`, so the filter needs no clause of its
    # own -- and, just as importantly, it cannot reorder anything: offers stay ranked
    # in-stock-first then by unit price, so the cheapest badge still means cheapest.
    #
    # `stores` keeps the ZIP's whole set. A client that filtered to nothing has to be able
    # to say *which* shops are shut and when they open, and that answer is in these rows.
    compared = open_now_stores(stores) if open_now else stores
    store_ids = [s.id for s in compared]
    category = category_for_query(query)

    last_updated: datetime | None = None
    offers_before_filter = 0
    total_products = 0
    outs: list[ProductOut] = []
    cheapest_id: int | None = None

    if store_ids:
        # One row answering both "how old is this?" and "did the filter hide everything?".
        # Freshness ignores the availability filter on purpose: how recently the prices were
        # collected is a fact about the scrape, not about what the shopper chose to look at.
        stmt = _match(
            _from_offers(func.max(Offer.scraped_at), func.count(Offer.id)),
            store_ids,
            "all",
            query,
            category,
        )
        last_updated, offers_before_filter = (await db.execute(stmt)).one()
        offers_before_filter = offers_before_filter or 0

        total_products = await _count_products(db, store_ids, availability, query, category)
        ranked = _ranked_products(store_ids, availability, query, category)
        product_ids = list(await db.scalars(ranked.limit(page_size).offset((page - 1) * page_size)))
        outs = await _load_page(db, product_ids, store_ids, availability)

        # Across categories unit prices are not comparable, so "cheapest" needs a category.
        if category is not None:
            cheapest_id = await _cheapest_offer_id(db, store_ids, availability, query, category)

    for product in outs:
        for offer in product.offers:
            offer.is_cheapest_overall = offer.id == cheapest_id
        if product.best_offer is not None:
            product.best_offer.is_cheapest_overall = product.best_offer.id == cheapest_id

    # Beside the confirmed results, never among them: the retailers that publish no stock.
    # Page one only -- a footnote that repeated under every page would stop being one.
    unknown_outs: list[ProductOut] = []
    if availability == IN_STOCK and store_ids and page == 1:
        unknown_outs = await _unknown_products(
            db, query, category, store_ids, UNKNOWN_SECTION_LIMIT
        )

    total_pages = max(1, -(-total_products // page_size))
    key = refresh_key(zip_code, category.key if category else None)
    freshness = freshness_out(last_updated, await registry.status(db, key))
    # Stale-while-revalidate: the results above are already final for this request. Starting
    # a refresh only changes what the *next* one will see, and what this response reports.
    started = await _revalidate(db, refresh_context, zip_code, category, freshness.is_stale)
    if started is not None:
        freshness = freshness_out(last_updated, started)

    return SearchResponse(
        query=query,
        zip_code=zip_code,
        category=category.key if category else None,
        category_label=category.label if category else None,
        comparison_unit=category.comparison_label if category else None,
        availability=availability,
        open_now=open_now,
        offers_before_filter=offers_before_filter,
        stores=[store_out(s) for s in stores],
        products=outs,
        unknown_products=unknown_outs,
        cheapest_offer_id=cheapest_id,
        last_updated_at=last_updated,
        freshness=freshness,
        page=PageOut(
            page=page,
            page_size=page_size,
            total_products=total_products,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        ),
    )


async def product_offers(
    db: AsyncSession,
    product_id: int,
    availability: AvailabilityFilter = "all",
    open_now: bool = False,
) -> ProductOffersResponse | None:
    """One product's offers. This is a drill-down, so it shows every state by default and
    labels them, rather than hiding an offer the shopper came here to look at.

    `open_now` narrows it the same way it narrows the search this was opened from: a
    drill-down that contradicted the list above it about which shops are shut would be
    worse than either answer alone. It takes no ZIP, so the stores are the ones already
    carrying an offer for this product.
    """
    product = await db.get(CanonicalProduct, product_id)
    if product is None:
        return None
    stmt = (
        select(Offer)
        .join(Offer.retailer_product)
        .where(RetailerProduct.canonical_product_id == product_id)
        .options(
            selectinload(Offer.store).selectinload(Store.retailer),
            selectinload(Offer.retailer_product),
        )
    )
    if availability != "all":
        stmt = stmt.where(Offer.availability == availability)
    offers = list(await db.scalars(stmt))
    if open_now:
        keep = {s.id for s in open_now_stores([o.store for o in offers])}
        offers = [o for o in offers if o.store_id in keep]
    out = _product_out(product, offers)
    for offer in out.offers:
        offer.is_cheapest_overall = offer.id == out.best_offer_id
    # Price history used to ride along here as a flat list of points capped at 200 rows
    # across every store at once -- which mixed stores into one undistinguishable sequence,
    # had no time range, and silently dropped the older half of a busy product. It has its
    # own endpoint now (`services/price_history.py`), where a series is a store.
    return ProductOffersResponse(product=out, availability=availability)


def category_label(key: str) -> str:
    return CATEGORIES[key].label if key in CATEGORIES else key
