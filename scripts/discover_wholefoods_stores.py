"""Rebuild app/retailers/wholefoods/stores.json from public Whole Foods surfaces.

Usage:  uv run python scripts/discover_wholefoods_stores.py [--start 10000] [--end 10800]
        uv run python scripts/discover_wholefoods_stores.py --folders-only

Two passes, both robots-allowed:

* the numeric store ID range against `/api/stores/<id>/summary`, which gives each store's
  name, address, ZIP and coordinates -- what ZIP lookup needs;
* the published stores sitemap, whose `/stores/<slug>` pages each state their own
  `storeCode`, which is the only way to learn which slug belongs to which store. The summary
  endpoint's own `folder` is a three-letter code ("ocn") whose URL 404s, and a store's
  display name matches its published slug for only about four stores in five, so the mapping
  is resolved once here rather than guessed at scrape time. It is what lets the adapter read
  a store's opening hours.

Takes a few minutes; run occasionally, never per scrape. `--folders-only` keeps the existing
directory and refreshes just the slugs.
"""

import argparse
import asyncio
import json
import re
from dataclasses import asdict
from functools import partial
from typing import Any

from app.concurrency import gather_bounded
from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.wholefoods.adapter import BASE_URL, WholeFoodsAdapter, parse_store_details
from app.retailers.wholefoods.stores import STORES_PATH

SITEMAP_URL = f"{BASE_URL}/sitemap/sitemap-stores.xml"
_SLUG_RE = re.compile(rf"<loc>{re.escape(BASE_URL)}/stores/([^<]+)</loc>")


async def store_slugs(client: Any) -> list[str]:
    response = await request_with_retry(client, "GET", SITEMAP_URL, headers={"Accept": "text/xml"})
    if response.status_code != 200:
        return []
    return sorted(set(_SLUG_RE.findall(response.text)))


async def folder_for_slug(client: Any, slug: str) -> tuple[str, str] | None:
    """(store external id, slug), read from the page's own `storeCode`."""
    response = await request_with_retry(
        client, "GET", f"{BASE_URL}/stores/{slug}", headers={"Accept": "text/html"}, max_retries=1
    )
    if response.status_code != 200:
        return None
    details = parse_store_details(response.text)
    return (details.external_id, slug) if details else None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=10000)
    parser.add_argument("--end", type=int, default=10800)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--folders-only",
        action="store_true",
        help="keep the vendored directory and refresh only the store-page slugs",
    )
    args = parser.parse_args()

    async with RetailerClients() as clients:
        adapter = WholeFoodsAdapter(clients)
        client = clients.shared()
        if args.folders_only:
            records: list[dict[str, Any]] = json.loads(STORES_PATH.read_text())
        else:
            summaries = await gather_bounded(
                args.workers,
                [
                    partial(adapter.fetch_store_summary, store_id)
                    for store_id in range(args.start, args.end)
                ],
            )
            found = [store for store in summaries if store]
            found.sort(key=lambda s: int(s.external_id))
            records = [asdict(store) for store in found]

        slugs = await store_slugs(client)
        resolved = await gather_bounded(
            args.workers, [partial(folder_for_slug, client, slug) for slug in slugs]
        )
    folders = {store_id: slug for store_id, slug in (r for r in resolved if r)}
    for record in records:
        folder = folders.get(str(record.get("external_id")))
        if folder:
            record["folder"] = folder
        else:
            record.pop("folder", None)

    STORES_PATH.write_text(json.dumps(records, indent=1) + "\n")
    with_folder = sum(1 for record in records if record.get("folder"))
    print(f"wrote {len(records)} stores to {STORES_PATH} ({with_folder} with a store-page slug)")


if __name__ == "__main__":
    asyncio.run(main())
