"""Stock as the Instacart storefront platform reports it, shared by the banners on it.

Sprouts, Lucky and Save Mart all run white-label Instacart storefronts: the same `Items`
GraphQL query, the same persisted-query hashes, the same payload. They therefore share this
one reading of what that payload says about stock, so a correction lands on all three.

The platform answers with three pieces, and only two of them can be trusted:

* `available` -- a per-shop boolean. Authoritative when it is `false`.
* `stockLevel` -- a token (`inStock`, `highlyInStock`, `lowStock`, `outOfStock`). It goes
  stale independently of the flag: Lucky product 19830961 came back `available: false` with
  `stockLevel: "inStock"` still set. The token alone decides nothing.
* `viewSection.stockLevelLabelString` -- the sentence the storefront prints on the product
  page. This is the shopper-visible verdict, and where the two disagree it is the one the
  retailer stands behind.

Reading the token instead of the label is what made StoreSplit call Lucky 19830961
`in_stock` while its product page said otherwise: the platform spells "Likely out of stock"
as `stockLevel: "lowStock"`, and a generic reading of "low stock" turned a hedge into a
promise. `lowStock` is therefore never `in_stock` here -- and because "likely" is a hedge
rather than a denial, it is `unknown` rather than `out_of_stock`.

Observed across ~6,700 items on both Save Mart Companies banners, this is the whole
vocabulary:

    available  stockLevel      stockLevelLabelString   ->  state
    true       inStock         (none)                      in_stock
    true       highlyInStock   "Many in stock"             in_stock
    true       lowStock        "Likely out of stock"       unknown
    false      outOfStock      "Out of stock"              out_of_stock
    false      inStock         "Out of stock"              out_of_stock   (flag wins)

Anything outside it is `unknown`, per the rule the rest of `app/normalize/availability.py`
keeps: a signal we cannot read is never `in_stock`.
"""

import logging
import re
from typing import Any

from app.normalize.availability import IN_STOCK, OUT_OF_STOCK, UNKNOWN, Availability

log = logging.getLogger("storesplit.retailers.instacart_storefront")

# The storefront's own sentences, lowercased. Only these are understood; a new one is
# `unknown` until somebody has looked at what the page does with it.
_LABELS: dict[str, Availability] = {
    "many in stock": IN_STOCK,
    "likely out of stock": UNKNOWN,  # a hedge: not buyable enough to rank, not a denial
    "out of stock": OUT_OF_STOCK,
}
# Levels that can carry an item to `in_stock`, and only alongside `available: true`.
_STOCKED_LEVELS = frozenset({"instock", "highlyinstock"})
_UNSTOCKED_LEVELS = frozenset({"outofstock"})


def storefront_availability(availability: Any) -> tuple[str | None, Availability]:
    """One `Items` availability block as (the retailer's own wording, normalized state).

    The wording kept is the label the product page shows where there is one, and the raw
    level token otherwise, so a surprising state can be traced to what the payload said.
    """
    if not isinstance(availability, dict):
        return None, UNKNOWN

    view = availability.get("viewSection")
    raw_label = view.get("stockLevelLabelString") if isinstance(view, dict) else None
    label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else None

    level_raw = availability.get("stockLevel")
    level = str(level_raw).strip().lower() if isinstance(level_raw, str) else ""
    wording = label or (str(level_raw) if level_raw else None)

    flag = availability.get("available")
    # `available: false` is the shop saying no, whatever a stale level still claims.
    if flag is False:
        return wording, OUT_OF_STOCK

    labelled = _LABELS.get(label.lower()) if label is not None else None
    if label is not None and labelled is None:
        # A sentence nobody has read yet. It resolves to `unknown`, which quietly drops the
        # offer out of the default view, so say so rather than letting a wording change
        # shrink the catalogue silently.
        log.warning("storefront_unknown_stock_label", extra={"label": label, "level": level})

    if level in _UNSTOCKED_LEVELS:
        # A negative level with a positive label is the two disagreeing. Neither is worth
        # promoting on, so it stays unbuyable: `out_of_stock` when the label agrees or says
        # nothing, `unknown` when the label actively contradicts it.
        return wording, UNKNOWN if labelled == IN_STOCK else OUT_OF_STOCK

    if labelled is not None:
        # The page's own sentence decides, including when it contradicts the level.
        return wording, labelled
    if label is not None:
        return wording, UNKNOWN

    # No sentence to go on: only an explicit `true` flag on a stocked level is `in_stock`.
    if flag is True and level in _STOCKED_LEVELS:
        return wording, IN_STOCK
    return wording, UNKNOWN


# A slug the storefront itself minted: one path segment of the characters these ids use.
# Anything else -- "..", an absolute URL, a placeholder like "null", an encoded separator --
# is not a slug and must not be pasted into a product path, because `clean_product_url`
# checks the assembled URL and would accept `/store/<slug>/products/..` as a valid page on
# the right host. It is a store page, not a product, and the id below is a real one.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")
_SLUG_PLACEHOLDERS = frozenset({"null", "undefined", "none", "nil", "nan", "n/a", "na"})


def storefront_product_path(store_slug: str, product_id: str, evergreen: object = None) -> str:
    """The storefront's own path for an item, preferring the slug it publishes.

    `evergreenUrl` is the slugged segment the storefront links to and the one a bare-id URL
    redirects to (`19830961` -> `19830961-boneless-skinless-master-cut-chicken-breast-3-5-lb`),
    so it is the retailer's canonical spelling rather than a pattern invented here. The id
    path is a real page too, and is the fallback whenever the payload's slug is missing or
    is not a slug.
    """
    slug = evergreen.strip() if isinstance(evergreen, str) else ""
    if slug and slug.lower() not in _SLUG_PLACEHOLDERS and _SLUG_RE.match(slug):
        return f"/store/{store_slug}/products/{slug}"
    return f"/store/{store_slug}/products/{product_id}"
