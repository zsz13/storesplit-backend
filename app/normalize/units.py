"""Quantity parsing and unit conversion. Pure functions, Decimal arithmetic, no I/O."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Canonical unit -> (dimension, factor to the dimension's base unit).
# Base units: count -> "count", mass -> "oz", volume -> "fl_oz".
UNITS: dict[str, tuple[str, Decimal]] = {
    "count": ("count", Decimal(1)),
    "oz": ("mass", Decimal(1)),
    "lb": ("mass", Decimal(16)),
    "g": ("mass", Decimal("0.03527396")),
    "kg": ("mass", Decimal("35.27396")),
    "fl_oz": ("volume", Decimal(1)),
    "pt": ("volume", Decimal(16)),
    "qt": ("volume", Decimal(32)),
    "gal": ("volume", Decimal(128)),
    "ml": ("volume", Decimal("0.033814")),
    "l": ("volume", Decimal("33.814")),
}

# Raw tokens found in retailer titles/size strings -> canonical unit.
_ALIASES: dict[str, str] = {
    "ct": "count",
    "count": "count",
    "ea": "count",
    "each": "count",
    "pk": "count",
    "pack": "count",
    "pc": "count",
    "pcs": "count",
    "piece": "count",
    "pieces": "count",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "fz": "fl_oz",
    "floz": "fl_oz",
    "fl oz": "fl_oz",
    "fl. oz": "fl_oz",
    "fl.oz": "fl_oz",
    "fluid ounce": "fl_oz",
    "fluid ounces": "fl_oz",
    "pt": "pt",
    "pint": "pt",
    "pints": "pt",
    "qt": "qt",
    "quart": "qt",
    "quarts": "qt",
    "gl": "gal",
    "gal": "gal",
    "gallon": "gal",
    "gallons": "gal",
    "ml": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
}

_UNIT_PATTERN = "|".join(sorted((re.escape(alias) for alias in _ALIASES), key=len, reverse=True))
_QUANTITY_RE = re.compile(
    rf"(?P<value>\d+/\d+|\d+(?:\.\d+)?|\.\d+)\s*-?\s*(?P<unit>{_UNIT_PATTERN})(?![a-z])",
    re.IGNORECASE,
)
_DOZEN_RE = re.compile(
    r"\b(?:(?P<n>\d+/\d+|\d+(?:\.\d+)?|half(?:\s+a)?)\s*)?(?:dozen|doz|dz)\b", re.IGNORECASE
)
_MULTIPACK_RE = re.compile(
    rf"(?P<packs>\d+)\s*(?:x|pk of|pack of|packs of)\s*"
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})(?![a-z])",
    re.IGNORECASE,
)


# A published weight range: "2.5-5.25lbs", "1.25 - 2.5 lb", "1 to 2 lb". The low end may
# repeat the unit ("2.5lb-5.25lb") or leave it to the high end, which is how most retailers
# write it. Anchored on a real separator so "12-ct" and "4 x 8 oz" are not ranges.
_RANGE_RE = re.compile(
    rf"(?P<low>\d+(?:\.\d+)?|\.\d+)\s*(?:(?P<low_unit>{_UNIT_PATTERN})(?![a-z]))?\s*"
    r"(?:[-\u2013\u2014]|\bto\b)\s*"
    rf"(?P<high>\d+(?:\.\d+)?|\.\d+)\s*(?P<unit>{_UNIT_PATTERN})(?![a-z])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: str  # canonical unit key from UNITS

    @property
    def dimension(self) -> str:
        return UNITS[self.unit][0]

    def to_base(self) -> Decimal:
        return self.value * UNITS[self.unit][1]

    def convert_to(self, unit: str) -> Decimal | None:
        """Convert to another unit of the same dimension; None if dimensions differ."""
        target_dim, target_factor = UNITS[unit]
        if target_dim != self.dimension:
            return None
        return self.to_base() / target_factor


@dataclass(frozen=True)
class QuantityRange:
    """The span a variable-weight package is sold within, as the retailer published it.

    It is deliberately not a `Quantity`: a tray that weighs somewhere between 2.5 and 5.25 lb
    has no single size, and collapsing it to one -- to either end, or to a midpoint -- is how
    a per-pound price ends up divided by a number nobody stated. It is carried beside the
    price so the shopper can be told the truth ("2.5-5.25 lb, final price based on weight")
    instead of a plausible average.
    """

    minimum: Quantity
    maximum: Quantity

    def __post_init__(self) -> None:
        if self.minimum.dimension != self.maximum.dimension:
            raise ValueError("a weight range's ends must measure the same dimension")


def parse_quantity_range(text: str | None, prefer_unit: str | None = None) -> QuantityRange | None:
    """Extract a published size range, e.g. "Chicken Breast - 2.5-5.25lbs" -> 2.5 to 5.25 lb.

    Returns None unless both ends are present, positive, ordered and in the same dimension.
    A single quantity is not a range and never becomes one here.

    `prefer_unit` settles a title that carries more than one: "4-6 oz Portions, 2.5-5.25 lbs"
    names a portion size before the pack weight, and the pack weight is the one a per-pound
    price is quoted against. The caller knows which unit it is pricing in, so it says so, and
    the first range in that unit wins; without a preference the first range does.
    """
    if not text:
        return None
    for match in _RANGE_RE.finditer(text):
        found = _range_from_match(match)
        if found is None:
            continue
        if prefer_unit is None or found.maximum.unit == prefer_unit:
            return found
    # Nothing in the preferred unit: fall back to the first readable range rather than none,
    # because a range in another unit is still what the retailer published about this pack.
    for match in _RANGE_RE.finditer(text):
        found = _range_from_match(match)
        if found is not None:
            return found
    return None


def _range_from_match(match: re.Match[str]) -> QuantityRange | None:
    high_unit = canonical_unit(match.group("unit"))
    if high_unit is None:
        return None
    raw_low_unit = match.group("low_unit")
    low_unit = canonical_unit(raw_low_unit) if raw_low_unit else high_unit
    if low_unit is None:
        return None
    low = _to_decimal(match.group("low"))
    high = _to_decimal(match.group("high"))
    if low is None or high is None or low <= 0 or high <= 0:
        return None
    minimum, maximum = Quantity(low, low_unit), Quantity(high, high_unit)
    if minimum.dimension != maximum.dimension:
        return None
    if maximum.to_base() <= minimum.to_base():
        return None  # "10-5 oz" is not a range; refusing it is cheaper than repairing it
    return QuantityRange(minimum, maximum)


def canonical_unit(raw: str) -> str | None:
    key = raw.strip().lower().replace("  ", " ")
    return _ALIASES.get(key)


def parse_quantity(text: str | None) -> Quantity | None:
    """Extract a package quantity from free text such as "Whole Milk, 64 FZ" or "12 ct".

    Returns None when no recognizable quantity is present. "dozen" maps to 12 count.
    A multipack such as "4 x 8 oz" is summed to its total.
    """
    if not text:
        return None
    multi = _MULTIPACK_RE.search(text)
    if multi:
        unit = canonical_unit(multi.group("unit"))
        if unit:
            total = Decimal(multi.group("packs")) * Decimal(multi.group("value"))
            return Quantity(total, unit)
    dozen = _DOZEN_RE.search(text)
    if dozen:
        raw = dozen.group("n")
        if raw is None:
            n: Decimal | None = Decimal(1)
        elif raw.lower().startswith("half"):
            n = Decimal("0.5")
        else:
            n = _to_decimal(raw)
        if n is None or n <= 0:
            return None
        return Quantity(n * 12, "count")
    match = _QUANTITY_RE.search(text)
    if not match:
        return None
    unit = canonical_unit(match.group("unit"))
    if unit is None:
        return None
    value = _to_decimal(match.group("value"))
    if value is None or value <= 0:
        return None
    return Quantity(value, unit)


def _to_decimal(raw: str) -> Decimal | None:
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        if Decimal(denominator) == 0:
            return None
        return Decimal(numerator) / Decimal(denominator)
    return Decimal(raw)


_PACK_RE = re.compile(r"\b\d+\s*-?\s*(?:pk|pack|packs)\b|multi-?pack|variety pack", re.IGNORECASE)


def is_multipack(text: str | None) -> bool:
    """True for "12pk", "2 pack", "multipack": the size string may be per-unit or total, so the
    package is not comparable without retailer-specific knowledge."""
    return bool(text and _PACK_RE.search(text))


def strip_quantity_phrases(text: str) -> str:
    """Remove quantity phrases ("12 ct", "64 fl oz", "dozen", "4 x 8 oz", "2.5-5.25lbs")."""
    # The range goes first: taking the single-quantity pass over "2.5-5.25lbs" removes only
    # its upper end and leaves "2.5-" behind in the product's name.
    text = _RANGE_RE.sub(" ", text)
    text = _MULTIPACK_RE.sub(" ", text)
    text = _QUANTITY_RE.sub(" ", text)
    return _DOZEN_RE.sub(" ", text)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantize_unit_price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
