"""The two reported products, from Target's own payload through to the API response.

`tests/test_target.py` pins the adapter's reading of those payloads and
`tests/test_pricing_semantics.py` pins the arithmetic. This is the rest of the journey: a
scrape that ingests them, and a search that hands a client what it needs to render the right
sentence. The numbers asserted here are the ones on Target's product pages:

    A-86676070   $12.95 max price ($2.59/lb)    Final price based on weight    2.5-5.25 lbs
    A-84991365   $11.98 max price ($5.99/lb)    Final price based on weight    1.25-2.5 lbs

and the numbers that must never come back are $0.49/lb and $2.40/lb, which are those rates
divided by their own upper weights.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.db.models import Offer, RetailerProduct
from app.retailers.base import StoreLocation
from app.retailers.target.adapter import parse_category
from app.services.scraper import run_scrape
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.fakes import FakeAdapter
from tests.test_scrape_and_api import use_adapters

FIXTURES = Path(__file__).parent / "fixtures" / "target"
# `all`, because the captured shelf carries no fulfillment payload: Target never said
# anything about stock for these two, and an unsaid stock level is `unknown`, not `in_stock`.
SEARCH = {"q": "chicken breast", "zip_code": "94132", "availability": "all"}
STONESTOWN = StoreLocation(
    external_id="3264",
    name="San Francisco Stonestown",
    address_line1="285 Winston Dr",
    city="San Francisco",
    state="CA",
    zip_code="94132",
    latitude=37.726614,
    longitude=-122.476464,
)


def _chicken() -> list:
    payload = json.loads((FIXTURES / "plp_search_v2_chicken.json").read_text())
    return parse_category([("plp_search_v2?x=1", payload)], "3264", "target:redsky/plp_search_v2")


@pytest.fixture
async def scraped_target(sessionmaker, clients, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scrape of one retailer, one store, one category, from the captured shelf."""
    listings = _chicken()
    assert len(listings) == 2, "the fixture carries both reported products"
    adapter = FakeAdapter("target", [STONESTOWN], {"chicken breast": {"3264": listings}})
    # Target's own `buy_url`s are on its own host, and the scrape checks every product URL
    # against the adapter's declared site before storing it.
    adapter.site_url = "https://www.target.com"
    use_adapters(monkeypatch, adapter)
    await run_scrape(sessionmaker, clients, "94132", None, ["chicken_breast"])


async def _offers(db: AsyncSession) -> dict[str, Offer]:
    rows = await db.scalars(
        select(Offer).options(selectinload(Offer.retailer_product)).order_by(Offer.id)
    )
    return {offer.retailer_product.retailer_sku: offer for offer in rows}


@pytest.mark.parametrize(
    ("sku", "rate", "maximum", "low", "high"),
    [
        ("86676070", "2.59", "12.95", "2.5000", "5.2500"),
        ("84991365", "5.99", "11.98", "1.2500", "2.5000"),
    ],
)
async def test_a_variable_weight_offer_is_stored_as_a_rate_with_its_range(
    db: AsyncSession, scraped_target, sku: str, rate: str, maximum: str, low: str, high: str
) -> None:
    offer = (await _offers(db))[sku]

    assert offer.price == Decimal(rate)
    assert offer.price_basis == "lb", "the price is a rate, and the row says so"
    assert offer.unit_price == Decimal(rate), "the rate is the unit price, not a quotient"
    assert offer.unit_price_unit == "lb"
    assert offer.max_total_price == Decimal(maximum)
    assert offer.retailer_product.min_weight == Decimal(low)
    assert offer.retailer_product.max_weight == Decimal(high)
    assert offer.retailer_product.weight_unit == "lb"
    assert offer.retailer_product.size_text is None


@pytest.mark.parametrize(
    ("sku", "wrong"), [("86676070", Decimal("0.4933")), ("84991365", Decimal("2.3960"))]
)
async def test_the_old_double_normalized_unit_price_is_gone(
    db: AsyncSession, scraped_target, sku: str, wrong: Decimal
) -> None:
    """The exact numbers the bug report quoted, rounded as the UI rounded them: $0.49/lb and
    $2.40/lb. Either of them reappearing means a package weight got back into the division."""
    offer = (await _offers(db))[sku]

    assert offer.unit_price != wrong
    assert offer.unit_price is not None and offer.unit_price > wrong


async def test_two_trays_at_different_rates_stay_two_products(
    db: AsyncSession, scraped_target
) -> None:
    """Both are "boneless skinless chicken breast" priced per pound, and a shopper choosing
    between $2.59 and $5.99 is choosing between two real products."""
    products = list(await db.scalars(select(RetailerProduct)))

    assert len(products) == 2
    assert {p.retailer_sku for p in products} == {"86676070", "84991365"}


async def test_the_api_gives_a_client_everything_the_sentence_needs(
    client: AsyncClient, scraped_target
) -> None:
    response = await client.get("/products/search", params=SEARCH)
    assert response.status_code == 200
    offers = [o for p in response.json()["products"] for o in p["offers"]]
    by_sku = {o["retailer_sku"]: o for o in offers}
    assert set(by_sku) == {"86676070", "84991365"}

    value_pack = by_sku["86676070"]
    assert value_pack["price"] == "2.59"
    assert value_pack["price_basis"] == "lb"
    assert value_pack["unit_price"] == "2.5900"
    assert value_pack["unit_price_unit"] == "lb"
    assert value_pack["max_total_price"] == "12.95"
    assert value_pack["min_weight"] == "2.5000"
    assert value_pack["max_weight"] == "5.2500"
    assert value_pack["weight_unit"] == "lb"

    foster_farms = by_sku["84991365"]
    assert foster_farms["price"] == "5.99"
    assert foster_farms["unit_price"] == "5.9900"
    assert foster_farms["max_total_price"] == "11.98"


async def test_the_comparison_unit_price_is_the_retailers_own_rate(
    client: AsyncClient, scraped_target
) -> None:
    """What a card ranks by. $2.59/lb is cheaper than $5.99/lb, and it is cheaper by the
    amount Target says -- not by a factor of whichever tray is heavier."""
    response = await client.get("/products/search", params=SEARCH)
    offers = [o for p in response.json()["products"] for o in p["offers"]]
    rates = {o["retailer_sku"]: Decimal(o["unit_price"]) for o in offers}

    assert rates["86676070"] == Decimal("2.5900")
    assert rates["84991365"] == Decimal("5.9900")
    assert rates["84991365"] / rates["86676070"] == pytest.approx(Decimal("2.31"), abs=0.01)
