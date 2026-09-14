"""Refresh app/retailers/raleys/stores.json.

Raley's publishes `/sitemap/stores-sitemap.xml`, and each store URL carries everything but the
ZIP: `/store/415/raley_s-4690-freeport-blvd-sacramentoca` is store 415, banner Raley's, 4690
Freeport Blvd, Sacramento CA. Nothing on a robots-allowed surface names the ZIP or the
coordinates (the store page renders those from `/api`, which robots.txt disallows), so each
address is geocoded once here with the U.S. Census geocoder - the same public-domain source
this repo already uses for its ZCTA centroids - and the result is vendored.

  uv run python scripts/discover_raleys_stores.py
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.retailers.clients import RetailerClients
from app.retailers.http import request_with_retry
from app.retailers.raleys.adapter import SITE_URL, STORES_PATH

log = logging.getLogger("storesplit.scripts.raleys_stores")

SITEMAP_URL = f"{SITE_URL}/sitemap/stores-sitemap.xml"
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
STORE_URL_RE = re.compile(r"<loc>https://www\.raleys\.com/store/(\d+)/([^<]+)</loc>")
# The slug ends with "<city><state>", e.g. "...-freeport-blvd-sacramentoca".
SLUG_RE = re.compile(r"^(?P<banner>raley_s|bel-air|nob-hill)-(?P<rest>.+?)(?P<state>[a-z]{2})$")
BANNERS = {"raley_s": "Raley's", "bel-air": "Bel Air", "nob-hill": "Nob Hill"}
# Street types that end an address; everything after one is the city.
STREET_SUFFIXES = (
    "ave",
    "avenue",
    "blvd",
    "boulevard",
    "ct",
    "dr",
    "drive",
    "hwy",
    "highway",
    "ln",
    "lane",
    "pkwy",
    "parkway",
    "pl",
    "place",
    "rd",
    "road",
    "st",
    "street",
    "way",
    "circle",
    "cir",
    "center",
    "centre",
    "plaza",
    "square",
    "mall",
    "loop",
    "trail",
    "terrace",
)


# A single-letter directional can be glued to the city too: "...-95a-nfernley" is highway
# 95a north, in Fernley.
DIRECTIONALS = ("n", "s", "e", "w", "ne", "nw", "se", "sw")


def address_candidates(slug: str) -> tuple[str, str, list[str]] | None:
    """(banner, state, readings of the address) for a store URL slug, best guess first.

    The slug glues the street type onto the city with nothing between them, and no rule reads
    every case: `...-2531-blanding-avenue` + `alameda` gives `avenuealameda`, which splits
    correctly at "avenue" and disastrously at "ave". Worse, a city can simply *begin* with a
    street type -- "pleasanton" starts with "pl" -- so splitting whenever it is possible is
    wrong as often as not.

    So nothing is decided here. Each plausible reading is offered to the Census geocoder in
    turn and the first one it recognises wins, which makes a public address database the
    arbiter instead of a list of suffixes. The unsplit reading is offered first, because most
    slugs need no split at all.
    """
    match = SLUG_RE.match(slug)
    if not match:
        return None
    rest = match.group("rest").strip("-").removeprefix("one-market-").removeprefix("foods-")
    words = [word for word in rest.split("-") if word]
    if not words:
        return None
    banner = BANNERS[match.group("banner")]
    candidates = [" ".join(words)]
    for index in range(len(words) - 1, -1, -1):
        word = words[index]
        for prefix in _splittable_prefixes(word):
            split = [*words[:index], prefix, word[len(prefix) :], *words[index + 1 :]]
            candidate = " ".join(split)
            if candidate not in candidates:
                candidates.append(candidate)
    return banner, match.group("state").upper(), candidates


def _splittable_prefixes(word: str) -> list[str]:
    """Street types and directionals this word could begin with, longest first."""
    return sorted(
        (
            prefix
            for prefix in (*STREET_SUFFIXES, *DIRECTIONALS)
            if word.startswith(prefix) and len(word) > len(prefix)
        ),
        key=len,
        reverse=True,
    )


def fallback_city(candidates: list[str]) -> str | None:
    """The city, for a store the geocoder could not place at all.

    The Census address database has real gaps -- shopping centres and rural Nevada, mostly --
    and a store it cannot match has no coordinates and no ZIP, so `zipmatch` can never rank
    it and a shopper can never be sent to it. This decides only what such a store is called.

    The city is whatever follows the *last* street type in a reading, taken over every
    reading and resolved to the shortest purely alphabetic answer. Both of those are what the
    old parser got wrong: it scanned left to right and stopped at the first street type it
    could find, so "1400 us highway 95a nfernley" became a store in "95A Nfernley".
    """
    cities: list[str] = []
    for candidate in candidates:
        words = candidate.split()
        boundaries = [
            index
            for index, word in enumerate(words)
            if word in STREET_SUFFIXES or word in DIRECTIONALS
        ]
        if not boundaries:
            continue
        city = " ".join(words[boundaries[-1] + 1 :])
        if city and all(part.isalpha() for part in city.split()):
            cities.append(city)
    if not cities:
        return None
    return min(cities, key=lambda city: (len(city), city)).title()


def street_case(text: str) -> str:
    """ "777 1ST ST" -> "777 1st St".

    The geocoder answers in capitals, and `str.title()` capitalises the letter after a digit,
    which turns every ordinal into "1St" / "3Rd". A word with a digit in it is simply
    lowercased; a word without one is capitalised.
    """
    return " ".join(word.capitalize() if word.isalpha() else word.lower() for word in text.split())


def disambiguate(records: list[dict[str, Any]]) -> None:
    """Give two stores in the same city different names, in place.

    Raley's has five stores in Sacramento and Bel Air six. Rendered as two offer rows reading
    "Raley's Sacramento" twice, they tell a shopper nothing about which shop to walk to, and
    the whole point of naming the exact store is that it is the exact store.
    """
    counts = Counter(record["name"] for record in records)
    for record in records:
        street = str(record.get("address_line1") or "")
        if counts[record["name"]] > 1 and street:
            # The street without its number: "4690 Freeport Blvd" -> "Freeport Blvd".
            words = street.split()
            label = " ".join(words[1:]) if words and words[0][:1].isdigit() else street
            record["name"] = f"{record['name']} ({label})"


def store_record(number: str, banner: str, match: dict[str, Any]) -> dict[str, Any] | None:
    """One vendored store, taken from the geocoder's canonical answer.

    The candidate reading that found the match is thrown away: it was a search term, not a
    fact. Street, city, state and ZIP all come back from the geocoder, which is why a store
    can no longer end up named after a mis-split slug.
    """
    coordinates = match.get("coordinates") or {}
    components = match.get("addressComponents") or {}
    city = str(components.get("city") or "").title()
    state = str(components.get("state") or "").upper()
    zip_code = str(components.get("zip") or "")
    if "x" not in coordinates or "y" not in coordinates or not city or not zip_code:
        return None
    street = street_case(str(match.get("matchedAddress") or "").split(",")[0].strip())
    return {
        "number": number,
        "name": f"{banner} {city}",
        "banner": banner,
        "address_line1": street or None,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "latitude": float(coordinates["y"]),
        "longitude": float(coordinates["x"]),
    }


async def geocode(client: httpx.AsyncClient, address: str, state: str) -> dict[str, Any] | None:
    """The Census geocoder's first match for one reading of an address."""
    response = await request_with_retry(
        client,
        "GET",
        GEOCODER_URL,
        params={
            "address": f"{address}, {state}",
            "benchmark": "Public_AR_Current",
            "format": "json",
        },
        headers={"Accept": "application/json"},
        max_retries=1,
    )
    if response.status_code != 200:
        return None
    matches = (response.json().get("result") or {}).get("addressMatches") or []
    return matches[0] if matches else None


async def resolve_store(
    client: httpx.AsyncClient, number: str, slug: str, delay: float
) -> dict[str, Any] | None:
    parsed = address_candidates(slug)
    if parsed is None:
        log.warning("store %s: cannot read slug %r", number, slug)
        return None
    banner, state, candidates = parsed
    for candidate in candidates:
        match = await geocode(client, candidate, state)
        await asyncio.sleep(delay)
        if match is None:
            continue
        record = store_record(number, banner, match)
        if record is not None:
            return record
    log.warning("store %s: no reading of %r geocoded", number, slug)
    city = fallback_city(candidates)
    return {
        "number": number,
        "name": f"{banner} {city}" if city else banner,
        "banner": banner,
        "address_line1": street_case(candidates[0]),
        "city": city,
        "state": state,
        "zip_code": None,
        "latitude": None,
        "longitude": None,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=STORES_PATH)
    parser.add_argument("--delay", type=float, default=0.3, help="pause between geocoder calls")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    records: list[dict[str, object]] = []
    async with RetailerClients() as clients:
        client = clients.shared()
        response = await request_with_retry(
            client, "GET", SITEMAP_URL, headers={"Accept": "text/xml"}
        )
        response.raise_for_status()
        entries = STORE_URL_RE.findall(response.text)
        log.info("%d stores in the sitemap", len(entries))

        for number, slug in entries:
            record = await resolve_store(client, number, slug, args.delay)
            if record is not None:
                records.append(record)

    records.sort(key=lambda record: int(str(record["number"])))
    disambiguate(records)
    args.out.write_text(json.dumps(records, indent=2) + "\n")
    located = sum(1 for record in records if record["zip_code"])
    log.info("wrote %s (%d stores, %d geocoded)", args.out, len(records), located)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
