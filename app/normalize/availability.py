"""The availability vocabulary every adapter reports in, and the database stores.

Pure, like the rest of `app/normalize`: it imports nothing but `typing`. It lives here rather
than under `app/retailers` because the database models and the API schemas need the same
vocabulary, and importing it from the retailer package would drag every adapter (and httpx)
into `app.db.models` -- and therefore into `alembic/env.py`, where a broken adapter would
break migrations.

Retailers spell inventory a dozen ways -- `inStock`, `available: true`, `inventoryAvailable:
"1"`, `stockLevel: "outOfStock"`, an absent field. StoreSplit collapses them to three states
so search, filtering and basket comparison can reason about one thing:

* `in_stock`     -- the retailer says this store has it.
* `out_of_stock` -- the retailer says this store does not.
* `unknown`      -- the retailer said nothing we can trust for this store.

**A missing signal is `unknown`, never `in_stock`.** Guessing here is what puts an
out-of-stock item at the top of a price comparison. The retailer's own wording is kept
alongside the normalized state (`ProductListing.stock_status`) so a surprising result can be
traced back to what the payload actually said.

**A hedge is not a signal either.** `lowStock`, `limited`, `low availability` and their
spellings used to read as `in_stock` here, on the assumption that "few left" still means
buyable. It does not mean that everywhere: the Instacart storefront platform (Sprouts,
Lucky, Save Mart) prints `lowStock` to the shopper as *"Likely out of stock"*, and reading
it as stocked is what showed Lucky product 19830961 as buyable while its own product page
said it was not. Words like these carry no portable meaning, so they resolve to `unknown`
and a retailer that knows what its own hedge means says so in its adapter -- Kroger's `LOW`
is a documented "in stock, running low" and is mapped in `kroger/adapter.py`, while the
storefront banners resolve theirs in `retailers/instacart_storefront.py`.
"""

from typing import Literal

Availability = Literal["in_stock", "out_of_stock", "unknown"]

IN_STOCK: Availability = "in_stock"
OUT_OF_STOCK: Availability = "out_of_stock"
UNKNOWN: Availability = "unknown"

AVAILABILITY_STATES: tuple[Availability, ...] = (IN_STOCK, OUT_OF_STOCK, UNKNOWN)

# Why a retailer's offer is `unknown`, which is a different question from what `unknown`
# means. The state is a fact about one offer; this is a standing fact about the retailer.
#
# `live` -- this retailer does publish per-store inventory, so `unknown` here is a reading
#           that could not be trusted: a hedge nobody has mapped, a negative that flapped,
#           a payload that came back without the field. Something is missing.
# `not_published` -- this retailer never states shelf stock anywhere StoreSplit can read.
#           Trader Joe's sells nothing online; Raley's commercetools catalogue runs with
#           `inventoryMode: "None"`. Nothing is missing: the answer does not exist, and the
#           price, the product page and the store are all real regardless.
#
# The distinction exists for one reason: "Stock unknown" is the wrong sentence for the
# second case. It reads as a fault -- as though StoreSplit failed to look -- when the honest
# statement is that the retailer does not publish it and the shopper should check in store.
# It changes no ranking. `unknown` is filtered out of the comparison either way: neither
# kind may win a badge, lead a card, or enter a basket.
StockReporting = Literal["live", "not_published"]

LIVE_STOCK: StockReporting = "live"
STOCK_NOT_PUBLISHED: StockReporting = "not_published"

# Retailer tokens, reduced to lowercase with separators collapsed to "_".
_IN_STOCK_TOKENS = frozenset(
    {
        "in_stock",
        "instock",
        "available",
        "is_available",
        "in_store",
        "true",
        "yes",
        "y",
        "1",
    }
)
_OUT_OF_STOCK_TOKENS = frozenset(
    {
        "out",
        "out_of_stock",
        "outofstock",
        "temporarily_out_of_stock",
        "oos",
        "unavailable",
        "not_available",
        "notavailable",
        "no_stock",
        "nostock",
        "sold_out",
        "soldout",
        "discontinued",
        "delisted",
        "false",
        "no",
        "n",
        "0",
    }
)


def _token(raw: object) -> str | None:
    """`"Out Of Stock"` and `"outOfStock"` are the same token; anything else is not one."""
    if isinstance(raw, bool) or not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    for separator in (" ", "-", ".", "/"):
        text = text.replace(separator, "_")
    return text


def normalize_availability(raw: object) -> Availability:
    """One of the three states. Anything unrecognised -- including `None` -- is `unknown`."""
    token = _token(raw)
    if token is None:
        return UNKNOWN
    if token in _IN_STOCK_TOKENS:
        return IN_STOCK
    if token in _OUT_OF_STOCK_TOKENS:
        return OUT_OF_STOCK
    return UNKNOWN


def as_availability(value: object) -> Availability:
    """Narrow a stored value to the vocabulary, treating anything unrecognised as `unknown`.

    The database column is a portable `String`, so a row written by an older build (or by
    hand) can hold something outside the three states. Rendering that as `unknown` is right:
    it is precisely what an unrecognised answer means, and it keeps one bad row from failing
    a whole search response.
    """
    if value in AVAILABILITY_STATES:
        return value  # type: ignore[return-value]  # membership in the tuple proves the type
    return UNKNOWN


def availability_from_flag(flag: object, level: object = None) -> Availability:
    """A retailer's boolean availability, refined by its stock level where it has one.

    A retailer that says `available: false` is out of stock whatever its level says; a
    retailer that says `available: true` may still label the item `outOfStock`, and that
    more specific answer wins. Neither being interpretable leaves `unknown`.
    """
    flagged = UNKNOWN if flag is None else normalize_availability(str(flag).lower())
    if flagged == OUT_OF_STOCK:
        return OUT_OF_STOCK
    levelled = normalize_availability(level)
    if levelled != UNKNOWN:
        return levelled
    return flagged


# What a shopper can act on, best first. Only relevant when a caller asked for more than one
# state; inside a single state every offer ranks the same and price decides as before. It
# lives here rather than in the search service because more than one surface ranks by it --
# search orders offers, price history orders the stores it charts -- and the rule that an
# offer nobody has confirmed never leads has to be one rule.
AVAILABILITY_RANK = {IN_STOCK: 0, UNKNOWN: 1, OUT_OF_STOCK: 2}


def availability_rank(availability: str) -> int:
    return AVAILABILITY_RANK.get(availability, AVAILABILITY_RANK[UNKNOWN])
