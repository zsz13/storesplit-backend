"""Reading Raley's store directory out of its stores sitemap.

The sitemap slug is the only allowed surface that names a store's address, and it glues the
street type onto the city with no separator: `...-2531-blanding-avenue` + `alameda` + `ca`.
Splitting that by guesswork is what produced store names like "Nob Hill Nuealameda",
"Raley's Reetfairfield" and "Raley's Ivereno", and addresses so wrong that 17 of 114 stores
failed to geocode at all -- which then dropped them out of distance ranking entirely.

So the slug is only a seed now. Candidate readings are offered to the Census geocoder, and
the first one it recognises wins; the store's city, ZIP and street come back from the
geocoder's own canonical answer rather than from the guess that found it.
"""

from scripts.discover_raleys_stores import (
    address_candidates,
    disambiguate,
    fallback_city,
    store_record,
    street_case,
)


def test_a_clean_slug_is_read_as_it_stands() -> None:
    banner, state, candidates = address_candidates("raley_s-4690-freeport-blvd-sacramentoca")

    assert banner == "Raley's" and state == "CA"
    assert candidates[0] == "4690 freeport blvd sacramento"


def test_a_street_type_glued_to_the_city_is_split_at_the_longest_match_first() -> None:
    """ "avenuealameda" is "avenue" + "alameda", never "ave" + "nuealameda"."""
    _, _, candidates = address_candidates("nob-hill-foods-2531-blanding-avenuealamedaca")

    split = [c for c in candidates if "avenue alameda" in c]
    assert split, candidates
    assert candidates.index(split[0]) < next(
        (i for i, c in enumerate(candidates) if "ave nuealameda" in c), len(candidates)
    )


def test_the_glued_token_is_not_always_the_last_one() -> None:
    _, _, candidates = address_candidates("raley_s-100-raley_s-town-centerrohnert-parkca")

    assert any("center rohnert park" in candidate for candidate in candidates)


def test_a_directional_glued_to_the_city_is_split_too() -> None:
    """ "95a-nfernley" is highway 95a north, in Fernley."""
    _, state, candidates = address_candidates("raley_s-1400-us-highway-95a-nfernleynv")

    assert state == "NV"
    assert any(candidate.endswith("95a n fernley") for candidate in candidates)


def test_a_slug_that_is_not_a_store_is_refused() -> None:
    assert address_candidates("some-other-page") is None


def test_the_record_takes_its_city_and_zip_from_the_geocoder_not_the_slug() -> None:
    """The guess that found the match is discarded; the canonical answer is kept."""
    match = {
        "matchedAddress": "2531 BLANDING AVE, ALAMEDA, CA, 94501",
        "addressComponents": {"city": "ALAMEDA", "state": "CA", "zip": "94501"},
        "coordinates": {"x": -122.235163, "y": 37.769440},
    }

    record = store_record("632", "Nob Hill", match)

    assert record is not None
    assert record["name"] == "Nob Hill Alameda"
    assert record["city"] == "Alameda"
    assert record["address_line1"] == "2531 Blanding Ave"
    assert record["zip_code"] == "94501"
    assert record["latitude"] == 37.769440 and record["longitude"] == -122.235163


def test_a_match_without_coordinates_is_no_record() -> None:
    assert store_record("1", "Raley's", {"matchedAddress": "x", "addressComponents": {}}) is None


def test_a_store_the_geocoder_cannot_place_still_gets_its_city_from_the_slug() -> None:
    """The Census database has real gaps -- shopping centres and rural Nevada, mostly.

    Such a store can never be ranked (no coordinates, no ZIP), so this only decides what it
    is called. The city is the tokens after the *last* street type, over whichever reading
    yields the shortest purely alphabetic city -- which is what "ave" beating "avenue" and a
    left-to-right scan got wrong before.
    """
    _, _, rohnert = address_candidates("raley_s-100-raley_s-town-centerrohnert-parkca")
    _, _, fernley = address_candidates("raley_s-1400-us-highway-95a-nfernleynv")
    _, _, tracy = address_candidates("raley_s-2550-south-tracy-blvd-tracyca")

    assert fallback_city(rohnert) == "Rohnert Park"
    assert fallback_city(fernley) == "Fernley"  # not "95A Nfernley"
    assert fallback_city(tracy) == "Tracy"


def test_a_reading_with_no_street_type_at_all_names_no_city() -> None:
    assert fallback_city(["100 nowhere"]) is None


def test_a_street_keeps_its_ordinals_readable() -> None:
    """The geocoder answers in capitals; "777 1ST ST" must not become "777 1St St"."""
    assert street_case("777 1ST ST") == "777 1st St"
    assert street_case("2531 BLANDING AVE") == "2531 Blanding Ave"
    assert street_case("100 N 3RD ST") == "100 N 3rd St"


def test_two_stores_in_one_city_are_told_apart() -> None:
    """Raley's has five stores in Sacramento. Two identical rows in an offer table tell a
    shopper nothing about which shop to walk to."""
    records = [
        {
            "number": "415",
            "name": "Raley's Sacramento",
            "banner": "Raley's",
            "address_line1": "4690 Freeport Blvd",
            "city": "Sacramento",
        },
        {
            "number": "435",
            "name": "Raley's Sacramento",
            "banner": "Raley's",
            "address_line1": "2075 Fair Oaks Blvd",
            "city": "Sacramento",
        },
        {
            "number": "321",
            "name": "Raley's San Pablo",
            "banner": "Raley's",
            "address_line1": "3360 San Pablo Dam Rd",
            "city": "San Pablo",
        },
    ]

    disambiguate(records)

    assert records[0]["name"] == "Raley's Sacramento (Freeport Blvd)"
    assert records[1]["name"] == "Raley's Sacramento (Fair Oaks Blvd)"
    assert records[2]["name"] == "Raley's San Pablo", "a store alone in its city is not renamed"
