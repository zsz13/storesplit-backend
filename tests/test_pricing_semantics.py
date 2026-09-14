"""What a retailer's price is *per*, and the one rule that keeps it from being divided twice.

The bug this file exists for: a price that is already a rate was treated as a package total
and divided by the package's weight, so Target's $2.59/lb chicken was published at $0.49/lb
and ranked first. The rule that fixes it is one line in `listing_quantity` -- a per-unit
price buys one unit of its own basis and nothing else is read -- and these tests hold it in
place for every retailer, not just the one that made it visible.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.normalize.pricing import (
    PACKAGE,
    PER_EACH,
    PER_OUNCE,
    PER_POUND,
    basis_quantity,
    is_per_unit,
    normalize_basis,
)
from app.normalize.unit_price import unit_price
from app.normalize.units import Quantity, QuantityRange, parse_quantity_range
from app.retailers.base import ProductListing
from app.services.scraper import listing_quantity

FIXTURES = Path(__file__).parent / "fixtures"


def _listing(**fields: object) -> ProductListing:
    base: dict[str, object] = {
        "retailer_sku": "1",
        "title": "Boneless Skinless Chicken Breast",
        "store_external_id": "A1",
        "price": Decimal("2.59"),
        "regular_price": Decimal("2.59"),
    }
    return ProductListing(**{**base, **fields})  # type: ignore[arg-type]


# ------------------------------------------------------------------------ the vocabulary


def test_a_basis_is_one_of_four_things_and_anything_else_is_a_package() -> None:
    """ "Nobody said" is a package total: that is what every retailer means unless it says
    otherwise, and it is the reading whose mistakes are visible rather than silent."""
    assert normalize_basis("lb") == PER_POUND
    assert normalize_basis("OZ") == PER_OUNCE
    assert normalize_basis(" each ") == PER_EACH
    assert normalize_basis("package") == PACKAGE
    for junk in (None, "", "kilogram", "per lb", "weight", "1"):
        assert normalize_basis(junk) == PACKAGE, junk


def test_a_per_unit_price_buys_exactly_one_unit_of_its_own_basis() -> None:
    assert basis_quantity(PER_POUND) == Quantity(Decimal(1), "lb")
    assert basis_quantity(PER_OUNCE) == Quantity(Decimal(1), "oz")
    assert basis_quantity(PER_EACH) == Quantity(Decimal(1), "count")
    assert basis_quantity(PACKAGE) is None
    assert is_per_unit(PER_POUND) and not is_per_unit(PACKAGE)


# --------------------------------------------------------------- the rule that was missing


def test_a_rate_is_never_divided_by_a_weight_found_in_the_title() -> None:
    """The regression, stated in the smallest possible terms."""
    tray = _listing(
        title="Chicken Breast Value Pack - 2.5-5.25lbs - price per lb", price_basis=PER_POUND
    )

    assert listing_quantity(tray) == Quantity(Decimal(1), "lb")
    assert unit_price(tray.price, Quantity(Decimal(1), "lb"), "lb") == Decimal("2.59")


def test_a_rate_is_never_divided_by_a_size_string_either() -> None:
    """Kroger keeps `size` ("1 lb") on its weight-sold items, so the size string is the same
    trap as the title -- latent while the size happens to be one pound, and a factor of three
    out the day it is not."""
    bananas = _listing(title="Bananas", size_text="3 lb", price=Decimal("0.59"))
    packaged = listing_quantity(bananas)
    assert packaged == Quantity(Decimal(3), "lb"), "with no basis stated it is a package"

    by_weight = _listing(
        title="Bananas", size_text="3 lb", price=Decimal("0.59"), price_basis=PER_POUND
    )
    assert listing_quantity(by_weight) == Quantity(Decimal(1), "lb")
    assert unit_price(by_weight.price, Quantity(Decimal(1), "lb"), "lb") == Decimal("0.59")


def test_a_per_ounce_rate_is_not_read_as_a_per_pound_one() -> None:
    """The old signal collapsed "/lb" and "/ounce" into a single "weight", then assumed
    pounds -- a sixteen-fold error in whichever direction the retailer happened to mean."""
    deli = _listing(title="Sliced Turkey Breast", price=Decimal("0.75"), price_basis=PER_OUNCE)

    assert listing_quantity(deli) == Quantity(Decimal(1), "oz")
    assert unit_price(deli.price, Quantity(Decimal(1), "oz"), "lb") == Decimal("12.00")


def test_a_package_total_is_still_divided_by_its_package() -> None:
    eggs = _listing(title="Large Grade A Eggs - 12ct", price=Decimal("5.89"), size_text="12 ct")

    assert listing_quantity(eggs) == Quantity(Decimal(12), "count")
    assert unit_price(eggs.price, Quantity(Decimal(12), "count"), "count") == Decimal("0.4908")


def test_a_multipack_is_still_skipped_when_it_is_priced_by_the_package() -> None:
    assert listing_quantity(_listing(title="Whole Milk 12pk, 8 FZ")) is None


def test_a_multipack_priced_by_the_pound_is_not_skipped() -> None:
    """A rate needs no package size, so the reason to skip a multipack does not apply: the
    thing that cannot be read is the package, and the package is not what is being divided."""
    listing = _listing(title="Chicken Thighs 2 pack - price per lb", price_basis=PER_POUND)

    assert listing_quantity(listing) == Quantity(Decimal(1), "lb")


# ------------------------------------------------------------------------- weight ranges


@pytest.mark.parametrize(
    ("text", "low", "high", "unit"),
    [
        ("Chicken Breast Value Pack - 2.5-5.25lbs - price per lb", "2.5", "5.25", "lb"),
        ("Foster Farms Chicken Breasts - 1.25-2.5lbs", "1.25", "2.5", "lb"),
        ("Beef Brisket 4 lb - 7 lb", "4", "7", "lb"),
        ("Salmon Fillet 8 to 12 oz", "8", "12", "oz"),
        ("Pork Shoulder 3\u20135 lb", "3", "5", "lb"),
    ],
)
def test_a_published_weight_range_is_read_as_a_range(
    text: str, low: str, high: str, unit: str
) -> None:
    parsed = parse_quantity_range(text)

    assert parsed == QuantityRange(Quantity(Decimal(low), unit), Quantity(Decimal(high), unit))


@pytest.mark.parametrize(
    "text",
    [
        "Large Grade A Eggs - 12ct",
        "Whole Milk, 64 FZ",
        "Organic Whole Milk SS Milk 4 x 8 oz",
        "Chicken Breast 10-5 lb",  # ends out of order: not a range, and not repaired into one
        "Cage-Free Eggs 12-18",  # no unit
        "",
        None,
    ],
)
def test_what_is_not_a_range_never_becomes_one(text: str | None) -> None:
    assert parse_quantity_range(text) is None


def test_a_range_never_stands_in_for_a_package_size() -> None:
    """A tray weighing somewhere between 2.5 and 5.25 lb has no single size, and the whole
    bug was picking one. The range is carried beside the price and never inside it."""
    tray = _listing(
        title="Chicken Breast - 2.5-5.25lbs - price per lb",
        price_basis=PER_POUND,
        weight_range=parse_quantity_range("2.5-5.25lbs"),
    )

    assert tray.weight_range is not None
    assert listing_quantity(tray) != Quantity(tray.weight_range.maximum.value, "lb")
    assert listing_quantity(tray) == Quantity(Decimal(1), "lb")


# ------------------------------------------------- every retailer, over its own captures


def _weight_priced_listings() -> list[tuple[str, ProductListing]]:
    """Every per-unit-priced listing in the captured payloads, by retailer.

    Imported lazily and kept in one place so the audit below covers whatever the fixtures
    happen to contain rather than a list somebody has to remember to extend.
    """
    from app.retailers.kroger.adapter import parse_products as kroger
    from app.retailers.smartandfinal.adapter import parse_search_results as smartandfinal
    from app.retailers.sprouts.adapter import parse_items as sprouts
    from app.retailers.target.adapter import parse_category as target
    from app.retailers.traderjoes.adapter import parse_products as traderjoes
    from app.retailers.wholefoods.adapter import parse_search_results as wholefoods

    def load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text())

    found: list[tuple[str, ProductListing]] = []
    for name in ("chicken_breast", "bananas", "rice", "butter", "eggs"):
        payload = load(f"wholefoods/search_{name}.json")
        found += [("wholefoods", i) for i in wholefoods(payload, "1")]
    found += [("kroger", i) for i in kroger(load("kroger/products_eggs.json"), "1")]
    for name in ("bananas", "chicken_breast"):
        payload = load(f"smartandfinal/search_{name}.json")
        found += [("smartandfinal", i) for i in smartandfinal(payload, "1")]
    found += [("sprouts", i) for i in sprouts(load("sprouts/items_chicken_breast.json"), "1")]
    found += [
        ("traderjoes", i)
        for i in traderjoes(load("traderjoes/search_chicken_breast.json"), "1", "tj:test")
    ]
    for shelf in ("plp_search_v2.json", "plp_search_v2_chicken.json"):
        found += [
            ("target", i)
            for i in target([("plp_search_v2?x", load(f"target/{shelf}"))], "3264", "t:test")
        ]
    return [(slug, item) for slug, item in found if is_per_unit(item.price_basis)]


def test_no_registered_retailer_double_normalizes_a_rate() -> None:
    """The audit. Every per-unit price in every captured payload must buy one of its own
    unit -- so the unit price published is the rate the retailer published, exactly."""
    audited = _weight_priced_listings()
    assert len(audited) >= 30, "the fixtures should still contain weight-priced items"
    for slug, item in audited:
        quantity = listing_quantity(item)
        expected = basis_quantity(item.price_basis)
        assert quantity is not None and quantity == expected, (
            f"{slug} {item.retailer_sku}: {item.title!r}"
        )
        assert unit_price(item.price, quantity, item.price_basis) == item.price, (
            f"{slug} {item.retailer_sku}: the rate must survive normalization unchanged"
        )


def test_every_retailer_that_prices_by_weight_leaves_the_package_size_unset() -> None:
    """A rate with a package size beside it is an invitation to divide one by the other."""
    for slug, item in _weight_priced_listings():
        assert item.size_text is None, f"{slug} {item.retailer_sku} kept size {item.size_text!r}"


def test_no_retailer_invents_a_maximum_total() -> None:
    """`max_total_price` is copied from a retailer or absent. Target's own ceiling for a
    2.5-5.25 lb tray at $2.59/lb is $12.95, not the $13.60 that multiplying gives."""
    for slug, item in _weight_priced_listings():
        if item.max_total_price is None or item.weight_range is None:
            continue
        derived = item.price * item.weight_range.maximum.value
        assert item.max_total_price != derived or slug == "", (
            f"{slug} {item.retailer_sku}: a maximum equal to price x max weight is suspicious"
        )


def test_a_title_with_two_ranges_publishes_the_one_the_price_is_quoted_in() -> None:
    """ "4-6 oz Portions - 2.5-5.25 lbs" names a portion size before the pack weight. A
    per-pound price is quoted against the pack, so the pack is the range to show."""
    text = "Chicken Breast 4-6 oz Portions - 2.5-5.25lbs - price per lb"

    assert parse_quantity_range(text, "lb") == QuantityRange(
        Quantity(Decimal("2.5"), "lb"), Quantity(Decimal("5.25"), "lb")
    )
    assert parse_quantity_range(text, "oz") == QuantityRange(
        Quantity(Decimal(4), "oz"), Quantity(Decimal(6), "oz")
    )
    # No range in the preferred unit: the first readable one is still what was published.
    assert parse_quantity_range(text, "gal") == parse_quantity_range(text)
