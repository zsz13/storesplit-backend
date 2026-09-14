"""How old the prices behind an answer are, and what is being done about it.

Freshness is reported, never enforced: a stale price is still the best answer anyone has, so
the API returns it and revalidates behind the answer. This module owns the two things both
the search and the manual-refresh endpoint need to say that consistently -- when a key's
offers were last collected, and how that reads next to the refresh registry's state.

Freshness deliberately ignores the availability filter. How recently a scrape ran is a fact
about the scrape; switching a filter to "out of stock" must not make the data look older or
newer than it is.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CanonicalProduct, Offer, RetailerProduct
from app.schemas import FreshnessOut
from app.services.refresh import ALL_CATEGORIES, RefreshStatus
from app.services.stores import stores_near


def freshness_out(last_updated: datetime | None, status: RefreshStatus) -> FreshnessOut:
    """Assemble what the API reports, from the data's age and the registry's state."""
    ttl = get_settings().search_freshness_ttl_seconds
    age = None
    if last_updated is not None:
        age = max(0.0, (datetime.now(UTC) - _aware(last_updated)).total_seconds())
    # Nothing collected here yet is stale by definition: there is something to go and fetch.
    is_stale = age is None or age > ttl
    return FreshnessOut(
        last_updated_at=last_updated,
        age_seconds=age,
        ttl_seconds=ttl,
        is_stale=is_stale,
        refreshing=status.refreshing,
        refresh_started_at=status.started_at,
        refresh_finished_at=status.finished_at,
        cooldown_seconds=status.cooldown_seconds,
        refresh_available_in_seconds=status.available_in_seconds,
        can_refresh=status.can_refresh,
        last_error=status.last_error,
    )


async def last_updated_for(db: AsyncSession, key: tuple[str, str]) -> datetime | None:
    """When this (ZIP, category) key's offers were last collected.

    Used by the refresh endpoint, which has a key but no search behind it. The search
    service gets the same value out of the aggregate it already runs, rather than paying
    for a second query.
    """
    zip_code, category = key
    stores = await stores_near(db, zip_code)
    if not stores:
        return None
    stmt = (
        select(func.max(Offer.scraped_at))
        .select_from(Offer)
        .join(Offer.retailer_product)
        .join(RetailerProduct.canonical_product)
        .where(Offer.store_id.in_([store.id for store in stores]))
    )
    if category != ALL_CATEGORIES:
        stmt = stmt.where(CanonicalProduct.category == category)
    return await db.scalar(stmt)


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything here compares in UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
