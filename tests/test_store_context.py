"""The store a retailer says it answered for, checked against the store it was asked about.

Adapters report the retailer's own echo -- Whole Foods' `storeId`, Raley's
`currentStoreNumber` -- and the scrape service enforces one rule over all of them. A price
collected from the wrong store is worse than no price, so a listing whose echo disagrees is
dropped rather than written under a store it does not belong to.
"""

from decimal import Decimal
from pathlib import Path

from app.db.models import Offer, Store
from app.normalize.categories import CATEGORIES
from app.retailers.base import ProductListing, StoreLocation
from app.retailers.raleys.adapter import parse_product_page as parse_raleys
from app.retailers.wholefoods.adapter import parse_search_results, parse_store_context
from app.services.scraper import scrape_retailer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_raleys_reports_the_store_its_page_was_priced_for() -> None:
    listing = parse_raleys(read("raleys/product_page.html"), "415")

    assert listing is not None
    assert listing.store_context == "415"


def test_raleys_refuses_a_page_priced_for_a_different_store() -> None:
    """The cookie asks for store 628; the page comes back priced at 415. Believe neither."""
    listing = parse_raleys(read("raleys/product_page.html"), "628")

    assert listing is None


def test_wholefoods_listings_carry_the_proven_store() -> None:
    import json

    payload = json.loads(read("wholefoods/search_eggs.json"))
    context = parse_store_context(read("wholefoods/product_page_soma.html"))
    assert context is not None

    listings = parse_search_results(payload, "10151")

    assert listings and all(item.store_context is None for item in listings), (
        "search says nothing about the store beyond what was asked; the page is the proof"
    )
    assert context.store_external_id == "10151"


async def test_the_scrape_drops_a_listing_whose_store_disagrees(db: AsyncSession) -> None:
    store = StoreLocation("S1", "Some Store", city="San Francisco", zip_code="94105")
    honest = ProductListing(
        retailer_sku="ok",
        title="Eggs, 12 CT",
        store_external_id="S1",
        price=Decimal("3.99"),
        regular_price=Decimal("3.99"),
        store_context="S1",
    )
    impostor = ProductListing(
        retailer_sku="wrong-store",
        title="Eggs, 18 CT",
        store_external_id="S1",
        price=Decimal("1.99"),
        regular_price=Decimal("1.99"),
        store_context="S9",  # the retailer answered for a different store
    )
    adapter = FakeAdapter("echo", [store], {"eggs": {"S1": [honest, impostor]}})

    await scrape_retailer(db, adapter, "94105", [CATEGORIES["eggs"]], 2)
    await db.commit()

    rows = list(await db.scalars(select(Offer)))
    assert [offer.store_context for offer in rows] == ["S1"]
    assert len(rows) == 1, "the offer priced at another store is not written"
    saved = await db.scalar(select(Store).where(Store.external_id == "S1"))
    assert saved is not None


def test_raleys_refuses_a_page_that_names_no_store_at_all() -> None:
    """The store cookie could stop taking -- a rename, a site change -- and the page would
    then render for the default store. Accepting a page that states no store would attach
    store 01's prices to whichever store was asked about."""
    silent = read("raleys/product_page.html").replace('"currentStoreNumber":"415"', '"x":"415"')
    silent = silent.replace('"key":"415"', '"key":""')
    assert silent != read("raleys/product_page.html")

    assert parse_raleys(silent, "415") is None
