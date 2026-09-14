"""Whole Foods store directory.

The site has no public ZIP-to-store endpoint that works without a browser session, but every
store exposes GET /api/stores/<id>/summary. scripts/discover_wholefoods_stores.py walks the
numeric ID range and writes stores.json next to this module. ZIP matching is the shared
deterministic ranking in app/retailers/zipmatch.py.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.retailers.base import StoreLocation
from app.retailers.zipmatch import rank_stores_by_zip

STORES_PATH = Path(__file__).with_name("stores.json")
# Fields of a vendored record that are not part of `StoreLocation`.
_METADATA_KEYS = ("folder",)


def store_from_summary(summary: dict[str, Any]) -> StoreLocation | None:
    store_id = summary.get("storeId")
    location = summary.get("primaryLocation") or {}
    address = location.get("address") or {}
    if not store_id or not summary.get("displayName"):
        return None
    zip_code = (address.get("ZIP_CODE") or address.get("POSTAL_CODE") or "").strip()
    return StoreLocation(
        external_id=str(store_id),
        name=f"Whole Foods {summary['displayName']}",
        address_line1=address.get("STREET_ADDRESS_LINE1"),
        city=address.get("CITY"),
        state=address.get("STATE"),
        zip_code=zip_code[:5] if zip_code else None,
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
    )


@lru_cache
def _records() -> list[dict[str, Any]]:
    if not STORES_PATH.exists():
        return []
    raw = json.loads(STORES_PATH.read_text())
    return raw if isinstance(raw, list) else []


@lru_cache
def load_stores() -> list[StoreLocation]:
    return [
        StoreLocation(**{k: v for k, v in entry.items() if k not in _METADATA_KEYS})
        for entry in _records()
    ]


@lru_cache
def store_folder(external_id: str) -> str | None:
    """The slug this store's own page lives at, e.g. `10432` -> "ocean".

    Vendored, because it is not derivable: the store summary API reports a three-letter
    `folder` ("ocn") that 404s, and the display name only matches the published slug for
    about four stores in five. `scripts/discover_wholefoods_stores.py` resolves it from the
    stores sitemap by reading each page's own `storeCode`. Missing means no hours, not a
    guessed URL.
    """
    for entry in _records():
        if str(entry.get("external_id")) == external_id:
            folder = entry.get("folder")
            return str(folder) if folder else None
    return None


def find_stores_for_zip(zip_code: str, limit: int = 5) -> list[StoreLocation]:
    return rank_stores_by_zip(load_stores(), zip_code, limit)
