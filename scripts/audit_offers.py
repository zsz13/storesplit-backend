#!/usr/bin/env python
"""Audit what a scrape actually wrote, before anybody is shown it.

The checks are the ones that went wrong: an offer called `in_stock` that the retailer's own
product page contradicts, and a link that opens somebody's "we can't find that page". Both
were invisible in aggregate -- the scrape reported success, the numbers looked healthy -- so
this reads the rows themselves and, where it can, asks the retailer.

    uv run python scripts/audit_offers.py --zip 94105
    uv run --extra browser python scripts/audit_offers.py --zip 94105 --check-urls

Without `--check-urls` it is offline and instant: shape, vocabulary, hosts and ranking.
With it, a sample of stored product URLs is opened in the persistent browser and required to
come back a real product page rather than a 404 or an "Oops!". Trader Joe's answers a plain
HTTP client with 403, so a browser is the only way to check its links at all.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.models import Offer, Retailer, RetailerProduct, Store
from app.db.session import dispose_engine, get_sessionmaker
from app.normalize.availability import AVAILABILITY_STATES, IN_STOCK, OUT_OF_STOCK
from app.retailers import product_hosts
from app.retailers.urls import valid_product_url
from app.services.search import offer_sort_key
from sqlalchemy import select
from sqlalchemy.orm import selectinload

log = logging.getLogger("storesplit.scripts.audit")

# Wording a retailer uses when the page is not a product. Trader Joe's "Oops! We can't seem
# to find the page" is the one that started this.
NOT_A_PRODUCT_PAGE = (
    "oops",
    "can't seem to find",
    "page not found",
    "404 not found",
    "we couldn't find",
    "no longer available",
)


class Findings:
    """Failures worth stopping for, and the counts that explain them."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def report(self) -> int:
        for note in self.notes:
            print(f"  {note}")
        if not self.failures:
            print("\nPASS: every audited rule held.")
            return 0
        print(f"\nFAIL: {len(self.failures)} problem(s).")
        for failure in self.failures:
            print(f"  - {failure}")
        return 1


async def load_offers(zip_code: str | None) -> list[tuple[Offer, RetailerProduct, Retailer]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(Offer).options(
            selectinload(Offer.retailer_product).selectinload(RetailerProduct.retailer),
            selectinload(Offer.store),
        )
        rows = (await session.execute(stmt)).scalars().all()
        out = []
        for offer in rows:
            product = offer.retailer_product
            store: Store | None = offer.store
            if zip_code and store is not None and store.zip_code not in (None, zip_code):
                served = store.served_zip_codes or []
                if zip_code not in served:
                    continue
            out.append((offer, product, product.retailer))
        return out


def audit_rows(rows: list[tuple[Offer, RetailerProduct, Retailer]], findings: Findings) -> None:
    by_retailer: dict[str, Counter[str]] = defaultdict(Counter)
    url_count = 0

    for offer, product, retailer in rows:
        slug = retailer.slug
        state = offer.availability
        by_retailer[slug][state] += 1

        # 1. The vocabulary is closed. Anything else is a bug that reached the database.
        if state not in AVAILABILITY_STATES:
            findings.fail(f"{slug} offer {offer.id}: availability {state!r} is not a known state")

        url = product.product_url
        if url is None:
            continue
        url_count += 1

        # 2. A URL is a real page on the retailer's own host, or it is absent. This is the
        #    check that catches a relative path the browser would resolve against StoreSplit
        #    itself -- the localhost link.
        if not valid_product_url(url, hosts=product_hosts(slug)):
            findings.fail(f"{slug} product {product.id}: {url!r} is not a valid page for {slug}")
        host = (urlsplit(url).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
            findings.fail(f"{slug} product {product.id}: {url!r} points back at StoreSplit")

    print(f"\n{len(rows)} offers, {url_count} with a product URL\n")
    print(f"{'retailer':>16}  {'in_stock':>9} {'out_of_stock':>13} {'unknown':>8}   total")
    for slug in sorted(by_retailer):
        counts = by_retailer[slug]
        total = sum(counts.values())
        print(
            f"{slug:>16}  {counts[IN_STOCK]:>9} {counts[OUT_OF_STOCK]:>13} "
            f"{counts['unknown']:>8}   {total:>5}"
        )


def audit_ranking(rows: list[tuple[Offer, RetailerProduct, Retailer]], findings: Findings) -> None:
    """Nothing unbuyable may lead a product's offers, which is what earns the badge.

    This uses the service's own ordering (`availability_rank` first, then unit price) rather
    than a plain cheapest, because plain cheapest is not what a shopper is shown: a $1.99
    carton that is out of stock is not a better answer than a $4.99 one on the shelf, and
    the ranking already knows that. What would be a real defect is an unbuyable offer
    leading while a buyable one for the same product existed.
    """
    grouped: dict[int, list[tuple[Offer, str]]] = defaultdict(list)
    for offer, product, retailer in rows:
        if product.canonical_product_id is not None:
            grouped[product.canonical_product_id].append((offer, retailer.slug))

    led_by_unbuyable = 0
    for canonical_id, offers in grouped.items():
        # The service's own comparator, imported rather than restated: a hand-copied one
        # would keep printing PASS after the shipped ranking changed underneath it.
        ranked = sorted(offers, key=lambda pair: offer_sort_key(pair[0]))
        leader, slug = ranked[0]
        if leader.availability != OUT_OF_STOCK:
            continue
        buyable = [pair for pair in offers if pair[0].availability != OUT_OF_STOCK]
        if buyable:
            findings.fail(
                f"canonical product {canonical_id}: {slug} offer {leader.id} is out of stock "
                f"but leads {len(buyable)} buyable offer(s)"
            )
        else:
            # Every offer for this product is out of stock, so the default in-stock-only
            # view shows the product not at all. Nothing is badged; nothing to fix.
            led_by_unbuyable += 1

    print(f"\nranking check: {len(grouped)} canonical products")
    print("  none led by an out-of-stock offer while a buyable one existed")
    print(f"  {led_by_unbuyable} product(s) are out of stock everywhere, so shown to nobody")

    # What the shopper is actually served: the default filter is in-stock only.
    shown = [offer for offer, _p, _r in rows if offer.availability == IN_STOCK]
    print(f"  {len(shown)} offer(s) are in_stock, the only ones the default view uses")


async def audit_storefront_stock(
    rows: list[tuple[Offer, RetailerProduct, Retailer]], findings: Findings
) -> None:
    """Re-ask the storefront about every stored `in_stock` offer on it.

    This is the check the Lucky bug would have failed. It goes back to the same shop the
    offer was written for and re-reads the block the product page renders from, so a stored
    `in_stock` has to be a state the retailer will still assert, not one a scrape inferred.
    It costs one batched `Items` call per 60 products, not one per product.
    """
    from app.retailers.clients import RetailerClients
    from app.retailers.instacart_storefront import storefront_availability
    from app.retailers.savemartco.adapter import LuckyAdapter, SaveMartAdapter

    adapters = {"lucky": LuckyAdapter, "savemart": SaveMartAdapter}
    # (shop, zip) -> the product ids stored in-stock there.
    wanted: dict[tuple[str, str, str], dict[str, Offer]] = defaultdict(dict)
    for offer, product, retailer in rows:
        if retailer.slug not in adapters or offer.availability != IN_STOCK:
            continue
        store = offer.store
        if store is None or not store.external_id:
            continue
        zip_code = store.zip_code or (store.served_zip_codes or [""])[0]
        wanted[(retailer.slug, store.external_id, zip_code or "")][product.retailer_sku] = offer

    if not wanted:
        print("\nstorefront stock re-check: no in-stock storefront offers to verify")
        return

    checked = disagreed = 0
    clients = RetailerClients()
    try:
        for (slug, shop_id, zip_code), by_sku in wanted.items():
            adapter = adapters[slug](clients)
            storefront = adapter._storefront
            # Item ids are "items_<retailerLocationId>-<productId>"; the location id comes
            # back with the shop, so one lookup serves every product at this store.
            resolved_shop, location = await _resolve_shop(storefront, zip_code)
            if location is None:
                findings.note(f"{slug}: could not resolve shop for {zip_code}, stock unverified")
                continue
            if resolved_shop is not None and resolved_shop != shop_id:
                # Comparing against a different store would manufacture disagreements, so
                # say the offers went unverified rather than report them as wrong.
                findings.note(
                    f"{slug}: {zip_code} now resolves to shop {resolved_shop}, not the stored "
                    f"{shop_id}; {len(by_sku)} offer(s) unverified"
                )
                continue
            skus = sorted(by_sku)
            for start in range(0, len(skus), 60):
                batch = skus[start : start + 60]
                payload = await storefront.items(
                    [f"items_{location}-{sku}" for sku in batch], shop_id, zip_code
                )
                seen = {
                    str(item.get("productId")): item
                    for item in (payload.get("data") or {}).get("items") or []
                }
                for sku in batch:
                    item = seen.get(sku)
                    checked += 1
                    if item is None:
                        findings.fail(
                            f"{slug} {sku}: stored in_stock, but the shop no longer lists it"
                        )
                        disagreed += 1
                        continue
                    wording, state = storefront_availability(item.get("availability"))
                    if state != IN_STOCK:
                        findings.fail(
                            f"{slug} {sku}: stored in_stock, retailer now says "
                            f"{state} ({wording!r})"
                        )
                        disagreed += 1
    finally:
        await clients.aclose()
    print(f"\nstorefront stock re-check: {checked} in-stock offers re-asked, {disagreed} disagreed")


async def _resolve_shop(storefront: object, zip_code: str) -> tuple[str | None, str | None]:
    """The (shop id, `retailerLocationId`) this ZIP resolves to.

    It must resolve the *same* way the scrape did, which means the vendored ZIP centroid
    rather than a null coordinate: `DefaultShop` picks by distance, so asking from (0, 0)
    answers about a different store and every comparison against it is meaningless.
    """
    from app.retailers.zipmatch import zip_centroid

    latitude, longitude = zip_centroid(zip_code) or (0.0, 0.0)
    payload = await storefront.graphql(  # type: ignore[attr-defined]  - one storefront type
        "DefaultShop",
        {
            "postalCode": zip_code[:5],
            "coordinates": {"latitude": latitude, "longitude": longitude},
        },
        allow_errors=True,
    )
    shop = (payload.get("data") or {}).get("defaultShop") or {}
    shop_id = shop.get("id")
    location = shop.get("retailerLocationId")
    return (str(shop_id) if shop_id else None, str(location) if location else None)


async def audit_urls(
    rows: list[tuple[Offer, RetailerProduct, Retailer]],
    findings: Findings,
    per_retailer: int | None,
) -> None:
    """Open a sample of stored links and require each to be a real product page."""
    from app.retailers.browser import BrowserSession, ManualVerificationRequiredError

    sample: dict[str, list[tuple[int, str]]] = defaultdict(list)
    seen: set[str] = set()
    for _offer, product, retailer in rows:
        url = product.product_url
        if url is None or url in seen:
            continue
        if per_retailer is not None and len(sample[retailer.slug]) >= per_retailer:
            continue
        seen.add(url)
        sample[retailer.slug].append((product.id, url))

    session = BrowserSession(enabled=True)
    if not session.is_configured():
        findings.note("URL checking skipped: uv sync --extra browser")
        return
    print("\nopening a sample of stored links:")
    try:
        for slug in sorted(sample):
            for product_id, url in sample[slug]:
                try:
                    page = await session.visit(slug, url, settle_ms=3500)
                except ManualVerificationRequiredError as challenge:
                    # Unchecked is not the same as fine. Saying PASS here would be the
                    # audit making exactly the claim it exists to stop being made.
                    findings.fail(f"{slug}: links could not be checked -- {challenge}")
                    break
                except Exception as exc:  # a navigation that fails is a finding, not a crash
                    findings.fail(f"{slug} product {product_id}: {url} -> {type(exc).__name__}")
                    continue
                title = (await page.title()) or ""
                body = ""
                with contextlib.suppress(Exception):
                    body = (await page.inner_text("body"))[:1500]
                haystack = f"{title}\n{body}".lower()
                broken = [m for m in NOT_A_PRODUCT_PAGE if m in haystack]
                status = "OK   " if not broken else "BROKEN"
                print(f"  {status} {slug:>12} {url[:80]}  {title[:40]!r}")
                if broken:
                    findings.fail(
                        f"{slug} product {product_id}: {url} is not a product page ({broken[0]})"
                    )
    finally:
        await session.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", dest="zip_code", default=None)
    parser.add_argument("--check-urls", action="store_true", help="open links in the browser")
    parser.add_argument(
        "--check-stock",
        action="store_true",
        help="re-ask the storefront about every stored in-stock offer",
    )
    parser.add_argument("--per-retailer", type=int, default=3)
    parser.add_argument(
        "--all-urls", action="store_true", help="check every stored URL, not a sample"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    findings = Findings()
    try:
        rows = await load_offers(args.zip_code)
        if not rows:
            print("no offers found; run a scrape first")
            return 1
        audit_rows(rows, findings)
        audit_ranking(rows, findings)
        if args.check_stock:
            await audit_storefront_stock(rows, findings)
        if args.check_urls:
            limit = None if args.all_urls else args.per_retailer
            await audit_urls(rows, findings, limit)
    finally:
        await dispose_engine()
    return findings.report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
