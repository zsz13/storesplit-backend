"""Pointing a shopper at a supermarket, rather than at the ground it stands on.

A map link has one job: open *this* store. The rule is a ladder, and every rung is honest
about what it knows.

1. **A place the retailer itself published.** Target's store page carries
   `miscellaneous.google_cid`, a `https://maps.google.com/maps?cid=...` link to its own
   Google listing. Nobody has to identify the business, because the business identified
   itself. This is the only rung that yields a *verified* place with no third party.
2. **A place resolved and checked against the store.** With `GOOGLE_MAPS_API_KEY` set, the
   Places API is asked for the business at this exact address, and the answer is accepted
   only if the name carries the retailer's brand and the street number and street match what
   the retailer published. An unverified answer is discarded, not stored.
3. **An address search that names the retailer.** No place id, so no promise of a specific
   listing -- but the query says `Target San Francisco Stonestown, 285 Winston Dr, ...`, and
   a brand plus a street address is what makes Maps land on the shop instead of the doorway.
   This is where the old behaviour went wrong: it queried `store.name` alone, which for
   Target is "San Francisco Stonestown" -- a phrase with no supermarket in it -- so Maps did
   the only thing it could and resolved the street address.
4. **A coordinate pin**, when the retailer published coordinates but no street. A pin is a
   place on Earth rather than a business, so it is last.
5. **Nothing.** A store known only by its ZIP gets no link. `zipmatch` places such a store at
   its ZIP's centroid so it can be *ranked*; a centroid is a neighbourhood, not a door, and
   `StorePoint.precision` is what keeps it out of here.

Rungs 1 and 2 are resolved during the scrape's store-details phase and cached on the row;
rungs 3-5 are pure functions of columns already loaded. Nothing on this path performs I/O
while a search or a product page is being rendered.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx

from app.db.models import Store
from app.normalize.hours import DayHours, UnzonedHours
from app.retailers.http import request_with_retry

log = logging.getLogger("storesplit.services.maps")

MAPS_SEARCH_URL = "https://www.google.com/maps/search/"
# Hosts a stored place link may live on. Deliberately short: these are the ones Google serves
# maps from, and a link is a destination whoever built it. A URL shortener is **not** here --
# `maps.app.goo.gl` redirects anywhere Google is told to, which is the whole of its job, so a
# Google-looking host would have vouched for a destination nobody checked.
_MAPS_HOSTS = frozenset({"maps.google.com", "www.google.com", "google.com"})
# A host alone does not make a link a map: `maps.google.com` still serves things that are
# not maps, and `https://maps.google.com/local/...?url=` wearing a "Maps" label is an
# off-site destination nobody checked. The path has to say so. One exception, because it is
# the classic form: `maps.google.com` serves a map from its root, where the query is the
# whole link (`/?q=`, `/maps?cid=`).
_MAPS_PATH_RE = re.compile(r"^/maps(/|$)")
_MAPS_ROOT_HOST = "maps.google.com"
# Google issues place ids as a long opaque token, conventionally `ChIJ...`/`Ei...`/`Gh...`.
# A bare run of digits is a CID, which is a different identifier: passing one as
# `query_place_id` produces a link that resolves to nothing while looking authoritative.
_PLACE_ID_RE = re.compile(r"^(?!\d+$)[A-Za-z0-9_\-]{10,128}$")
_CID_RE = re.compile(r"[?&]cid=(\d{1,32})\b")

# Words that carry no identity, so they are ignored when checking a resolved name against
# the retailer. "Target Grocery" is Target; "Whole Foods Market" is Whole Foods.
_NOISE = re.compile(
    r"\b(?:the|market|markets|supermarket|supermarkets|grocery|store|stores|"
    r"co|company|inc|llc|and|of|supercenter|superstore|neighborhood)\b"
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class MapsPlace:
    """A specific business on Google Maps, with where the claim came from."""

    url: str
    place_id: str | None
    source: str


# ----------------------------------------------------------------- reading what a retailer gives


def place_from_retailer(raw: str | None, *, source: str) -> MapsPlace | None:
    """A Maps link or place id a retailer published, as a place -- or None.

    Target hands over a whole URL (`https://maps.google.com/maps?cid=1019...`); another
    retailer might hand over a bare `place_id`. Both arrive here, and anything that is not
    recognisably one of them is refused rather than passed through: an unvalidated string in
    an `href` is how a value that is not a URL becomes a link that looks like one.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if "://" in text:
        return _place_from_url(text, source)
    if text.isdigit():
        # A bare CID. It identifies a place, but only through the `?cid=` form.
        return _place_from_url(f"https://maps.google.com/maps?cid={text}", source)
    if _PLACE_ID_RE.fullmatch(text):
        return MapsPlace(place_url_for_id(text), text, source)
    return None


def _place_from_url(raw: str, source: str) -> MapsPlace | None:
    if "\\" in raw:
        return None  # browsers and URL parsers disagree about where the host ends
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or "@" in (parts.netloc or ""):
        return None
    host = (parts.hostname or "").lower()
    if host not in _MAPS_HOSTS:
        return None
    # Always https: the stored value becomes an `href`, and the retailer's own http link
    # would be an unnecessary downgrade for a link nobody has to follow in plaintext.
    url = parts._replace(scheme="https").geturl()
    path = parts.path or "/"
    if not _MAPS_PATH_RE.match(path) and not (host == _MAPS_ROOT_HOST and path == "/"):
        return None
    cid = _CID_RE.search(url)
    return MapsPlace(url, cid.group(1) if cid else None, source)


def place_url_for_id(place_id: str) -> str:
    """Google's documented URL for one place by id (Maps URLs API, `search` + place id)."""
    return (
        f"{MAPS_SEARCH_URL}?{urlencode({'api': 1, 'query': place_id, 'query_place_id': place_id})}"
    )


# --------------------------------------------------------------------- checking a resolved place


def brand_matches(retailer_name: str, resolved_name: str, store_name: str = "") -> bool:
    """True when a resolved listing **is** this retailer's shop, not something sharing its door.

    The obvious rule -- "the retailer's name appears in the candidate's" -- is too generous
    by exactly the case that matters. A supermarket's address is shared with the businesses
    inside it, so `Target` appears in "Target Optical" and in "CVS pharmacy at Target", and
    both stand at 285 Winston Dr. The reverse direction is no better: it lets "Foods Co"
    satisfy "Whole Foods Market".

    So the names have to *be* the same, once two things are set aside: words that carry no
    identity ("Market", "Grocery"), and words the store's own name already contains, which is
    how a branch qualifier survives ("Target Stonestown" against the Stonestown store). A
    candidate with any other extra word is a different business, and refusing it costs only
    the fallback to an address search -- a worse link, and never a wrong one.
    """
    wanted = _identity_words(retailer_name)
    # Only the *branch* part of the store's name is set aside. Subtracting the whole of
    # it would delete the retailer from itself: "Safeway San Francisco" contains
    # "Safeway", and a candidate named "Safeway" would then match nothing at all.
    branch = _identity_words(store_name) - wanted
    found = _identity_words(resolved_name) - branch
    return bool(wanted) and found == wanted


def address_matches(published: str | None, resolved: str | None) -> bool:
    """True when two street addresses name the same doorway.

    Compares the house number and the significant words of the street, so "285 Winston Dr"
    matches "285 Winston Drive, San Francisco, CA 94132" and does not match "825 Winston Dr".
    A resolved address with no house number never matches: that is a neighbourhood result.
    """
    want_number, want_words = _street_parts(published)
    got_number, got_words = _street_parts(resolved)
    if not want_number or not got_number or want_number != got_number:
        return False
    return bool(want_words) and bool(got_words) and (want_words <= got_words)


_STREET_TYPES = re.compile(
    r"\b(?:st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|ct|court|pl|place|"
    r"pkwy|parkway|hwy|highway|ter|terrace|cir|circle|sq|square|n|s|e|w|ne|nw|se|sw)\b"
)


def _street_parts(address: str | None) -> tuple[str, frozenset[str]]:
    text = (address or "").strip().lower()
    if not text:
        return "", frozenset()
    number = re.match(r"(\d+)", text)
    words = _NON_WORD.sub(" ", text)
    words = _STREET_TYPES.sub(" ", words)
    significant = {w for w in words.split() if w and not w.isdigit() and len(w) > 1}
    return (number.group(1) if number else ""), frozenset(significant)


def _identity_words(name: str) -> frozenset[str]:
    text = _NON_WORD.sub(" ", (name or "").lower())
    text = _NOISE.sub(" ", text)
    return frozenset(w for w in text.split() if len(w) > 1)


# ----------------------------------------------------------------------- the link a store gets


def maps_url(store: Store) -> str | None:
    """The Google Maps link for this store, best available rung of the ladder above."""
    if store.maps_place_url:
        place = place_from_retailer(store.maps_place_url, source=store.maps_source or "stored")
        if place is not None:
            return place.url
        # A stored value that no longer passes the rules is a bug somewhere upstream, and it
        # is louder as a log line plus a working search link than as a broken destination.
        log.warning(
            "stored_maps_place_rejected",
            extra={"store": store.id, "source": store.maps_source},
        )
    query = search_query(store)
    if query:
        return f"{MAPS_SEARCH_URL}?{urlencode({'api': 1, 'query': query})}"
    if store.latitude is not None and store.longitude is not None:
        pin = f"{store.latitude},{store.longitude}"
        return f"{MAPS_SEARCH_URL}?{urlencode({'api': 1, 'query': pin})}"
    return None


def search_query(store: Store) -> str | None:
    """The text a Maps search should carry: the retailer, the branch, then the street address.

    None when there is no street address. A city and a ZIP are where a store *is*, not what
    it is, and searching for "Lucky, San Francisco, CA" picks a Lucky -- possibly not this
    one. The retailer's name leads because it is the thing being looked for; without it Maps
    is handed a branch label like "San Francisco Stonestown" and resolves the street instead.
    """
    if not store.address_line1:
        return None
    retailer_name = store.retailer.name if store.retailer is not None else ""
    lead = _business_name(retailer_name, store.name)
    locality = " ".join(part for part in (store.state, store.zip_code) if part)
    parts = [lead, store.address_line1, store.city, locality]
    return ", ".join(part for part in parts if part) or None


def _business_name(retailer_name: str, store_name: str) -> str:
    """The shop's name, with the retailer in front of it exactly when it is missing.

    Most adapters already prefix a branch with its retailer ("Whole Foods SoMa", "Smart &
    Final Daly City"), and gluing the retailer on again produced "Whole Foods Market Whole
    Foods SoMa" -- not wrong enough to break a search, and not something to hand a shopper
    or a place resolver either. Target is the case that needs the prefix: its branch label is
    "San Francisco Stonestown", which names a neighbourhood and a mall and no supermarket.
    """
    if not store_name:
        return retailer_name
    if not retailer_name or _identity_words(retailer_name) <= _identity_words(store_name):
        return store_name
    return f"{retailer_name} {store_name}"


# --------------------------------------------------------- resolving a place, when allowed

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Only the fields the check needs. A narrower mask is cheaper and, more to the point, makes
# it plain in the request itself that nothing else about the place is being collected.
PLACES_FIELD_MASK = "places.id,places.displayName,places.formattedAddress"
# How far from the store's own coordinates a candidate may sit, in metres. A supermarket is a
# big building with several entrances; a few hundred metres is the same site, and a kilometre
# is the next block.
PLACES_BIAS_RADIUS_M = 400.0
PLACES_SOURCE = "google:places/searchText"


@dataclass(frozen=True)
class PlaceQuery:
    """What is known about a store, as the question to ask about it."""

    retailer_name: str
    store_name: str
    address_line1: str
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    def text(self) -> str:
        locality = " ".join(p for p in (self.state, self.zip_code) if p)
        parts = [
            _business_name(self.retailer_name, self.store_name),
            self.address_line1,
            self.city,
            locality,
        ]
        return ", ".join(p for p in parts if p)


async def resolve_place(
    client: httpx.AsyncClient, query: PlaceQuery, api_key: str
) -> MapsPlace | None:
    """Ask Google for the business at this address, and accept it only if it *is* that shop.

    Off unless `GOOGLE_MAPS_API_KEY` is set, and never on a request path: the scrape's weekly
    store-details pass calls it for stores whose retailer publishes no place of its own, and
    the answer is cached on the row.

    The check is the point. A text search near a coordinate will always return *something*,
    and the something at 285 Winston Dr is a mall with a Target, a Sports Basement, a nail
    salon and a Starbucks in it. So a candidate is taken only when its name carries the
    retailer's brand **and** its address names the same house number and street the retailer
    published. Nothing that fails both is stored, and a store with no accepted candidate
    keeps its NULLs and gets the address search -- a worse link, and never a wrong one.
    """
    if not api_key or not query.address_line1:
        return None
    body: dict[str, object] = {"textQuery": query.text(), "maxResultCount": 5}
    if query.latitude is not None and query.longitude is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": query.latitude, "longitude": query.longitude},
                "radius": PLACES_BIAS_RADIUS_M,
            }
        }
    response = await request_with_retry(
        client,
        "POST",
        PLACES_SEARCH_URL,
        json=body,
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": PLACES_FIELD_MASK},
        max_retries=1,
    )
    if response.status_code != 200:
        log.warning("places_search_failed", extra={"status": response.status_code})
        return None
    return pick_place(response.json(), query)


PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/"
# Two masks, because they are two prices. `timeZone` is a Place Details Pro field and
# `regularOpeningHours` an Enterprise one, so a store that published its own week and only
# needs a zone -- Trader Joe's, every one of them -- is never billed for hours it already has.
PLACE_ZONE_MASK = "timeZone"
PLACE_HOURS_MASK = "regularOpeningHours,timeZone"
PLACE_HOURS_SOURCE = "google:places/details"
# Google numbers weekdays from Sunday; `date.weekday()` numbers them from Monday.
_GOOGLE_DAY_OFFSET = 6
# Midnight to midnight: a day with no shut moment in it. `hours_today` reads a close at or
# before its open as running past midnight, so this is how "open 24 hours" is spelled here
# and in `savemartco/storefront.py`, which maps the banners' own `OPEN_24_HOURS` to it.
ALWAYS_OPEN = DayHours("00:00", "00:00")


@dataclass(frozen=True)
class PlaceSchedule:
    """What Google states about a place's week, and the zone that week is kept in.

    `hours` is deliberately `UnzonedHours` rather than `StoreHours`: it is the same kind of
    thing Trader Joe's locator produces, and pairing a clock with a zone is one decision that
    belongs in one place (`hours_from_unzoned`) rather than two.
    """

    timezone: str | None
    hours: UnzonedHours | None


async def fetch_place_schedule(
    client: httpx.AsyncClient, place_id: str, api_key: str, *, want_hours: bool
) -> PlaceSchedule | None:
    """The opening hours and timezone of a place that has already been verified as this store.

    Only ever called for a place on rung 1 or 2 of the ladder above -- one the retailer
    itself published, or one the Places search returned *and* `pick_place` accepted on brand
    and street address. A place found by a bare address search never reaches here, because
    the hours of the business next door are worse than no hours at all.

    Off unless `GOOGLE_MAPS_API_KEY` is set, and never on a request path: the weekly
    store-details pass calls it and the answer is cached on the row, inside the 30-day limit
    Google's terms place on caching this content.
    """
    if not api_key or not _PLACE_ID_RE.fullmatch(place_id):
        return None
    response = await request_with_retry(
        client,
        "GET",
        f"{PLACE_DETAILS_URL}{place_id}",
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": PLACE_HOURS_MASK if want_hours else PLACE_ZONE_MASK,
        },
        max_retries=1,
    )
    if response.status_code != 200:
        log.warning(
            "place_details_failed",
            extra={"status": response.status_code, "place": place_id},
        )
        return None
    return schedule_from_place(response.json())


def schedule_from_place(payload: object) -> PlaceSchedule | None:
    """A Places `Place` record as a zone and a week, or None when it states neither."""
    if not isinstance(payload, dict):
        return None
    zone = payload.get("timeZone")
    timezone = str((zone or {}).get("id") or "").strip() or None if isinstance(zone, dict) else None
    hours = _week_from_periods(((payload.get("regularOpeningHours") or {}) or {}).get("periods"))
    if timezone is None and hours is None:
        return None
    return PlaceSchedule(timezone=timezone, hours=hours)


def _week_from_periods(periods: object) -> UnzonedHours | None:
    """`regularOpeningHours.periods` as one window per weekday.

    Three shapes have to be told apart, and only one of them is ordinary:

    * A period with an `open` and a `close` is a window. Its day is the day it *opens* on,
      so a close the next morning stays attached to the evening it belongs to -- which is
      also how `hours_today` reads a close at or before its open.
    * **One** period with an `open` at Sunday 00:00 and no `close` is how Google writes a
      place that never shuts *at all*. It is a statement about the whole week, not about
      Sunday, so it becomes all seven days -- the same value the Save Mart banners'
      `OPEN_24_HOURS` maps to, applied the same way they apply it. Reading it as one day is
      how a 24-hour shop came out as "Closed - Opens 12:00 AM Sunday" for six days a week.
    * Any *other* period with no `close` is that one day running to midnight and on, and is
      kept against its own day.
    * A day carrying **more than one** period is split hours -- a lunchtime closure -- and
      `DayHours` holds one window, so the day is dropped and logged rather than flattened.
      Merging them would claim the shop is open across the gap between them, which is the
      one error worth less than admitting the day is unknown.
    """
    if not isinstance(periods, list):
        return None
    if _is_always_open(periods):
        return UnzonedHours(weekly=dict.fromkeys(range(7), ALWAYS_OPEN))
    weekly: dict[int, DayHours] = {}
    seen: set[int] = set()
    for period in periods:
        if not isinstance(period, dict):
            continue
        opens_at, weekday = _point_from_period(period.get("open"))
        if opens_at is None or weekday is None:
            continue
        if weekday in seen:
            log.info("places_split_hours", extra={"weekday": weekday})
            weekly.pop(weekday, None)
            continue
        seen.add(weekday)
        closes_at, _ = _point_from_period(period.get("close"))
        # No close at all is Google's "always open"; a close that will not parse is a day
        # nobody can state, and an unstated day is left unknown.
        if period.get("close") is None:
            weekly[weekday] = ALWAYS_OPEN
        elif closes_at is not None:
            weekly[weekday] = DayHours(opens_at, closes_at)
    return UnzonedHours(weekly=weekly) if weekly else None


def _is_always_open(periods: list[object]) -> bool:
    """Google's shape for a place that never closes: one period, Sunday 00:00, no `close`.

    Documented as "if the place is always open, the open period contains day 0, hour 0 and
    minute 0, and no close". It describes the week, so it must not be filed under Sunday.
    """
    if len(periods) != 1 or not isinstance(periods[0], dict):
        return False
    period = periods[0]
    opens = period.get("open")
    if period.get("close") is not None or not isinstance(opens, dict):
        return False
    return (opens.get("day"), opens.get("hour"), opens.get("minute")) == (0, 0, 0)


def _point_from_period(point: object) -> tuple[str | None, int | None]:
    """One `{day, hour, minute}` as a wall clock and a `date.weekday()` index."""
    if not isinstance(point, dict):
        return None, None
    hour, minute, day = point.get("hour"), point.get("minute"), point.get("day")
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None, None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, None
    weekday = (day + _GOOGLE_DAY_OFFSET) % 7 if isinstance(day, int) and 0 <= day <= 6 else None
    return f"{hour:02d}:{minute:02d}", weekday


def pick_place(payload: object, query: PlaceQuery) -> MapsPlace | None:
    """The one candidate that is recognisably this retailer at this address, or None.

    **One.** A supermarket shares its address with the businesses inside it: the Target at
    285 Winston Dr sits in a mall alongside a Target Optical, and both carry the brand and
    the street. Taking the first of several would be guessing a store from a coordinate,
    which the requirement forbids by name. So candidates that pass brand and address are
    gathered, a name that *is* the retailer beats one that merely contains it, and anything
    still tied is refused -- the address search that follows is a worse link, never a wrong
    one.
    """
    places = (payload or {}).get("places") or [] if isinstance(payload, dict) else []
    exact: list[MapsPlace] = []
    contains: list[MapsPlace] = []
    wanted = _identity_words(query.retailer_name)
    for candidate in places:
        if not isinstance(candidate, dict):
            continue
        place_id = str(candidate.get("id") or "").strip()
        name = str((candidate.get("displayName") or {}).get("text") or "")
        address = str(candidate.get("formattedAddress") or "")
        if not place_id or not _PLACE_ID_RE.fullmatch(place_id):
            continue
        if not brand_matches(query.retailer_name, name, query.store_name):
            continue
        if not address_matches(query.address_line1, address):
            continue
        place = MapsPlace(place_url_for_id(place_id), place_id, PLACES_SOURCE)
        (exact if _identity_words(name) == wanted else contains).append(place)
    for tier in (exact, contains):
        if len(tier) == 1:
            return tier[0]
        if tier:
            log.info(
                "places_ambiguous",
                extra={"retailer": query.retailer_name, "address": query.address_line1},
            )
            return None
    return None
