from decimal import Decimal

from app.normalize.unit_price import comparison_quantity, unit_price
from app.normalize.units import Quantity


def test_price_per_egg() -> None:
    assert unit_price(Decimal("4.99"), Quantity(Decimal(12), "count"), "count") == Decimal("0.4158")


def test_price_per_gallon_from_fluid_ounces() -> None:
    assert unit_price(Decimal("4.49"), Quantity(Decimal(64), "fl_oz"), "gal") == Decimal("8.98")


def test_price_per_pound_from_ounces() -> None:
    assert unit_price(Decimal("5.49"), Quantity(Decimal(8), "oz"), "lb") == Decimal("10.98")


def test_price_per_ounce() -> None:
    assert unit_price(Decimal("7.29"), Quantity(Decimal(27), "oz"), "oz") == Decimal("0.27")


def test_incompatible_dimension_gives_none() -> None:
    assert unit_price(Decimal("7.49"), Quantity(Decimal(16), "oz"), "count") is None
    assert comparison_quantity(Quantity(Decimal(16), "oz"), "gal") is None
