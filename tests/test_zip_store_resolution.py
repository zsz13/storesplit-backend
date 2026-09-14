"""The selected ZIP decides which stores are searched, for Raley's and Whole Foods.

Both retailers resolve stores from a vendored directory rather than a ZIP-aware endpoint, so
these run offline against the same data a scrape uses. They are the regression for the two
reported faults: Raley's appearing not to use the chosen ZIP, and Whole Foods being read at
whatever store a previous request had left behind.
"""

from collections.abc import Callable

import pytest
from app.retailers.base import StoreLocation
from app.retailers.raleys.adapter import store_directory
from app.retailers.wholefoods.stores import find_stores_for_zip
from app.retailers.zipmatch import (
    DEFAULT_MAX_MILES,
    miles_from_zip,
    rank_stores_by_zip,
    store_point,
)

type Resolver = Callable[[str], list[StoreLocation]]

# Two materially different ZIPs in the same metro, and one 90 miles inland.
DALY_CITY = "94014"
SAN_FRANCISCO = "94105"
SACRAMENTO = "95814"


def raleys_stores(zip_code: str) -> list[StoreLocation]:
    return rank_stores_by_zip(list(store_directory()), zip_code)


def wholefoods_stores(zip_code: str) -> list[StoreLocation]:
    return find_stores_for_zip(zip_code)


RESOLVERS = [raleys_stores, wholefoods_stores]


def ids(stores: list[StoreLocation]) -> list[str]:
    return [store.external_id for store in stores]


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_a_different_zip_resolves_to_a_different_nearest_store(resolve: Resolver) -> None:
    downtown = ids(resolve(SAN_FRANCISCO))
    inland = ids(resolve(SACRAMENTO))

    assert downtown and inland
    assert downtown[0] != inland[0]
    assert set(downtown).isdisjoint(inland), "90 miles apart shares no store"


def test_wholefoods_neighbouring_zips_get_their_own_stores() -> None:
    """94014 is Daly City and 94105 is downtown: close together, and not the same stores."""
    daly_city = ids(wholefoods_stores(DALY_CITY))
    downtown = ids(wholefoods_stores(SAN_FRANCISCO))

    assert daly_city[:2] == ["10432", "10717"]  # Ocean, Stonestown
    assert downtown[:2] == ["10151", "10718"]  # SoMa, Trinity
    assert daly_city[0] not in downtown[:2]


def test_raleys_neighbouring_zips_order_their_stores_by_distance() -> None:
    """Raley's has no store in San Francisco, so the two ZIPs share candidates -- in a
    different order, because the order is what decides which two a scrape visits."""
    daly_city = ids(raleys_stores(DALY_CITY))
    downtown = ids(raleys_stores(SAN_FRANCISCO))

    assert daly_city[:2] == ["632", "628"]  # Alameda, then Redwood City
    assert downtown[:2] == ["632", "321"]  # Alameda, then San Pablo
    assert daly_city[:2] != downtown[:2]


@pytest.mark.parametrize("resolve", RESOLVERS)
@pytest.mark.parametrize("zip_code", [DALY_CITY, SAN_FRANCISCO, SACRAMENTO])
def test_every_resolved_store_is_inside_the_radius(resolve: Resolver, zip_code: str) -> None:
    for store in resolve(zip_code):
        miles = miles_from_zip(
            store_point(store.latitude, store.longitude, store.zip_code), zip_code
        )
        assert miles is not None and miles <= DEFAULT_MAX_MILES


def test_a_zip_with_no_store_of_a_retailer_nearby_gets_none_rather_than_a_distant_one() -> None:
    """New York. Neither retailer trades there, and neither offers a substitute."""
    assert raleys_stores("10001") == []
    assert wholefoods_stores("10001") != []  # Whole Foods does trade in Manhattan
    assert set(ids(wholefoods_stores("10001"))).isdisjoint(ids(wholefoods_stores(SAN_FRANCISCO)))
