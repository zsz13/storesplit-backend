from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_http_clients, optional_http_clients
from app.db.session import get_db, get_sessionmaker
from app.normalize.categories import category_for_query
from app.retailers.clients import RetailerClients
from app.schemas import (
    DEFAULT_AVAILABILITY,
    ZIP_PATTERN,
    AvailabilityFilter,
    PriceHistoryResponse,
    ProductOffersResponse,
    RefreshRequest,
    RefreshResponse,
    SearchResponse,
)
from app.services.freshness import freshness_out, last_updated_for
from app.services.price_history import (
    DEFAULT_RANGE_DAYS,
    MAX_RANGE_DAYS,
    product_price_history,
)
from app.services.refresh import ALL_CATEGORIES, refresh_key, registry
from app.services.search import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    RefreshContext,
    product_offers,
    search_products,
)

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(min_length=1, max_length=100),
    zip_code: str = Query(min_length=5, max_length=10, pattern=ZIP_PATTERN),
    availability: AvailabilityFilter = Query(
        default=DEFAULT_AVAILABILITY,
        description="Which offers to return; defaults to in_stock only.",
    ),
    open_now: bool = Query(
        default=False,
        description=(
            "Compare only stores that are not shut right now, decided in each store's own "
            "timezone. A store whose hours nobody publishes is kept, because unknown hours "
            "are not evidence of a closed door; `stores` is never narrowed, so a caller can "
            "still see which shops are shut and when they next open."
        ),
    ),
    page: int = Query(default=1, ge=1, description="Page of canonical products, 1-based."),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    clients: RetailerClients | None = Depends(optional_http_clients),
) -> SearchResponse:
    """Answer from what has been collected, and revalidate behind the answer if it is stale.

    The refresh context is supplied only when the application lifespan opened the HTTP
    clients, so a search can never be the thing that starts a client pool.
    """
    context = RefreshContext(sessionmaker, clients) if clients is not None else None
    return await search_products(
        db,
        q,
        zip_code,
        availability=availability,
        open_now=open_now,
        page=page,
        page_size=page_size,
        refresh_context=context,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    clients: RetailerClients = Depends(get_http_clients),
) -> RefreshResponse:
    """Collect this ZIP's prices again, now, for the category the shopper is looking at.

    The same single-flight mechanism the automatic stale refresh uses, so pressing the
    button while one is already running joins it instead of starting a second scrape over
    the same offers.

    `already_running` and `cooling_down` come back as 200 with a `state`, not as an error:
    the request was correct and the API declined to start a second scrape, which is what
    the caller needs in order to render. Nothing is collected in either case -- that is the
    enforcement, and it lives here rather than in the browser so a reload or a second tab
    cannot shorten the cooldown.
    """
    category = category_for_query(body.query) if body.query else None
    key = refresh_key(body.zip_code, category.key if category else None)
    state, status = await registry.ensure_refresh(db, sessionmaker, clients, key)
    return RefreshResponse(
        state=state,
        zip_code=key[0],
        category=None if key[1] == ALL_CATEGORIES else key[1],
        category_label=category.label if category else None,
        freshness=freshness_out(await last_updated_for(db, key), status),
    )


@router.get("/{product_id}/price-history", response_model=PriceHistoryResponse)
async def price_history(
    product_id: int,
    days: int = Query(
        default=DEFAULT_RANGE_DAYS,
        ge=1,
        le=MAX_RANGE_DAYS,
        description=(
            "How far back to read, in days. The window decides which observations are "
            "returned, not which are drawn: the newest observation *before* it comes back "
            "too, flagged `before_window`, because a price that last moved outside the "
            "range is still the price the range starts at."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> PriceHistoryResponse:
    """One product's price over time, as one series per retailer, store and SKU.

    Separate from `/offers` on purpose. That endpoint answers "what can I buy right now",
    is opened by every expanded card, and must stay cheap; this one reads a table that
    grows for ever and is opened deliberately, by a shopper who asked for it.
    """
    result = await product_price_history(db, product_id, days=days)
    if result is None:
        raise HTTPException(status_code=404, detail="product not found")
    return result


@router.get("/{product_id}/offers", response_model=ProductOffersResponse)
async def offers(
    product_id: int,
    availability: AvailabilityFilter = Query(default="all"),
    open_now: bool = Query(
        default=False,
        description="Compare only stores that are not shut right now, as on /search.",
    ),
    db: AsyncSession = Depends(get_db),
) -> ProductOffersResponse:
    result = await product_offers(db, product_id, availability, open_now)
    if result is None:
        raise HTTPException(status_code=404, detail="product not found")
    return result
