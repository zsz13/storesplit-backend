"""Basket comparison: whole basket from one store vs. each item from its cheapest store.

Every decision is arithmetic on stored offers. For each requested item and each store the
cheapest way to cover the requested quantity is the product minimising
    packs * price,   packs = ceil(needed / package size in the comparison unit)
with ties broken by unit price and then offer id, so results are stable across runs.
Items are tracked by position, so the same query may appear more than once.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Offer
from app.normalize.categories import Category, category_for_query
from app.normalize.units import Quantity, canonical_unit, quantize_money
from app.schemas import (
    BasketItemIn,
    BasketItemResult,
    BasketLineOut,
    BasketRequest,
    BasketResponse,
    SplitBasketOut,
    StoreBasketOut,
)
from app.services.search import availability_rank, offer_out, offers_for_category
from app.services.stores import open_now_stores, store_out, stores_near


class BasketItemError(ValueError):
    """A basket item cannot be interpreted (unsupported query or unit)."""


@dataclass(frozen=True)
class RequestedItem:
    query: str
    category: Category
    requested_quantity: Decimal
    requested_unit: str
    needed: Decimal  # in the category's comparison unit


def resolve_item(item: BasketItemIn) -> RequestedItem:
    category = category_for_query(item.query)
    if category is None:
        raise BasketItemError(f"unsupported basket item '{item.query}'")
    unit_raw = item.unit.strip().lower()
    quantity = item.quantity
    if unit_raw in {"item", "items", "each", "ea", "count", "ct"}:
        unit = "count"
    elif unit_raw in {"dozen", "dz"}:
        unit = "count"
        quantity = quantity * 12
    else:
        unit = canonical_unit(unit_raw)
    if unit is None:
        raise BasketItemError(f"unknown unit '{item.unit}'")
    needed = Quantity(quantity, unit).convert_to(category.comparison_unit)
    if needed is None:
        raise BasketItemError(
            f"'{item.query}' is compared per {category.comparison_label}; "
            f"quantity unit '{item.unit}' cannot be converted"
        )
    return RequestedItem(item.query, category, item.quantity, unit_raw, needed)


def packs_needed(needed: Decimal, package_size: Decimal) -> int:
    return max(1, math.ceil(needed / package_size))


def best_line_for_store(item: RequestedItem, offers: list[Offer]) -> BasketLineOut | None:
    """The cheapest way to cover this item at one store, among offers it can be bought from.

    Availability leads the key for the same reason it leads search ordering: a basket is a
    recommendation, so an offer the shopper cannot buy never wins one, even when the caller
    widened the filter to see everything.
    """
    best: tuple[tuple[int, Decimal, Decimal, int], Offer, int, Decimal] | None = None
    for offer in offers:
        product = offer.retailer_product.canonical_product
        if product is None or product.comparison_quantity is None or offer.unit_price is None:
            continue
        packs = packs_needed(item.needed, product.comparison_quantity)
        total = quantize_money(offer.price * packs)
        key = (availability_rank(offer.availability), total, offer.unit_price, offer.id)
        if best is None or key < best[0]:
            best = (key, offer, packs, total)
    if best is None:
        return None
    _, offer, packs, total = best
    product = offer.retailer_product.canonical_product
    assert product is not None
    return BasketLineOut(
        query=item.query,
        category=item.category.key,
        requested_quantity=item.requested_quantity,
        requested_unit=item.requested_unit,
        needed_quantity=item.needed,
        comparison_unit=item.category.comparison_label,
        product_id=product.id,
        product_name=product.normalized_name,
        brand=product.brand,
        offer=offer_out(offer),
        packs=packs,
        line_total=total,
    )


def _line_sort_key(preferred_store_id: int | None):
    """Cheapest line first; on a price tie prefer the cheapest single store so a split
    never sends the shopper to an extra store for nothing."""

    def key(line: BasketLineOut) -> tuple[int, Decimal, Decimal, bool, int]:
        return (
            availability_rank(line.offer.availability),
            line.line_total,
            line.offer.unit_price or Decimal(0),
            line.offer.store.id != preferred_store_id,
            line.offer.id,
        )

    return key


async def compare_basket(db: AsyncSession, request: BasketRequest) -> BasketResponse:
    items = [resolve_item(i) for i in request.items]
    stores = await stores_near(db, request.zip_code)
    # A basket is a shopping trip, so "open now" removes the shops that are shut from the
    # comparison -- and only those: a shop whose hours nobody publishes is not evidence of a
    # closed door, and dropping it would delete whole retailers from the basket. `stores`
    # stays the ZIP's whole set so a client can still say what is shut and when it opens.
    compared = open_now_stores(stores) if request.open_now else stores
    store_by_id = {s.id: s for s in compared}
    store_ids = list(store_by_id)

    # Offers per item index, then the best line per (store, item index).
    # A basket tells a shopper where to go, so an offer it cannot buy must never win.
    # `request.availability` defaults to in_stock; nothing else reaches the comparison.
    offers_by_item: list[list[Offer]] = [
        await offers_for_category(db, item.category.key, store_ids, request.availability)
        for item in items
    ]
    lines: dict[int, dict[int, BasketLineOut]] = {sid: {} for sid in store_ids}
    for index, item in enumerate(items):
        per_store: dict[int, list[Offer]] = {}
        for offer in offers_by_item[index]:
            per_store.setdefault(offer.store_id, []).append(offer)
        for sid, store_offers in per_store.items():
            line = best_line_for_store(item, store_offers)
            if line is not None:
                lines[sid][index] = line

    single_options: list[StoreBasketOut] = []
    for sid in store_ids:
        store_lines = [lines[sid][i] for i in range(len(items)) if i in lines[sid]]
        if not store_lines:
            continue
        missing = [item.query for i, item in enumerate(items) if i not in lines[sid]]
        single_options.append(
            StoreBasketOut(
                store=store_out(store_by_id[sid]),
                total=quantize_money(sum((line.line_total for line in store_lines), Decimal(0))),
                covers_all_items=not missing,
                missing_items=missing,
                lines=store_lines,
            )
        )
    # Availability leads here too, or a store whose basket is merely cheaper on paper beats
    # one the shopper can actually fill: the split (which does rank availability) would then
    # come out dearer than the "cheapest" single store, and the reported saving negative.
    single_options.sort(
        key=lambda o: (
            not o.covers_all_items,
            max((availability_rank(line.offer.availability) for line in o.lines), default=0),
            o.total,
            o.store.id,
        )
    )
    cheapest_single = next((o for o in single_options if o.covers_all_items), None)

    split_lines: list[BasketLineOut] = []
    item_results: list[BasketItemResult] = []
    preferred_store_id = cheapest_single.store.id if cheapest_single else None
    for index, item in enumerate(items):
        options = sorted(
            (lines[sid][index] for sid in store_ids if index in lines[sid]),
            key=_line_sort_key(preferred_store_id),
        )
        cheapest = options[0] if options else None
        if cheapest is not None:
            split_lines.append(cheapest)
        matched = {o.retailer_product.canonical_product_id for o in offers_by_item[index]} - {None}
        item_results.append(
            BasketItemResult(
                query=item.query,
                category=item.category.key,
                category_label=item.category.label,
                needed_quantity=item.needed,
                comparison_unit=item.category.comparison_label,
                matching_products=len(matched),
                cheapest=cheapest,
                options=options,
            )
        )

    cheapest_split: SplitBasketOut | None = None
    if split_lines and len(split_lines) == len(items):
        split_store_ids = sorted({line.offer.store.id for line in split_lines})
        cheapest_split = SplitBasketOut(
            total=quantize_money(sum((line.line_total for line in split_lines), Decimal(0))),
            stores=[store_out(store_by_id[sid]) for sid in split_store_ids],
            lines=split_lines,
        )

    savings = savings_percent = None
    if cheapest_single is not None and cheapest_split is not None:
        savings = quantize_money(cheapest_single.total - cheapest_split.total)
        if cheapest_single.total > 0:
            savings_percent = (savings / cheapest_single.total * 100).quantize(Decimal("0.1"))

    timestamps = [o.scraped_at for offers in offers_by_item for o in offers]
    return BasketResponse(
        zip_code=request.zip_code,
        availability=request.availability,
        open_now=request.open_now,
        stores=[store_out(s) for s in stores],
        items=item_results,
        single_store_options=single_options,
        cheapest_single_store=cheapest_single,
        cheapest_split=cheapest_split,
        savings=savings,
        savings_percent=savings_percent,
        last_updated_at=max(timestamps) if timestamps else None,
        oldest_updated_at=min(timestamps) if timestamps else None,
    )
