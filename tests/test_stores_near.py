"""Which stores a ZIP means, at search time.

The scrape ranks a retailer's directory by distance from the ZIP's centroid. Search has to
reach the same answer from the stores already in the database, or the ZIP a shopper chose
stops being the source of truth for what they are shown.
"""

from app.db.models import Retailer, Store
from app.services.stores import stores_near
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Real coordinates, so the distances under test are the ones the application computes.
WHOLE_FOODS = {
    "10151": ("Whole Foods SoMa", "94107", 37.781321, -122.39964),
    "10718": ("Whole Foods Trinity", "94103", 37.774929, -122.41014),
    "10432": ("Whole Foods Ocean", "94112", 37.72387, -122.454877),
    "10717": ("Whole Foods Stonestown", "94132", 37.728012, -122.475494),
}
RALEYS = {
    "632": ("Nob Hill Alameda", "94501", 37.769440, -122.235163),
    "415": ("Raley's Sacramento", "95822", 38.522860, -121.494400),
}


async def _seed(db: AsyncSession) -> None:
    for slug, name, stores in (
        ("wholefoods", "Whole Foods Market", WHOLE_FOODS),
        ("raleys", "Raley's", RALEYS),
    ):
        retailer = Retailer(slug=slug, name=name)
        db.add(retailer)
        await db.flush()
        for external_id, (store_name, zip_code, lat, lng) in stores.items():
            db.add(
                Store(
                    retailer_id=retailer.id,
                    external_id=external_id,
                    name=store_name,
                    zip_code=zip_code,
                    latitude=lat,
                    longitude=lng,
                    served_zip_codes=[],
                )
            )
    await db.flush()


async def test_each_zip_gets_its_own_nearest_stores(db: AsyncSession) -> None:
    await _seed(db)

    downtown = [s.name for s in await stores_near(db, "94105")]
    daly_city = [s.name for s in await stores_near(db, "94014")]

    assert downtown[:2] == ["Whole Foods SoMa", "Whole Foods Trinity"]
    assert daly_city[:2] == ["Whole Foods Ocean", "Whole Foods Stonestown"]
    assert set(downtown).isdisjoint({"Whole Foods Ocean", "Whole Foods Stonestown"})


async def test_a_store_scraped_for_another_zip_does_not_leak_in_on_its_zip_prefix(
    db: AsyncSession,
) -> None:
    """The measured bug: Ocean (94112) and Stonestown (94132) were resolved and scraped only
    for 94014, and reached 94105 searches because their ZIPs share the prefix `941`."""
    await _seed(db)

    names = [s.name for s in await stores_near(db, "94105")]

    assert "Whole Foods Ocean" not in names
    assert "Whole Foods Stonestown" not in names


async def test_a_store_beyond_the_radius_is_excluded_even_though_a_scrape_discovered_it(
    db: AsyncSession,
) -> None:
    await _seed(db)
    sacramento = await db.scalar(select(Store).where(Store.external_id == "415"))
    assert sacramento is not None
    sacramento.served_zip_codes = ["94105"]
    await db.flush()

    assert "Raley's Sacramento" not in [s.name for s in await stores_near(db, "94105")]


async def test_a_store_without_coordinates_is_ranked_by_its_own_zip(db: AsyncSession) -> None:
    """Safeway, Sprouts and Lucky publish none; excluding them would delete those retailers."""
    retailer = Retailer(slug="safeway", name="Safeway")
    db.add(retailer)
    await db.flush()
    db.add(
        Store(
            retailer_id=retailer.id,
            external_id="1234",
            name="Safeway Market St",
            zip_code="94103",
            served_zip_codes=[],
        )
    )
    db.add(
        Store(
            retailer_id=retailer.id,
            external_id="5678",
            name="Safeway Los Angeles",
            zip_code="90012",
            served_zip_codes=[],
        )
    )
    await db.flush()

    names = [s.name for s in await stores_near(db, "94105")]

    assert names == ["Safeway Market St"]


async def test_a_zip_with_no_centroid_falls_back_to_the_prefix_rule(db: AsyncSession) -> None:
    await _seed(db)

    assert await stores_near(db, "M5R 3B4") == []  # non-numeric input never raises
    assert await stores_near(db, "00000") == []  # no centroid, no store shares the prefix


async def test_the_fallback_for_a_centroid_less_zip_still_caps_each_retailer(
    db: AsyncSession,
) -> None:
    """A ZIP with no centroid cannot be measured from, so the prefix heuristic is all there
    is. It still may not hand back a retailer's whole estate: the cap is what keeps the
    answer to "which stores" the size of an answer."""
    retailer = Retailer(slug="wholefoods", name="Whole Foods Market")
    db.add(retailer)
    await db.flush()
    for index in range(5):
        db.add(
            Store(
                retailer_id=retailer.id,
                external_id=f"X{index}",
                name=f"Store {index}",
                zip_code="00501",  # a real ZIP with no ZCTA centroid in the vendored file
                served_zip_codes=["00501"],
            )
        )
    await db.flush()

    near = await stores_near(db, "00501")

    assert 0 < len(near) <= 2, [s.name for s in near]
