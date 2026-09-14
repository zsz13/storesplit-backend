from decimal import Decimal

import pytest
from app.normalize.units import Quantity, is_multipack, parse_quantity, strip_quantity_phrases


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("Large Grade A Eggs, 12 CT", "12", "count"),
        ("Whole Milk, 64 FZ", "64", "fl_oz"),
        ("Whole Milk, .5 GL", "0.5", "gal"),
        ("Whole Milk, 1 PT", "1", "pt"),
        ("Organic Jasmati Rice, 32 OZ", "32", "oz"),
        ("3 lb", "3", "lb"),
        ("12 ct", "12", "count"),
        ("1 gal", "1", "gal"),
        ("59 fl oz", "59", "fl_oz"),
        ("1 dozen", "12", "count"),
        ("Eggs, Dozen", "12", "count"),
        ("Banana Nut Mini Muffin, 12ct", "12", "count"),
        ("4 x 8 oz", "32", "oz"),
        ("500 g", "500", "g"),
        ("2 lbs", "2", "lb"),
        ("1/2 gal", "0.5", "gal"),
        ("Eggs, 1/2 Dozen", "6", "count"),
        ("Half Dozen Eggs", "6", "count"),
        ("Half a dozen large eggs", "6", "count"),
    ],
)
def test_parse_quantity(text: str, value: str, unit: str) -> None:
    quantity = parse_quantity(text)
    assert quantity == Quantity(Decimal(value), unit)


@pytest.mark.parametrize("text", ["Boneless Skinless Chicken Breast", "Banana", "", None])
def test_parse_quantity_none(text: str | None) -> None:
    assert parse_quantity(text) is None


def test_conversions() -> None:
    assert Quantity(Decimal(1), "lb").convert_to("oz") == Decimal(16)
    assert Quantity(Decimal(64), "fl_oz").convert_to("gal") == Decimal("0.5")
    assert Quantity(Decimal(2), "qt").convert_to("gal") == Decimal("0.5")
    assert Quantity(Decimal(12), "count").convert_to("count") == Decimal(12)
    assert Quantity(Decimal(16), "oz").convert_to("gal") is None  # mass vs volume
    assert Quantity(Decimal(12), "count").convert_to("lb") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Organic Whole Milk SS Milk 12pk, 8 FZ", True),
        ("Smooth Peanut Butter 2pk, 26 OZ", True),
        ("Eggs Variety Pack", True),
        ("Whole Milk, 64 FZ", False),
        ("Large Grade A Eggs, 12 CT", False),
        (None, False),
    ],
)
def test_is_multipack(text: str | None, expected: bool) -> None:
    assert is_multipack(text) is expected


def test_strip_quantity_phrases() -> None:
    assert strip_quantity_phrases("Whole Milk, 64 FZ").strip(", ") == "Whole Milk"
    assert "dozen" not in strip_quantity_phrases("Eggs, 1 dozen")
