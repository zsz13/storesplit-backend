"""Trigger a scrape without the HTTP server.

Usage:
    uv run python scripts/scrape.py --zip 94105 [--retailers wholefoods] [--categories eggs milk]
"""

import argparse
import asyncio

from app.config import get_settings
from app.db.session import dispose_engine, get_session_factory
from app.logging import configure_logging
from app.normalize.categories import CATEGORIES
from app.retailers import adapter_slugs
from app.retailers.clients import RetailerClients
from app.services.scraper import run_scrape


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", dest="zip_code", default="94105")
    parser.add_argument("--retailers", nargs="*", choices=adapter_slugs())
    parser.add_argument("--categories", nargs="*", choices=list(CATEGORIES))
    args = parser.parse_args()
    configure_logging(get_settings().log_level)
    # One client pool and one engine for the whole run, closed once at the end.
    try:
        async with RetailerClients() as clients:
            runs = await run_scrape(
                get_session_factory(),
                clients,
                args.zip_code,
                args.retailers or None,
                args.categories or None,
            )
    finally:
        await dispose_engine()
    for run in runs:
        line = (
            f"{run.retailer_slug}: {run.status} "
            f"seen={run.products_seen} offers={run.offers_written}"
        )
        print(line + (f" error={run.error}" if run.error else ""))


if __name__ == "__main__":
    asyncio.run(main())
