"""Deterministic ZIP-code -> nearest-store ranking, shared by every surface that has to
decide which stores a ZIP means: the adapters whose retailer has no ZIP-aware store endpoint,
and the search and basket services choosing among stores already in the database.

Primary rule: great-circle distance from the ZIP's Census ZCTA centroid (vendored, public
domain, `data/zip_centroids.csv`) to the store's own point. A store's point is its
retailer-published coordinates when it has them and the centroid of its own ZIP code when it
does not -- Safeway, Sprouts, Save Mart/Lucky and Walmart publish no coordinates at all, and
ranking them out would delete those retailers from every search. A ZIP centroid is a fact
about that store, accurate to a few miles, and it is never used for anything that claims to
be the store's address (a map pin, for instance): `StorePoint.precision` says which it is.

Fallback, used only when the *searched* ZIP has no centroid: stores in the exact 5-digit ZIP
first, then stores sharing the 3-digit ZIP prefix (USPS sectional center) ordered by numeric
ZIP distance. Ties break on the store id.

The same table is read backwards by `nearest_zip`, which turns a browser's coordinates into
the ZIP whose centroid is closest to them -- so a shopper who shares their location is placed
at exactly the point every subsequent store ranking measures from.
"""

import csv
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.normalize.geo import is_on_earth as _is_on_earth
from app.retailers.base import StoreLocation

CENTROIDS_PATH = Path(__file__).with_name("data") / "zip_centroids.csv"
# The fallback when no caller states a radius. `default_radius_miles()` is what callers
# should use: one knob decides how far a store may be, or search and the scrape that fed it
# can disagree about which stores a ZIP means.
DEFAULT_MAX_MILES = 30.0
_EARTH_RADIUS_MILES = 3958.8

Precision = Literal["exact", "zip_centroid"]


# Callers pass their own accessor rather than satisfying a protocol: `StoreLocation` is a
# frozen dataclass and `Store` is a SQLAlchemy `Mapped[...]` model, and no single structural
# type describes both. `location_point` is the accessor for the dataclass side.


@dataclass(frozen=True)
class StorePoint:
    latitude: float
    longitude: float
    # "exact" is the retailer's own coordinates; "zip_centroid" is good enough to rank a
    # store and not good enough to point at it.
    precision: Precision


@lru_cache
def _centroids() -> dict[str, tuple[float, float]]:
    if not CENTROIDS_PATH.exists():
        return {}
    with CENTROIDS_PATH.open(newline="") as handle:
        rows = csv.DictReader(handle)
        return {row["zip"]: (float(row["lat"]), float(row["lng"])) for row in rows}


# Re-exported so every existing caller keeps importing it from here. The implementation
# moved to `normalize/geo.py` because `normalize/timezones.py` needs it too, and `normalize`
# importing `app.retailers` would drag the whole adapter registry in behind it.
is_on_earth = _is_on_earth


def default_radius_miles() -> float:
    """How far from a ZIP a store may be, from settings. The one knob for that decision."""
    return get_settings().search_store_radius_miles


def zip_centroid(zip_code: str) -> tuple[float, float] | None:
    return _centroids().get(zip_code.strip()[:5])


def store_point(
    latitude: float | None, longitude: float | None, zip_code: str | None
) -> StorePoint | None:
    """Where a store is, and how well that is known. `None` when it cannot be placed.

    A coordinate that is not a place on Earth is treated as no coordinate at all, and the
    store falls back to its ZIP centroid. This is not defensive decoration: `stores_near`
    measures every store in the database on every search, basket and freshness request, and
    `math.sin(inf)` raises -- so a single unusable latitude, from a retailer bug or a
    sentinel value, would have taken down every one of those surfaces for every ZIP until
    somebody repaired the row by hand. `NaN` is worse than an error, because every
    comparison against it is false and the store would simply, silently, stop existing.
    """
    if _is_on_earth(latitude, longitude):
        return StorePoint(float(latitude), float(longitude), "exact")  # type: ignore[arg-type]
    centroid = zip_centroid(zip_code) if zip_code else None
    if centroid is None:
        return None
    return StorePoint(centroid[0], centroid[1], "zip_centroid")


def location_point(store: StoreLocation) -> StorePoint | None:
    return store_point(store.latitude, store.longitude, store.zip_code)


def distance_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def miles_from_zip(point: StorePoint | None, zip_code: str) -> float | None:
    """How far a point is from a ZIP's centroid, or `None` when either is unknown."""
    centroid = zip_centroid(zip_code)
    if centroid is None or point is None:
        return None
    return distance_miles(centroid[0], centroid[1], point.latitude, point.longitude)


def rank_within_radius[StoreT](
    stores: Iterable[StoreT],
    zip_code: str,
    *,
    point: Callable[[StoreT], StorePoint | None],
    max_miles: float = DEFAULT_MAX_MILES,
    tiebreak: Callable[[StoreT], object] = lambda _: 0,
) -> list[tuple[float, StoreT]]:
    """(miles, store) for every store inside the radius, nearest first.

    Empty when the searched ZIP has no centroid: callers decide what to do without a
    distance, and none of them may quietly invent one.
    """
    centroid = zip_centroid(zip_code)
    if centroid is None:
        return []
    lat, lng = centroid
    measured: list[tuple[float, object, StoreT]] = []
    for store in stores:
        placed = point(store)
        if placed is None:
            continue
        miles = distance_miles(lat, lng, placed.latitude, placed.longitude)
        if miles <= max_miles:
            measured.append((miles, tiebreak(store), store))
    measured.sort(key=lambda entry: (entry[0], entry[1]))  # type: ignore[arg-type]  # heterogeneous tiebreak keys
    return [(miles, store) for miles, _, store in measured]


def _id_key(store: StoreLocation) -> tuple[int, str]:
    return (int(store.external_id), "") if store.external_id.isdigit() else (0, store.external_id)


def rank_stores_by_zip(
    stores: list[StoreLocation],
    zip_code: str,
    limit: int = 5,
    max_miles: float | None = None,
) -> list[StoreLocation]:
    max_miles = default_radius_miles() if max_miles is None else max_miles
    zip5 = zip_code.strip()[:5]
    if zip_centroid(zip5) is not None:
        ranked = rank_within_radius(
            stores, zip5, point=location_point, max_miles=max_miles, tiebreak=_id_key
        )
        return [store for _, store in ranked][:limit]
    return _rank_by_zip_prefix(stores, zip5, limit)


def _rank_by_zip_prefix(stores: list[StoreLocation], zip5: str, limit: int) -> list[StoreLocation]:
    target = int(zip5) if zip5.isdigit() else 0
    candidates = [s for s in stores if s.zip_code and len(s.zip_code) >= 5]
    exact = sorted((s for s in candidates if s.zip_code == zip5), key=_id_key)
    prefix = [s for s in candidates if s.zip_code != zip5 and s.zip_code[:3] == zip5[:3]]  # type: ignore[index]
    prefix.sort(key=lambda s: (abs(int(s.zip_code or 0) - target), _id_key(s)))
    return (exact + prefix)[:limit]


# How far a point may be from the nearest ZIP centroid and still be called that ZIP. A ZCTA
# is a few miles across, so anything beyond this is not in a US ZIP at all -- at sea, or in
# another country -- and the honest answer is that there is none.
MAX_REVERSE_MILES = 100.0


def nearest_zip(
    latitude: float | None, longitude: float | None, max_miles: float = MAX_REVERSE_MILES
) -> tuple[str, float] | None:
    """The ZIP whose Census centroid is closest to a point, and how far that is in miles.

    The inverse of `zip_centroid`, over the same vendored table, so a coordinate resolves to
    exactly the ZIP whose centroid `rank_within_radius` would then measure stores from. Any
    other source -- a geocoding service, a state table -- could name a ZIP whose centroid is
    not the nearest one, and StoreSplit would rank stores around a point the shopper is not
    standing at.

    A centroid is an internal point of the ZCTA, not the shopper's own position, so this is
    accurate to the width of a ZIP code and no better. That is the same accuracy every other
    distance in this system already has.

    `None` for a coordinate that is not a place on Earth, and for one with no ZIP within
    `max_miles` -- the Atlantic has no postcode and must not be given the closest one.
    """
    if not _is_on_earth(latitude, longitude):
        return None
    lat, lng = float(latitude), float(longitude)  # type: ignore[arg-type]
    # A degree of latitude is ~69 miles everywhere, so a point outside this band cannot be
    # inside the radius. Filtering on it first is exact and leaves a small fraction of the
    # 33k centroids to measure properly. Longitude is deliberately not filtered: its degrees
    # shrink towards the poles, and a band that is wrong at one latitude would silently drop
    # the right answer.
    band = max_miles / 69.0
    best: tuple[str, float] | None = None
    for zip_code, (clat, clng) in _centroids().items():
        if abs(clat - lat) > band:
            continue
        miles = distance_miles(lat, lng, clat, clng)
        if miles <= max_miles and (best is None or miles < best[1]):
            best = (zip_code, miles)
    return best
