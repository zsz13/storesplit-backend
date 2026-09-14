"""Refresh app/retailers/raleys/catalogue.json.

`/search` is disallowed by Raley's robots.txt, so the adapter cannot ask the site for a
category. It reads instead from Raley's own published product sitemaps, which robots.txt
advertises: `/sitemap/products-sitemap.xml` fans out to one sitemap per department
(`/sitemap/products/PMC7/products-sitemap.xml` is Dairy & Eggs), and every entry is a
`/product/<id>/<slug>` URL whose slug is the product name.

For each supported category this script searches only the departments that can hold it and
keeps the product ids whose slug passes that category's own include/exclude rules, so the
adapter's per-store price lookups are spent on plausible products.

  uv run python scripts/discover_raleys_products.py
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.normalize.categories import CATEGORIES
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.raleys.adapter import CATALOGUE_PATH, SITE_URL

log = logging.getLogger("storesplit.scripts.raleys_products")

INDEX_URL = f"{SITE_URL}/sitemap/products-sitemap.xml"
PRODUCT_RE = re.compile(r"<loc>https://www\.raleys\.com/product/(\d+)/([^<]+)</loc>")
SITEMAP_RE = re.compile(r"<loc>(https://www\.raleys\.com/sitemap/products/(PMC\d+)/[^<]+)</loc>")
PRODUCTS_PER_CATEGORY = 40
# Departments that can hold each category, so "chicken flavoured rice" in the pantry sitemap
# never lands in chicken_breast.  PMC4 bakery-bread, PMC7 dairy-eggs, PMC12 meat-seafood,
# PMC13 pantry-essentials, PMC16 produce.
CATEGORY_DEPARTMENTS = {
    "eggs": ("PMC7",),
    "milk": ("PMC7",),
    "butter": ("PMC7",),
    "chicken_breast": ("PMC12",),
    "rice": ("PMC13",),
    "bread": ("PMC4",),
    "bananas": ("PMC16",),
}


def matches_category(slug: str, key: str) -> bool:
    category = CATEGORIES[key]
    title = slug.replace("-", " ").replace("_", " ")
    if not any(re.search(pattern, title, re.I) for pattern in category.include):
        return False
    return not any(re.search(pattern, title, re.I) for pattern in category.exclude)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=CATALOGUE_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    catalogue: dict[str, list[str]] = {}
    async with RetailerClients() as clients:
        client = clients.shared()
        index = await request_with_retry(client, "GET", INDEX_URL, headers={"Accept": "text/xml"})
        index.raise_for_status()
        wanted = {key for keys in CATEGORY_DEPARTMENTS.values() for key in keys}
        by_department: dict[str, list[tuple[str, str]]] = {}
        for url, department in SITEMAP_RE.findall(index.text):
            if department not in wanted:
                continue
            response = await request_with_retry(client, "GET", url, headers={"Accept": "text/xml"})
            response.raise_for_status()
            by_department[department] = PRODUCT_RE.findall(response.text)
            log.info("%s: %d products", department, len(by_department[department]))

        for key, category in CATEGORIES.items():
            products: list[str] = []
            for department in CATEGORY_DEPARTMENTS[key]:
                products.extend(
                    product_id
                    for product_id, slug in by_department.get(department, [])
                    if matches_category(slug, key)
                )
            unique = list(dict.fromkeys(products))[:PRODUCTS_PER_CATEGORY]
            catalogue[category.search_query] = unique
            log.info("%s -> %d products", key, len(unique))

    args.out.write_text(json.dumps(catalogue, indent=2, sort_keys=True) + "\n")
    log.info("wrote %s (%d queries)", args.out, len(catalogue))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
