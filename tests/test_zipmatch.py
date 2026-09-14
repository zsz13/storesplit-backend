from app.retailers.base import StoreLocation
from app.retailers.zipmatch import (
    distance_miles,
    location_point,
    rank_stores_by_zip,
    zip_centroid,
)

SF_7TH = StoreLocation("351", "SF", zip_code="94118", latitude=37.7806, longitude=-122.4661)
DALY = StoreLocation("320", "Daly City", zip_code="94015", latitude=37.6879, longitude=-122.4702)
OAKLAND = StoreLocation("445", "Oakland", zip_code="94611", latitude=37.8272, longitude=-122.2515)
LA = StoreLocation("304", "Glendale", zip_code="91206", latitude=34.1485, longitude=-118.2355)


def test_zip_centroid_and_distance() -> None:
    centroid = zip_centroid("94110")
    oakland = zip_centroid("94611")
    assert centroid is not None and oakland is not None and abs(centroid[0] - 37.75) < 0.05
    assert 5 < distance_miles(*centroid, *oakland) < 15
    assert zip_centroid("00000") is None


def test_rank_by_distance_crosses_zip_prefixes_and_caps_radius() -> None:
    ranked = rank_stores_by_zip([LA, OAKLAND, DALY, SF_7TH], "94110")
    assert [s.external_id for s in ranked] == ["351", "320", "445"]  # LA is beyond 30 miles
    assert rank_stores_by_zip([LA, OAKLAND, DALY, SF_7TH], "94611")[0].external_id == "445"
    assert rank_stores_by_zip([LA, OAKLAND], "94110", limit=1) == [OAKLAND]


def test_prefix_fallback_without_coordinates() -> None:
    stores = [
        StoreLocation("1", "a", zip_code="94118"),
        StoreLocation("2", "b", zip_code="94110"),
        StoreLocation("3", "c", zip_code="90001"),
    ]
    assert [s.external_id for s in rank_stores_by_zip(stores, "94110")] == ["2", "1"]
    assert rank_stores_by_zip(stores, "99999") == []  # unknown ZIP, no coordinates -> prefix only


def test_store_without_coordinates_is_placed_at_its_own_zip_centroid() -> None:
    """Safeway, Sprouts, Lucky and Walmart publish no coordinates.

    Ranking them out would delete those retailers from every search, so a store with a ZIP
    is placed at that ZIP's Census centroid -- a fact about the store, not a default location.
    """
    sprouts_sf = StoreLocation("S1", "Sprouts SF", zip_code="94118")
    sprouts_la = StoreLocation("S2", "Sprouts LA", zip_code="91206")

    near = location_point(sprouts_sf)
    assert near is not None and near.precision == "zip_centroid"
    assert abs(near.latitude - 37.78) < 0.05

    exact = location_point(SF_7TH)
    assert exact is not None and exact.precision == "exact"

    ranked = rank_stores_by_zip([sprouts_la, sprouts_sf], "94110")
    assert [s.external_id for s in ranked] == ["S1"]  # LA is beyond the radius, SF is not


def test_store_with_neither_coordinates_nor_a_known_zip_has_no_point() -> None:
    assert location_point(StoreLocation("X", "nowhere")) is None
    assert location_point(StoreLocation("X", "nowhere", zip_code="00000")) is None
