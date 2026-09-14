"""Refresh app/retailers/safeway/seeds.json.

Safeway answers no keyword search (Imperva null-routes pgmsearch and the aisle listing), so the
adapter lists a category by asking `v1/aisles/similar-products` for the shelf neighbours of a
seed product. This script picks those seeds from Safeway's own published product sitemaps:

  1. read /shop/sitemaps/product-sitemap-{0..4}.xml (robots.txt advertises them),
  2. keep slugs whose words match a category's include/exclude rules,
  3. ask similar-products what each candidate actually returns,
  4. keep the candidates that return the most products on a shelf named after the category.

Step 4 matters: similar-products is a recommender, not a shelf listing, and its answer quality
varies per seed - a butter seed can come back with a page of ground coffee. Seeds are therefore
judged on their own output rather than on their own shelf.

Run it when a category stops returning results:  uv run python scripts/discover_safeway_seeds.py
"""

import argparse
import asyncio
import collections
import json
import logging
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.normalize.categories import CATEGORIES
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.safeway.adapter import SEEDS_PATH, SITE_URL, similar_products

log = logging.getLogger("storesplit.scripts.safeway_seeds")

SITEMAP_URL = f"{SITE_URL}/shop/sitemaps/product-sitemap-{{index}}.xml"
SITEMAP_COUNT = 5
LOC_RE = re.compile(r"<loc>https://www\.safeway\.com/shop/pd/([^/<]+)/(\d+)</loc>")
CANDIDATES_PER_CATEGORY = 40
SEEDS_PER_CATEGORY = 4
MIN_ON_SHELF = 2
# The slug rules alone are not enough: "butter stripe coupe glass" and "easter egg dye" pass
# the category regexes. A shelf counts for a category only when its name contains this word.
SHELF_KEYWORD = {
    "eggs": "egg",
    "milk": "milk",
    "chicken_breast": "chicken",
    "rice": "rice",
    "bread": "bread",
    "butter": "butter",
    "bananas": "banana",
}


def matches_category(slug: str, key: str) -> bool:
    category = CATEGORIES[key]
    title = slug.replace("-", " ")
    if not any(re.search(pattern, title, re.I) for pattern in category.include):
        return False
    return not any(re.search(pattern, title, re.I) for pattern in category.exclude)


async def score_seed(
    client: httpx.AsyncClient, pid: str, store_id: str, keyword: str
) -> tuple[int, str] | None:
    """(number of docs on a matching shelf, that shelf) for one candidate seed."""
    try:
        payload = await similar_products(client, pid, store_id)
    except httpx.HTTPError as exc:
        log.warning("seed %s failed: %s", pid, exc)
        return None
    shelves = collections.Counter(
        str(doc.get("shelfNameWithId") or "")
        for doc in (payload.get("response") or {}).get("docs") or []
        if keyword in str(doc.get("shelfNameWithId") or "").casefold()
    )
    if not shelves:
        return None
    shelf, count = shelves.most_common(1)[0]
    return count, shelf


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="1490", help="store id used to score candidates")
    parser.add_argument("--out", type=Path, default=SEEDS_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    seeds: dict[str, list[dict[str, str]]] = {}
    async with RetailerClients() as clients:
        client = clients.shared()
        products: list[tuple[str, str]] = []
        for index in range(SITEMAP_COUNT):
            response = await request_with_retry(
                client, "GET", SITEMAP_URL.format(index=index), headers={"Accept": "text/xml"}
            )
            response.raise_for_status()
            products.extend(LOC_RE.findall(response.text))
        log.info("%d products in the sitemaps", len(products))

        for key, category in CATEGORIES.items():
            keyword = SHELF_KEYWORD[key]
            candidates = [pid for slug, pid in products if matches_category(slug, key)]
            scored: list[tuple[int, str, str]] = []
            for pid in candidates[:CANDIDATES_PER_CATEGORY]:
                result = await score_seed(client, pid, args.store, keyword)
                if result is not None and result[0] >= MIN_ON_SHELF:
                    scored.append((result[0], pid, result[1]))
                if len(scored) >= SEEDS_PER_CATEGORY:
                    break
            if not scored:
                log.warning(
                    "%s: none of %d candidates returned %d+ %r products",
                    key,
                    len(candidates),
                    MIN_ON_SHELF,
                    keyword,
                )
                continue
            scored.sort(reverse=True)
            seeds[category.search_query] = [
                {"pid": pid, "shelf": shelf} for _count, pid, shelf in scored
            ]
            log.info(
                "%s -> %d seeds, best %d products on %r",
                key,
                len(scored),
                scored[0][0],
                scored[0][2],
            )

    args.out.write_text(json.dumps(seeds, indent=2, sort_keys=True) + "\n")
    log.info("wrote %s (%d queries)", args.out, len(seeds))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
