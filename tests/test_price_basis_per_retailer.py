"""Every retailer that can price by weight, read through its own parser.

`tests/test_pricing_semantics.py` audits the captured payloads, but those captures are of
eggs and chicken and bananas — the categories a scrape asks for — and several retailers
simply had no weight-priced row in the shelves that were captured. Their `PER_POUND` branch
was therefore live code with no test at all, which is how a fix like this quietly stops
covering five of the retailers it was written for.

So each case below takes a **real captured record from that retailer** and flips only the one
field the retailer itself uses to mark a weight-priced item — `sellByWeight: "W"` for Safeway,
`quantityType: "weight"` for the Instacart storefronts, `saleUom: "LB"` for 99 Ranch. Nothing
else about the record is invented, and the flag values are the ones the adapter docstrings and
the other fixtures already document.
"""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from app.normalize.pricing import PACKAGE, PER_POUND
from app.normalize.unit_price import unit_price
from app.normalize.units import Quantity
from app.retailers.ranch99.adapter import parse_search_results as ranch99
from app.retailers.safeway.adapter import parse_similar_products as safeway
from app.retailers.savemartco.storefront import LUCKY_BANNER
from app.retailers.savemartco.storefront import parse_items as savemartco
from app.services.scraper import listing_quantity

FIXTURES = Path(__file__).parent / "fixtures"
ONE_POUND = Quantity(Decimal(1), "lb")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_safeway_reads_its_own_sell_by_weight_flag() -> None:
    """Safeway marks a weighed item `sellByWeight: "W"`; every captured doc is `"I"`."""
    payload = deepcopy(load("safeway/similar_eggs.json"))
    docs = payload["response"]["docs"]
    assert {d.get("sellByWeight") for d in docs} == {"I"}, "the capture has no weighed row"
    docs[0]["sellByWeight"] = "W"
    docs[0]["price"] = 2.99

    listings = safeway(payload, "4601", None)
    weighed = next(i for i in listings if i.retailer_sku == str(docs[0]["pid"]))

    assert weighed.price_basis == PER_POUND
    assert weighed.size_text is None, "a rate must leave no package size to divide by"
    assert listing_quantity(weighed) == ONE_POUND
    assert unit_price(weighed.price, ONE_POUND, "lb") == Decimal("2.99")
    others = [i for i in listings if i.retailer_sku != str(docs[0]["pid"])]
    assert others and all(i.price_basis == PACKAGE for i in others)


def test_the_instacart_storefronts_read_their_own_quantity_type() -> None:
    """Lucky and Save Mart share one storefront; it marks a weighed item `quantityType`."""
    payload = deepcopy(load("lucky/items_eggs.json"))
    items = payload["data"]["items"]
    assert all((i.get("quantityAttributes") or {}).get("quantityType") != "weight" for i in items)
    items[0].setdefault("quantityAttributes", {})["quantityType"] = "weight"
    items[0]["price"]["viewSection"]["priceString"] = "$7.49"

    listings = savemartco(payload, "23130", "lucky:test", banner=LUCKY_BANNER)
    weighed = next(i for i in listings if i.retailer_sku == str(items[0]["productId"]))

    assert weighed.price_basis == PER_POUND
    assert weighed.size_text is None
    assert listing_quantity(weighed) == ONE_POUND
    assert unit_price(weighed.price, ONE_POUND, "lb") == Decimal("7.49")


def test_99_ranch_reads_its_own_sale_unit_of_measure() -> None:
    """99 Ranch states the selling unit in `saleUom`; `LB` is a rate."""
    payload = deepcopy(load("ranch99/search_eggs.json"))
    records = payload["data"]["list"]
    assert all(str(r.get("saleUom") or "").upper() != "LB" for r in records)
    records[0]["saleUom"] = "LB"
    records[0]["price"] = 4.99
    records[0]["salePrice"] = None

    listings = ranch99(payload, "1")
    weighed = next(i for i in listings if i.retailer_sku == str(records[0]["productId"]))

    assert weighed.price_basis == PER_POUND
    assert weighed.size_text is None
    assert listing_quantity(weighed) == ONE_POUND
    assert unit_price(weighed.price, ONE_POUND, "lb") == Decimal("4.99")


@pytest.mark.parametrize(
    ("price", "unit_line", "basis"),
    [
        ("2.59", "$2.59/lb", PER_POUND),
        ("0.25", "25.0 ¢/oz", "oz"),
        # A fixed package carries a unit-price line too. Reading its presence as a weight flag
        # published "$8.00 a pound" for a 32 oz jar.
        ("8.00", "25.0 ¢/oz", PACKAGE),
        ("4.99", None, PACKAGE),
    ],
)
def test_walmart_reads_the_amount_in_its_unit_price_line_not_merely_its_presence(
    price: str, unit_line: str | None, basis: str
) -> None:
    from app.retailers.walmart.adapter import _price_basis

    assert _price_basis(Decimal(price), unit_line) == basis
