"""Coordinates -> ZIP, over the same centroid table that then ranks stores from it.

The answer is the ZIP whose *centroid* is nearest, which is not always the ZIP the point is
legally inside: the Ferry Building stands in 94111 and resolves to 94105, whose internal
point is a quarter of a mile away. That is the intended behaviour and not a rounding error.
StoreSplit uses a ZIP for exactly one thing -- measuring stores from its centroid -- so the
nearest centroid is the point a shopper's stores should be ranked around, and a service that
returned the legally correct ZIP would rank them around a centroid further from where they
are standing.
"""

import pytest
from app.retailers.zipmatch import MAX_REVERSE_MILES, nearest_zip, zip_centroid
from httpx import AsyncClient

# Landmarks, with the sectional centre (3-digit prefix) each falls in. Downtown ZIPs are
# small, so "the nearest centroid" is always one of the handful surrounding the point.
FERRY_BUILDING = (37.7955, -122.3937, "941")
SPACE_NEEDLE = (47.6205, -122.3493, "981")
EMPIRE_STATE = (40.7484, -73.9857, "101")


@pytest.mark.parametrize(
    ("latitude", "longitude", "prefix"), [FERRY_BUILDING, SPACE_NEEDLE, EMPIRE_STATE]
)
def test_nearest_zip_places_a_landmark_in_its_own_neighbourhood(
    latitude: float, longitude: float, prefix: str
) -> None:
    found = nearest_zip(latitude, longitude)
    assert found is not None
    zip_code, miles = found
    assert zip_code.startswith(prefix)
    # Within a city the centroids are dense, so the point it resolves to is close enough that
    # a store ranking from it is the same ranking as from the shopper.
    assert miles < 1


def test_nearest_zip_agrees_with_the_forward_lookup() -> None:
    """A ZIP's own centroid resolves back to that ZIP. The two directions read one table."""
    for zip_code in ("94105", "98109", "10001", "60601", "00601"):
        centroid = zip_centroid(zip_code)
        assert centroid is not None
        assert nearest_zip(*centroid) == (zip_code, pytest.approx(0.0, abs=0.01))


def test_a_point_with_no_zip_gets_none_rather_than_the_closest_one() -> None:
    """The Atlantic has no postcode, and Paris is not in one either."""
    assert nearest_zip(30.0, -40.0) is None
    assert nearest_zip(48.8584, 2.2945) is None
    # And a coordinate that is not a place on Earth never reaches the table.
    assert nearest_zip(None, None) is None
    assert nearest_zip(float("nan"), -122.0) is None


def test_max_miles_is_a_real_ceiling() -> None:
    """Far enough offshore that the default declines, while a wider radius still answers."""
    pacific = (36.0, -124.5)
    assert nearest_zip(*pacific) is None
    assert nearest_zip(*pacific, max_miles=MAX_REVERSE_MILES) is None
    found = nearest_zip(*pacific, max_miles=200.0)
    assert found is not None and found[1] > MAX_REVERSE_MILES


async def test_location_endpoint_returns_the_zip_and_how_far_it_measured(
    client: AsyncClient,
) -> None:
    latitude, longitude, prefix = FERRY_BUILDING
    response = await client.get(
        "/location/zip", params={"latitude": latitude, "longitude": longitude}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["zip_code"].startswith(prefix)
    assert body["latitude"] == latitude and body["longitude"] == longitude
    assert 0 <= body["distance_miles"] < 1


async def test_location_endpoint_404s_where_there_is_no_zip(client: AsyncClient) -> None:
    response = await client.get("/location/zip", params={"latitude": 30.0, "longitude": -40.0})
    assert response.status_code == 404
    assert "ZIP" in response.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {"latitude": 91.0, "longitude": 0.0},
        {"latitude": 0.0, "longitude": 181.0},
        {"latitude": "north", "longitude": 0.0},
        {"longitude": -122.0},
    ],
)
async def test_location_endpoint_rejects_coordinates_that_are_not_coordinates(
    client: AsyncClient, params: dict[str, object]
) -> None:
    assert (await client.get("/location/zip", params=params)).status_code == 422
