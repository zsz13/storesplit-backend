"""What a retailer's price is *per*, stated rather than inferred.

A number on a shelf label is not a price until you know what it buys. `$2.59` against a tray
of chicken is a rate if the tray is weighed at the till and a total if it is not, and the two
readings differ by a factor of the package weight. Retailers publish which one they mean --
Target says `"formatted_unit_price_suffix": "/lb"`, Kroger says `"soldBy": "WEIGHT"`, Safeway
says `"sellByWeight": "W"` -- and this module is where that statement is written down, so no
later step has to guess it back out of a display string.

`PriceBasis` is the whole vocabulary:

  * `package`  the price is the total for one package. The unit price is the quotient of that
               total and the package size, which is the only case where a division belongs.
  * `lb`/`oz`  the price is a *rate*. It is already per unit, so it is converted to the
               category's comparison unit and **never divided by a package weight**: doing
               that is the double normalization that made Target's `$2.59/lb` chicken read
               `$0.49/lb`, because its 5.25 lb upper weight was taken off the title and
               applied to a number that had already accounted for it.
  * `each`     the price is per item where the package is not a fixed count (loose fruit).

A variable-weight item adds two facts that are not prices: the weight range the retailer
publishes (`2.5-5.25 lb`), and the maximum total it will charge. Both are carried separately
and neither is invented -- `max_total_price` is stored only when the retailer states it.
Target's own maximum for a 2.5-5.25 lb tray at $2.59/lb is $12.95, which is $2.59 x 5.00 and
not $2.59 x 5.25, so a maximum computed here from the published range would be wrong by
sixty-five cents while looking exactly as authoritative.
"""

from decimal import Decimal
from typing import Literal

from app.normalize.units import Quantity

# What `ProductListing.price` / `Offer.price` is quoted per.
PriceBasis = Literal["package", "lb", "oz", "each"]

PACKAGE: PriceBasis = "package"
PER_POUND: PriceBasis = "lb"
PER_OUNCE: PriceBasis = "oz"
PER_EACH: PriceBasis = "each"

BASES: frozenset[str] = frozenset({PACKAGE, PER_POUND, PER_OUNCE, PER_EACH})

# The canonical unit one of each per-unit basis is measured in. `package` has no entry: it is
# the absence of a rate, not a rate in some other unit.
_BASIS_UNITS: dict[str, str] = {PER_POUND: "lb", PER_OUNCE: "oz", PER_EACH: "count"}


def normalize_basis(raw: str | None) -> PriceBasis:
    """A stored or wire value as a basis, defaulting to `package`.

    Unreadable input means "nobody said", and "nobody said" is a package total: that is what
    every retailer publishes unless it says otherwise, and it is the reading that makes a
    wrong guess visible (a rate shown as a total looks absurdly cheap) rather than silent.
    """
    text = (raw or "").strip().lower()
    return text if text in BASES else PACKAGE  # type: ignore[return-value]


def is_per_unit(basis: PriceBasis) -> bool:
    """True when the price is already a rate, so dividing it by a package size is an error."""
    return basis != PACKAGE


def basis_quantity(basis: PriceBasis) -> Quantity | None:
    """The quantity a per-unit price buys -- one pound, one ounce, one item.

    This is what makes the fix a single rule rather than a special case per retailer: a rate
    priced against one unit of its own basis yields itself, whatever comparison unit the
    category uses, and the arithmetic that follows is the ordinary `price / size`.
    """
    unit = _BASIS_UNITS.get(basis)
    return Quantity(Decimal(1), unit) if unit else None


def basis_label(basis: PriceBasis) -> str | None:
    """How the basis reads next to a price ("/lb"), or None for a package total."""
    return f"/{basis}" if is_per_unit(basis) else None
