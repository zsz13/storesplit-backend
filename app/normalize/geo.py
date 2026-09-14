"""Whether a pair of numbers names a real point on Earth.

One predicate, in `normalize` because it is shared vocabulary rather than retailer knowledge
and because of where its callers sit. `retailers/zipmatch.py` ranks stores with it,
`services/scraper.py` gates both coordinate writers with it, and `normalize/timezones.py`
needs it before it can look a zone up -- and `normalize` must not reach into `app.retailers`
to get it. Importing any `app.retailers` submodule executes that package's `__init__`, which
constructs every adapter and pulls in httpx: 354 modules where a `normalize` module otherwise
costs 44. `CLAUDE.md` already forbids that edge for `db/models.py`, on the grounds that a
broken adapter would break migrations, and says shared vocabulary belongs here instead.

`zipmatch` re-exports it, so nothing that already imported it from there had to change.
"""

import math


def is_on_earth(latitude: float | None, longitude: float | None) -> bool:
    """A pair that names a real point, so it can be stored, measured and pointed at.

    Every coordinate in this system came out of a retailer's payload, so the answer has to
    survive a missing value, a string, a NaN and an infinity. `services/stores.py` measures
    every store on every search, basket and freshness request, and `math.sin(inf)` raises --
    one unchecked row would 500 every ZIP until somebody repaired it by hand.
    """
    if latitude is None or longitude is None:
        return False
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
