"""Turning a browser's coordinates into the ZIP the rest of the API speaks.

Every other surface takes a ZIP: search, offers, baskets and refresh all scope to one, and
`retailers/zipmatch.py` ranks stores by distance from that ZIP's Census ZCTA centroid. A
shopper who lets the browser share their location has coordinates and no ZIP, so something
has to make the translation, and it has to make it *against that same table* -- a geocoding
service could name a ZIP whose centroid is not the nearest one to where they are standing,
and every store distance below it would then be measured from a point they are not at.

No third party is involved and nothing leaves the machine: the table is vendored, public
domain, and read from disk. The coordinates are used for one lookup and never stored.
"""

from fastapi import APIRouter, HTTPException, Query

from app.retailers.zipmatch import MAX_REVERSE_MILES, nearest_zip
from app.schemas import ZipLookupOut

router = APIRouter(prefix="/location", tags=["location"])


@router.get("/zip", response_model=ZipLookupOut)
def zip_for_coordinates(
    latitude: float = Query(ge=-90, le=90, description="Decimal degrees, WGS84."),
    longitude: float = Query(ge=-180, le=180, description="Decimal degrees, WGS84."),
) -> ZipLookupOut:
    """The US ZIP code whose Census centroid is nearest to a point.

    A 404 is the honest answer for a point with no ZIP within `MAX_REVERSE_MILES` -- the
    Atlantic, or another country. Returning the closest one anyway would put a shopper in
    Vancouver in Blaine, Washington and then rank stores for them as if they could shop there.

    Synchronous on purpose: it reads an in-process dict and does no I/O, so it is CPU work in
    the terms `CLAUDE.md` sets, and FastAPI runs a sync endpoint in the threadpool rather than
    on the event loop.
    """
    found = nearest_zip(latitude, longitude)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No US ZIP code within {MAX_REVERSE_MILES:.0f} miles of that location. "
                "Enter a ZIP code instead."
            ),
        )
    zip_code, miles = found
    return ZipLookupOut(
        zip_code=zip_code,
        latitude=latitude,
        longitude=longitude,
        distance_miles=round(miles, 2),
    )
