"""The Google Maps link on an offer: which supermarket it opens, and when it declines to say.

A map link is a claim about a *business*, not about a patch of ground. The ladder in
`services/maps.py` is: a place the retailer published, then a place resolved and checked
against the store, then an address search that names the retailer, then a coordinate pin,
then nothing. Each rung down gives up some certainty and says so.

The bug these tests pin: the link used to be built from `store.name` alone, which for Target
is "San Francisco Stonestown" -- a phrase containing no supermarket -- so Google resolved the
street address instead of the shop. A brand plus a street address is what fixes that without
a place id, and Target's own `google_cid` is what fixes it with one.
"""

from datetime import UTC, datetime

from app.db.models import Retailer, Store
from app.services.maps import (
    MapsPlace,
    PlaceQuery,
    address_matches,
    brand_matches,
    maps_url,
    pick_place,
    place_from_retailer,
    search_query,
)
from app.services.stores import store_out


def _store(retailer: Retailer | None = None, **fields: object) -> Store:
    base: dict[str, object] = {
        "retailer_id": 1,
        "external_id": "1",
        "name": "Whole Foods SoMa",
        "address_line1": None,
        "city": None,
        "state": None,
        "zip_code": None,
        "latitude": None,
        "longitude": None,
        "maps_place_url": None,
        "maps_place_id": None,
        "maps_source": None,
    }
    store = Store(**{**base, **fields})
    store.retailer = retailer or Retailer(slug="wholefoods", name="Whole Foods Market")
    return store


def _target(**fields: object) -> Store:
    """The store from the report: Target's branch label names no supermarket."""
    return _store(
        Retailer(slug="target", name="Target"),
        name="San Francisco Stonestown",
        address_line1="285 Winston Dr",
        city="San Francisco",
        state="CA",
        zip_code="94132",
        latitude=37.726614,
        longitude=-122.476464,
        **fields,
    )


# ------------------------------------------------------- rung 1: the retailer's own place


def test_a_place_the_retailer_published_is_the_link() -> None:
    store = _target(
        maps_place_url="https://maps.google.com/maps?cid=10195751074682041949",
        maps_source="target:sl-page/store",
    )

    assert maps_url(store) == "https://maps.google.com/maps?cid=10195751074682041949"


def test_a_stored_place_beats_the_coordinates_and_the_address() -> None:
    """A pin is a location; a place is a business. The business wins whenever there is one."""
    store = _target(maps_place_url="https://maps.google.com/maps?cid=10195751074682041949")

    assert "cid=" in (maps_url(store) or "")
    assert "285" not in (maps_url(store) or "")


def test_a_google_cid_url_is_read_as_a_place_with_its_id() -> None:
    place = place_from_retailer(
        "https://maps.google.com/maps?cid=10195751074682041949", source="target:sl-page/store"
    )

    assert place is not None
    assert place.place_id == "10195751074682041949"
    assert place.source == "target:sl-page/store"


def test_a_bare_place_id_becomes_googles_own_documented_place_url() -> None:
    """Safeway publishes `googlePlaceId`, not a link. Google's Maps URLs API takes it."""
    place = place_from_retailer("ChIJKfpyiGCAhYARNyu9cCeqEbk", source="safeway:local-page")

    assert place is not None
    assert place.place_id == "ChIJKfpyiGCAhYARNyu9cCeqEbk"
    assert place.url == (
        "https://www.google.com/maps/search/?api=1&query=ChIJKfpyiGCAhYARNyu9cCeqEbk"
        "&query_place_id=ChIJKfpyiGCAhYARNyu9cCeqEbk"
    )


def test_http_is_upgraded_because_the_value_becomes_an_href() -> None:
    place = place_from_retailer("http://maps.google.com/maps?cid=123", source="x")

    assert place is not None and place.url.startswith("https://")


def test_a_value_that_is_not_a_maps_place_is_refused_rather_than_stored() -> None:
    """The column becomes an `href`; the same gate every product URL passes."""
    for raw in (
        "",
        None,
        "not a url",
        "https://evil.test/maps?cid=1",
        "https://maps.google.com\\@evil.test/maps",
        "javascript:alert(1)",
        "//maps.google.com/maps?cid=1",
        # A Google host is not a map. Without a maps path or a place in the query this is an
        # off-site destination wearing the label of one.
        "https://www.google.com/url?q=https://evil.test",
        "https://maps.google.com/local/business/redirect?url=https://evil.test",
        # A shortener's whole job is to redirect somewhere nobody here has seen.
        "https://maps.app.goo.gl/abcdef",
    ):
        assert place_from_retailer(raw, source="x") is None, raw


def test_a_bare_cid_is_a_cid_and_not_a_place_id() -> None:
    """They are different identifiers. Passing a CID as `query_place_id` builds a link that
    resolves to nothing while looking exactly as authoritative as one that works."""
    place = place_from_retailer("10195751074682041949", source="target:sl-page/store")

    assert place is not None
    assert place.url == "https://maps.google.com/maps?cid=10195751074682041949"
    assert "query_place_id" not in place.url


def test_a_place_that_shares_its_address_with_another_of_the_same_brand_is_refused() -> None:
    """285 Winston Dr is a mall: the Target and the Target Optical inside it share the street
    and the brand. Picking either would be guessing a store from a coordinate."""
    payload = {
        "places": [
            {
                "id": "ChIJtargetoptical1",
                "displayName": {"text": "Target Optical"},
                "formattedAddress": "285 Winston Dr, San Francisco, CA 94132, USA",
            },
            {
                "id": "ChIJtargetmobile01",
                "displayName": {"text": "Target Mobile"},
                "formattedAddress": "285 Winston Dr, San Francisco, CA 94132, USA",
            },
        ]
    }

    assert pick_place(payload, TARGET_QUERY) is None


def test_the_shop_itself_beats_a_concession_sharing_its_door() -> None:
    """A name that *is* the retailer outranks one that merely contains it."""
    payload = {
        "places": [
            {
                "id": "ChIJtargetoptical1",
                "displayName": {"text": "Target Optical"},
                "formattedAddress": "285 Winston Dr, San Francisco, CA 94132, USA",
            },
            {
                "id": "ChIJtargetstonestown",
                "displayName": {"text": "Target"},
                "formattedAddress": "285 Winston Dr, San Francisco, CA 94132, USA",
            },
        ]
    }

    place = pick_place(payload, TARGET_QUERY)
    assert place is not None and place.place_id == "ChIJtargetstonestown"


def test_a_stored_place_that_stopped_validating_falls_back_rather_than_breaking() -> None:
    """Defence in depth: a bad row yields a working search link, not a broken destination."""
    store = _target(maps_place_url="https://evil.test/maps?cid=1")

    url = maps_url(store)
    assert url is not None and url.startswith("https://www.google.com/maps/search/")
    assert "Target" in url


# --------------------------------------------- rung 3: an address search naming the retailer


def test_the_search_query_leads_with_the_retailer_not_the_branch_label() -> None:
    """The reported bug, at its source: "San Francisco Stonestown, 285 Winston Dr" resolves
    to a street, because no part of it is the name of a shop."""
    assert search_query(_target()) == (
        "Target San Francisco Stonestown, 285 Winston Dr, San Francisco, CA 94132"
    )


def test_the_fallback_link_is_a_maps_search_for_that_query() -> None:
    url = maps_url(_target())

    assert url == (
        "https://www.google.com/maps/search/?api=1&query=Target+San+Francisco+Stonestown"
        "%2C+285+Winston+Dr%2C+San+Francisco%2C+CA+94132"
    )


def test_a_branch_that_already_names_its_retailer_is_not_prefixed_again() -> None:
    """Most adapters already write the retailer into the branch. Gluing it on a second time
    gave "Whole Foods Market Whole Foods SoMa" -- harmless to a search, and not something to
    put in front of a shopper or a place resolver."""
    for retailer, branch, expected in [
        ("Safeway", "Safeway", "Safeway"),
        ("Whole Foods Market", "Whole Foods SoMa", "Whole Foods SoMa"),
        ("Smart & Final", "Smart & Final Daly City", "Smart & Final Daly City"),
        ("99 Ranch Market", "99 Ranch Market Richmond", "99 Ranch Market Richmond"),
        ("Raley's", "Raley's San Pablo", "Raley's San Pablo"),
        # A banner of the same retailer under another name still needs its parent named.
        ("Raley's", "Nob Hill Alameda", "Raley's Nob Hill Alameda"),
        # And the case the whole change exists for.
        ("Target", "San Francisco Stonestown", "Target San Francisco Stonestown"),
    ]:
        store = _store(
            Retailer(slug="x", name=retailer),
            name=branch,
            address_line1="1 Main St",
            city="San Francisco",
            state="CA",
            zip_code="94105",
        )
        assert search_query(store) == f"{expected}, 1 Main St, San Francisco, CA 94105"


# ------------------------------------------------------- rungs 4 and 5: pins, and silence


def test_coordinates_are_the_last_resort_not_the_first() -> None:
    """A pin is a place on Earth rather than a business, so an address outranks it."""
    with_address = _target()
    assert "maps/search/?api=1&query=Target" in (maps_url(with_address) or "")

    coordinates_only = _store(latitude=37.781321, longitude=-122.39964)
    assert maps_url(coordinates_only) == (
        "https://www.google.com/maps/search/?api=1&query=37.781321%2C-122.39964"
    )


def test_a_zip_alone_is_not_an_address_and_gets_no_link() -> None:
    """The ZIP centroid places this store for ranking; it does not say where its door is."""
    assert maps_url(_store(zip_code="94107")) is None
    assert maps_url(_store(city="San Francisco", state="CA", zip_code="94107")) is None


def test_a_store_with_nothing_to_point_at_gets_no_link() -> None:
    assert maps_url(_store()) is None


# --------------------------------------------------- rung 2: resolving, and refusing to


TARGET_QUERY = PlaceQuery(
    retailer_name="Target",
    store_name="San Francisco Stonestown",
    address_line1="285 Winston Dr",
    city="San Francisco",
    state="CA",
    zip_code="94132",
    latitude=37.726614,
    longitude=-122.476464,
)


def test_a_resolved_place_is_taken_only_when_brand_and_street_both_match() -> None:
    payload = {
        "places": [
            {
                "id": "ChIJnailsalon",
                "displayName": {"text": "Winston Nails"},
                "formattedAddress": "285 Winston Dr, San Francisco, CA 94132, USA",
            },
            {
                "id": "ChIJtargetstonestown",
                "displayName": {"text": "Target"},
                "formattedAddress": "285 Winston Dr, San Francisco, CA 94132, USA",
            },
        ]
    }

    place = pick_place(payload, TARGET_QUERY)

    assert isinstance(place, MapsPlace)
    assert place.place_id == "ChIJtargetstonestown"
    assert place.source == "google:places/searchText"


def test_the_right_brand_at_the_wrong_door_is_refused() -> None:
    """A mall has several Targets within a search radius; only one is this store."""
    payload = {
        "places": [
            {
                "id": "ChIJtargetother",
                "displayName": {"text": "Target"},
                "formattedAddress": "789 Mission St, San Francisco, CA 94103, USA",
            }
        ]
    }

    assert pick_place(payload, TARGET_QUERY) is None


def test_an_empty_or_shapeless_answer_resolves_nothing() -> None:
    for payload in ({}, {"places": []}, {"places": [{}]}, [], None, "no"):
        assert pick_place(payload, TARGET_QUERY) is None


def test_a_candidate_has_to_be_the_retailer_not_merely_contain_its_name() -> None:
    """The permissive reading is wrong by exactly the case that matters: a supermarket shares
    its address with the businesses inside it, so "Target" appears in "Target Optical" and in
    "CVS pharmacy at Target", and both stand at 285 Winston Dr."""
    assert brand_matches("Target", "Target", "San Francisco Stonestown")
    assert brand_matches("Target", "Target Grocery", "San Francisco Stonestown")
    assert brand_matches("Whole Foods Market", "Whole Foods", "Whole Foods SoMa")
    assert brand_matches("Safeway", "Safeway", "Safeway San Francisco")

    assert not brand_matches("Target", "Target Optical", "San Francisco Stonestown")
    assert not brand_matches("Target", "CVS pharmacy at Target", "San Francisco Stonestown")
    assert not brand_matches("Whole Foods Market", "Foods Co", "Whole Foods SoMa")
    assert not brand_matches("Lucky Supermarkets", "Lucky Dragon Restaurant", "Lucky 7705")
    assert not brand_matches("Target", "Trader Joe's", "San Francisco Stonestown")
    assert not brand_matches("Target", "", "San Francisco Stonestown")


def test_a_branch_qualifier_in_the_stores_own_name_is_not_an_extra_word() -> None:
    """Google may name the listing "Target Stonestown" where StoreSplit's branch is "San
    Francisco Stonestown". That is the same shop, said two ways."""
    assert brand_matches("Target", "Target Stonestown", "San Francisco Stonestown")
    assert not brand_matches("Target", "Target Stonestown", "Emeryville")


def test_address_matching_compares_the_doorway_not_the_formatting() -> None:
    assert address_matches("285 Winston Dr", "285 Winston Drive, San Francisco, CA 94132, USA")
    assert address_matches("145 Jackson St", "145 Jackson Street, San Francisco, CA")
    assert not address_matches("285 Winston Dr", "825 Winston Dr, San Francisco, CA")
    assert not address_matches("285 Winston Dr", "285 Market St, San Francisco, CA")
    assert not address_matches("285 Winston Dr", "Stonestown Galleria, San Francisco, CA")
    assert not address_matches(None, "285 Winston Dr")


# ------------------------------------------------------------------------ through the API


def test_store_out_carries_the_place_link_and_todays_hours() -> None:
    store = _store(
        latitude=37.781321,
        longitude=-122.39964,
        address_line1="399 4th St",
        city="San Francisco",
        state="CA",
        zip_code="94107",
        timezone="America/Los_Angeles",
        hours={"weekly": {"3": {"opens": "08:00", "closes": "22:00"}}, "dates": {}},
    )
    store.id = 7

    out = store_out(store, now=datetime(2026, 9, 10, 19, 0, tzinfo=UTC))

    assert out.maps_url is not None and out.maps_url.startswith("https://www.google.com/maps/")
    assert "Whole+Foods" in out.maps_url, "the link names the retailer, not just the branch"
    assert out.latitude == 37.781321 and out.timezone == "America/Los_Angeles"
    assert out.hours_today.state == "open" and out.hours_today.closes_at == "22:00"


def test_store_out_carries_a_published_closure_as_its_own_state() -> None:
    """A day the retailer published as shut is a different sentence from a shop that has
    closed for the night, and the client cannot tell them apart without this flag."""
    store = _store(
        timezone="America/Los_Angeles",
        hours={
            "weekly": {
                "3": {"opens": None, "closes": None},
                "4": {"opens": "08:00", "closes": "22:00"},
            },
            "dates": {},
        },
    )
    store.id = 8

    out = store_out(store, now=datetime(2026, 9, 10, 19, 0, tzinfo=UTC))  # a Thursday

    assert out.hours_today.state == "closed"
    assert out.hours_today.closed_all_day is True
    assert out.hours_today.opens_day == "tomorrow"


def test_store_out_reports_unknown_hours_for_a_retailer_that_publishes_none() -> None:
    store = _store(
        Retailer(slug="raleys", name="Raley's"),
        name="Raley's Alameda",
        address_line1="2531 Blanding Ave",
        city="Alameda",
        state="CA",
        zip_code="94501",
    )
    store.id = 9

    out = store_out(store, now=datetime(2026, 9, 10, 19, 0, tzinfo=UTC))

    assert out.hours_today.state == "unknown"
    assert out.hours_today.opens_at is None and out.hours_today.closes_at is None
    assert out.maps_url is not None, "no hours is not no address"


def test_the_radius_search_uses_is_the_one_the_scrape_ranked_with() -> None:
    """Two knobs for one decision would let search and scrape disagree about a store."""
    from app.config import get_settings
    from app.retailers import zipmatch

    assert zipmatch.default_radius_miles() == get_settings().search_store_radius_miles


# ------------------------------------------------- resolving through a scrape, end to end


async def test_a_retailers_own_place_beats_one_resolved_on_its_behalf(db) -> None:
    """The business naming its own listing outranks anything a search concludes about it."""
    from app.normalize.categories import CATEGORIES
    from app.retailers.base import StoreDetails, StoreLocation
    from app.services.maps import MapsPlace, PlaceQuery
    from app.services.scraper import fetch_retailer, ingest_retailer
    from sqlalchemy import select

    from tests.fakes import FakeAdapter

    location = StoreLocation(
        "3264", "San Francisco Stonestown", address_line1="285 Winston Dr", zip_code="94132"
    )
    published = StoreDetails(
        external_id="3264",
        maps_place_url="https://maps.google.com/maps?cid=10195751074682041949",
        source="target:sl-page/store",
    )
    adapter = FakeAdapter("t", [location], {"eggs": {"3264": []}}, store_details=published)
    asked: list[PlaceQuery] = []

    async def resolver(query: PlaceQuery) -> MapsPlace | None:
        asked.append(query)
        return MapsPlace("https://www.google.com/maps/search/?api=1&query=x", "x", "google")

    fetch = await fetch_retailer(
        adapter,
        "94132",
        [CATEGORIES["eggs"]],
        max_stores=2,
        request_limit=4,
        place_resolver=resolver,
    )
    await ingest_retailer(db, fetch, "94132")
    await db.commit()

    store = await db.scalar(select(Store).where(Store.external_id == "3264"))
    assert store is not None
    assert store.maps_place_url == "https://maps.google.com/maps?cid=10195751074682041949"
    assert store.maps_source == "target:sl-page/store"
    assert not asked, "a store the retailer already identified is never looked up"


async def test_a_store_the_retailer_names_no_place_for_is_resolved_and_then_left_alone(
    db,
) -> None:
    """The lookup is billed, so it happens once a week per store -- not once a scrape. A
    resolution that *fails* has to be remembered too, or exactly the stores that cost money
    are the ones asked about again on every run, for ever."""
    from app.normalize.categories import CATEGORIES
    from app.retailers.base import StoreLocation
    from app.services.maps import MapsPlace, PlaceQuery
    from app.services.scraper import fetch_retailer, fresh_store_details, ingest_retailer
    from sqlalchemy import select

    from tests.fakes import FakeAdapter

    location = StoreLocation(
        "S1", "Somewhere", address_line1="1 Market St", city="San Francisco", zip_code="94105"
    )
    adapter = FakeAdapter("plain", [location], {"eggs": {"S1": []}})
    assert not hasattr(adapter, "fetch_store_details"), "no published details at all"
    calls: list[PlaceQuery] = []

    async def resolver(query: PlaceQuery) -> MapsPlace | None:
        calls.append(query)
        return MapsPlace(
            "https://www.google.com/maps/search/?api=1&query=ChIJabcdefghij"
            "&query_place_id=ChIJabcdefghij",
            "ChIJabcdefghij",
            "google:places/searchText",
        )

    async def scrape() -> None:
        fresh = await fresh_store_details(db, "plain", 7 * 24 * 3600)
        fetch = await fetch_retailer(
            adapter,
            "94105",
            [CATEGORIES["eggs"]],
            max_stores=2,
            request_limit=4,
            fresh_details=fresh,
            place_resolver=resolver,
        )
        await ingest_retailer(db, fetch, "94105")
        await db.commit()

    await scrape()
    store = await db.scalar(select(Store).where(Store.external_id == "S1"))
    assert store is not None and store.maps_place_id == "ChIJabcdefghij"
    assert store.maps_source == "google:places/searchText"
    assert store.maps_updated_at is not None
    assert len(calls) == 1
    assert calls[0].retailer_name == "Plain" and calls[0].address_line1 == "1 Market St"

    await scrape()
    assert len(calls) == 1, "a week's cache, not a billed lookup per scrape"


async def test_a_place_that_cannot_be_resolved_is_still_only_asked_about_once_a_week(
    db,
) -> None:
    from app.normalize.categories import CATEGORIES
    from app.retailers.base import StoreLocation
    from app.services.maps import MapsPlace, PlaceQuery
    from app.services.scraper import fetch_retailer, fresh_store_details, ingest_retailer
    from sqlalchemy import select

    from tests.fakes import FakeAdapter

    location = StoreLocation(
        "S2", "Somewhere", address_line1="1 Market St", city="San Francisco", zip_code="94105"
    )
    adapter = FakeAdapter("plain", [location], {"eggs": {"S2": []}})
    calls: list[PlaceQuery] = []

    async def refuses(query: PlaceQuery) -> MapsPlace | None:
        calls.append(query)
        return None  # nothing passed the brand and address check

    async def scrape() -> None:
        fresh = await fresh_store_details(db, "plain", 7 * 24 * 3600)
        fetch = await fetch_retailer(
            adapter,
            "94105",
            [CATEGORIES["eggs"]],
            max_stores=2,
            request_limit=4,
            fresh_details=fresh,
            place_resolver=refuses,
        )
        await ingest_retailer(db, fetch, "94105")
        await db.commit()

    await scrape()
    await scrape()

    store = await db.scalar(select(Store).where(Store.external_id == "S2"))
    assert store is not None
    assert store.maps_place_url is None, "nothing verified, so nothing stored"
    assert len(calls) == 1, "the failure is remembered; the API is not asked again this week"
