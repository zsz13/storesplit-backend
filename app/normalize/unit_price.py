"""Price-per-unit arithmetic."""

from decimal import Decimal

from app.normalize.units import Quantity, quantize_unit_price


def comparison_quantity(quantity: Quantity, comparison_unit: str) -> Decimal | None:
    """Package size expressed in the category's comparison unit, or None if not convertible."""
    converted = quantity.convert_to(comparison_unit)
    if converted is None or converted <= 0:
        return None
    return converted


def unit_price(price: Decimal, quantity: Quantity, comparison_unit: str) -> Decimal | None:
    """Price per comparison unit (e.g. $/egg, $/lb, $/gal), rounded to 4 decimals."""
    size = comparison_quantity(quantity, comparison_unit)
    if size is None:
        return None
    return quantize_unit_price(Decimal(price) / size)
