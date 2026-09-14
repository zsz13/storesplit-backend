---
paths:
  - "app/retailers/zipmatch.py"
  - "app/services/stores.py"
  - "app/services/maps.py"
  - "app/normalize/hours.py"
  - "app/normalize/timezones.py"
  - "app/normalize/geo.py"
  - "app/api/location.py"
  - "tests/test_store*.py"
  - "tests/test_zip*.py"
  - "tests/test_open_now.py"
  - "tests/test_timezones.py"
---

# Which stores a ZIP means, and what is true at them

Store selection, coordinate/ZIP resolution, opening hours, timezones and map links. One
rule decides store selection for both the scrape and the search; root `CLAUDE.md` states
that invariant, and the detail lives here.

## One ZIP rule, from the scrape to the search

- **One ZIP rule, from the scrape to the search.** `retailers/zipmatch.py` is the only place
  that decides which stores a ZIP means. A store is placed by its retailer-published
  coordinates, or -- Safeway, Sprouts, Save Mart/Lucky and Walmart publish none, 11 of 26
  live stores -- by the ZCTA centroid of **its own ZIP**, which `StorePoint.precision` marks
  as `zip_centroid` so it is never used as a map pin. Stores are then ranked by great-circle
  distance from the searched ZIP's centroid, kept within `SEARCH_STORE_RADIUS_MILES` (30) and
  capped per retailer at `SCRAPE_STORES_PER_RETAILER` (2). Retailers with a ZIP-aware
  endpoint (Trader Joe's, Sprouts, 99 Ranch, Kroger) still use it to *discover* stores; the
  ranking above decides which of them a ZIP is shown.
  **`services/stores.py::stores_near` uses that same rule, and this is a fix, not a
  refinement.** It used to select by 3-digit ZIP prefix or a `served_zip_codes` hit, which is
  unrelated to how the scrape chose stores, so stores leaked between ZIPs: Whole Foods Ocean
  (94112) and Stonestown (94132) were resolved and scraped only for 94014 and were served to
  94105 searches because all three ZIPs start `941`, while 94105's own nearest two are SoMa
  and Trinity. Distance alone does not fix that -- Ocean is well inside 30 miles of downtown
  -- the per-retailer cap does. On the distance path `served_zip_codes` no longer readmits a
  store: having once discovered it says nothing about whether this ZIP's ranking would pick
  it. The prefix heuristic survives only for a ZIP with no centroid at all, logs when it is
  used, and there consults `served_zip_codes` (with no distance to rank by, a scrape having
  reached that exact ZIP is the best evidence left) under the same per-retailer cap.
  `zipmatch.default_radius_miles()` is the one knob: `rank_stores_by_zip` reads it too, so a
  changed radius cannot leave the scrape and the search disagreeing about which stores a ZIP
  means. **A coordinate that is not a point on Earth is treated as no coordinate**: NaN,
  infinity and out-of-range values fall back to the ZIP centroid, because `stores_near`
  measures every store on every search, basket and freshness request and `math.sin(inf)`
  raises -- one bad row would otherwise have 500ed every ZIP until somebody repaired it.

## A coordinate becomes a ZIP over the same table

- **A coordinate becomes a ZIP over the table that ranks stores, or it becomes nothing.**
  `GET /location/zip` exists because every other surface takes a ZIP and a browser has only
  latitude and longitude. `retailers/zipmatch.py::nearest_zip` is `zip_centroid` read
  backwards over the same vendored Census ZCTA file, so the ZIP a shopper is placed in is the
  one whose centroid `rank_within_radius` will then measure their stores from -- a geocoding
  service could name the ZIP they are legally standing in and still name a centroid further
  from them, and every distance below it would be measured from a point they are not at. It
  is therefore *nearest centroid*, not *containing ZCTA*: the Ferry Building stands in 94111
  and resolves to 94105, and that is the answer this system wants. No third party is called,
  nothing is stored, and the scan is filtered on latitude first -- exact at any longitude,
  where a longitude band would shrink towards the poles and drop the right answer. A point
  with no centroid within `MAX_REVERSE_MILES` is a 404: the Atlantic has no postcode, and
  handing back the closest one would put a shopper in Vancouver into Blaine, Washington and
  rank stores they cannot reach.

## Store details, hours, timezones and map links

- **A store's own details are read once a week, and only by an adapter that can.**
  `fetch_store_details` is an optional capability (`base.py::StoreDetailsFetcher`), found with
  `getattr` like `unconfigured_reason` rather than declared on the adapter Protocol: a method
  eight adapters would raise from is a worse contract than one they do not have. Whole Foods
  implements it from `/stores/<folder>`, whose `detail-page-state` island carries
  `operationalDailyHours` (absolute UTC windows, converted to the store's own wall clock),
  the address, coordinates, `timeZone` and a `storeCode` that proves which store the page is.
  The scrape fetches it only for stores whose cached copy is older than
  `STORE_DETAILS_TTL_SECONDS` (7 days) and writes it onto the `stores` row; searches never
  fetch it. **Raley's has no first-party hours**: it serves store details from the
  robots-disallowed `/api` and its store page carries only interface strings, so without a
  Google key its rows say "Hours not published" rather than showing a plausible invention.
  Target, Safeway and Smart & Final implement it too, each from the surface its retailer
  actually publishes on: Target's `/sl/<slug>/<id>` store page (robots-allowed, outside the
  PerimeterX challenge, so it costs the browser session nothing) carries fourteen dated days
  of local wall clock, `iso_time_zone_code` and its own `google_cid`; Safeway's `localPage`
  on `local.safeway.com` carries a Yext profile with `normalHours`/`holidayHours`, an IANA
  `timezone`, `googlePlaceId` and the coordinates its store resolver has never published;
  Smart & Final states `timeZone` and an `openingHours` sentence on every record of the
  `/api/stores` directory the scrape already downloads, so its hours cost no extra request.
  A `StoreLocation` carries `details_url` when its locator names the store's own page, so no
  slug is ever rebuilt by rule.
  **Sprouts' hours are on a different host from its prices.** The Instacart storefront
  states none, but `www.sprouts.com` -- Sprouts' own WordPress site, with its own permissive
  robots.txt -- serves `GET /wp-json/spr-wp-rest/v1/store/<store number>`: address,
  coordinates, phone, an IANA `timezone`, and one `open_time`/`close_time` pair that applies
  to every weekday. The store number is the `location_code` the storefront's own shop
  payload already publishes (and which `parse_shops` writes into the store name as
  "(Store #276)"), so nothing is guessed and no slug is rebuilt. A store number nobody
  serves answers **200 with every field null** rather than 404, so `store_details_from_record`
  checks for a `store_id`, not a status code. Sprouts publishes no dated holiday exceptions
  as data -- its store page hides the hours line with a script -- so none is claimed. This
  is also where a Sprouts store stops being placed by the centroid of its ZIP: the record
  carries real coordinates the storefront never did.
  **99 Ranch states its own week in the payload the ZIP lookup already returned.** Every
  record of `/be-api/store/web/nearby/stores` carries an IANA `timeZone` and two schedules,
  and only one of them is the shop: `offlineBusinessTimes` is the door, `onlineBusinessTimes`
  is the delivery window, and they differ per store (Richmond delivers until 22:00 all week
  and shuts its doors at 21:00 Monday to Thursday). Days come as inclusive ranges --
  `"Monday - Thursday"` -- so `normalize/hours.py::parse_day_range` expands them, wrap
  included; a range nobody can read loses its days rather than guessing them. Hours cost no
  request of their own: `find_stores` already made it, and the records are cached per run.
  **Trader Joe's publishes a whole week and no zone, and the zone is read off its own
  coordinates.** Its locator -- `hosted.where2getit.com/traderjoes/rest/locatorsearch`, the
  one data call its own `/home/store-search` page makes -- states `monday_open` ..
  `sunday_close` for every store, keyed by the store number the UI shows ("(78)"), and names
  a timezone on no surface: not the locator, not the store page, not the GraphQL API. A wall
  clock with no zone is not a fact about a store, so the week is still carried as
  `StoreDetails.unzoned_hours` (`normalize/hours.py::UnzonedHours`) and becomes a schedule
  only through `hours_from_unzoned`, which needs a zone and returns None without one -- the
  rule stays enforced by the type rather than by an author remembering it, and the adapter
  still states exactly what the retailer states.
  **The zone comes from `normalize/timezones.py`, not from a bill.** The same locator record
  carries the store's coordinates and a point lies in exactly one timezone, so `timezone_at`
  reads it from the timezone-boundary polygons offline (`tzfpy`: one self-contained wheel, no
  runtime dependencies of its own). The answer is then checked against
  `timezones.py::US_TIMEZONES`, the 29 zones the country keeps: StoreSplit is US-only in every
  other dimension, so a store resolving to `Asia/Shanghai` is a dropped minus sign on a
  longitude rather than a shop in China, and this is what keeps that error an hour wide
  instead of fifteen. It is a codomain check on the polygons' answer, **not** a resolver --
  the boundaries still decide. `normalize/geo.py` holds `is_on_earth` for it, because
  `normalize` importing `app.retailers` for that predicate pulled the whole adapter registry
  and httpx in behind it (354 modules against 44), which is the edge `db/models.py` is already
  forbidden. A hand-rolled state table was rejected because the
  boundaries that matter are exactly the ones it approximates -- Trader Joe's Schererville
  store is in Indiana and on Chicago time, and `America/Phoenix` keeps no daylight saving --
  and because the zone must be right or absent, never plausible. `Etc/GMT±N` is refused: it
  is what open water resolves to, so it means the coordinates do not name a place, and its
  fixed offset would read a US store an hour wrong all summer.
  In `_resolved_hours` the zone ladder for a retailer's *own* unzoned week is: a zone Google
  stated for a verified place, then the coordinates', then the row's. `hours_source` stays
  `traderjoes:locator` either way -- the clock is entirely Trader Joe's, and only the frame it
  is read in is borrowed. **The derived zone sits above the row's and not below it**, which
  looks backwards and is not: `apply_store_details` writes every resolved zone onto the row,
  so a derived zone becomes a *cached* derived zone next week, and a cache consulted before
  its own source can never be corrected -- one payload with a dropped minus sign on the
  longitude would strand a store in the wrong hemisphere for good. Deriving first makes the
  answer recomputed rather than remembered. The row keeps the rung below, where it still
  serves a store whose coordinates nobody publishes. **Google's own week (rung 3) is never
  read in a derived zone**: `timeZone` is requested in the same call as
  `regularOpeningHours`, so hours arriving without one are a malformed answer, not a gap, and
  this exists to stop first-party hours being discarded rather than to make third-party hours
  storable where they were not before. It also **removes a billed
  request**: `_store_schedules` no longer asks Places for a zone it can derive, decided from
  the payload via `_place_query` so the fetch phase still holds no `Store`. A store the
  locator places nowhere still says "Hours not published". Migration `a4e91b2c7d68` clears
  `hours_updated_at` for Trader Joe's, or the weekly gate would hide the fix for seven days
  on an existing database -- the same trap `d7b21f0c4e93` documents, reached by a different
  route (the capability did not change; its answer did).
  Its free-text `holidayhours` and `Temp Hours Note` are deliberately not parsed,
  because half a week read wrongly is worse than a week nobody claimed. Reading a 12-hour clock is normalization, not retailer knowledge, so
  `normalize/hours.py::parse_clock_12h` is shared by Sprouts and Smart & Final rather than
  copied into both. Smart & Final's sentence is the one prose source
  here: it is parsed strictly and a wording nobody can read yields nothing, because half a
  week shown as a whole one is worse than admitting the hours are unknown.
  **A map link opens a business, not a patch of ground** (`services/maps.py`). The ladder is:
  a place the retailer published (Target's `google_cid`, Safeway's `googlePlaceId`, validated
  and cached on `stores.maps_place_*` by the same weekly pass), then a place resolved through
  the Places API and *checked* -- brand in the name, house number and street in the address --
  when `GOOGLE_MAPS_API_KEY` is set, then an address search naming the retailer, then a
  coordinate pin, then nothing. Nothing on this path performs I/O while a search is answered.
  **Google fills an hours gap, and only for a place already verified as this store.**
  `services/maps.py::fetch_place_schedule` reads Place Details for a place on rung 1 or 2 --
  one the retailer published, or one `pick_place` accepted on brand *and* house number *and*
  street. A place found by a bare address search never reaches it: the hours of the business
  next door are worse than no hours. It is cost-aware, because the two fields are two SKUs:
  a store that published its own week and needs only a zone is asked for `timeZone` (Places
  Details Pro), a store with no week at all is asked for `regularOpeningHours,timeZone`
  (Enterprise), and a store whose retailer published a zoned week is never asked. **Nor is
  one whose own coordinates settle the zone offline** -- which is every Trader Joe's store
  its locator places, so that rung now costs nothing at all. The decision is still made from
  the *payload* and never from the row (the fetch phase holds no `Store`), which is what
  allowed the saving without threading the database through three signatures; the row's own
  zone is consulted later, in `_resolved_hours`, where it is already at hand.
  Google's `day` is Sunday-0 where `date.weekday()` is Monday-0; a period with an `open` and
  no `close` is "open 24 hours" and becomes `DayHours("00:00", "00:00")`, the same value the
  Save Mart banners' `OPEN_24_HOURS` maps to; a day carrying **two** periods is split hours,
  which `DayHours` cannot hold, so the day is dropped rather than flattened across the gap
  between them. Off unless `GOOGLE_MAPS_API_KEY` is set, exactly as `resolve_place` is, and
  cached on the row under the same 7-day TTL -- inside the 30-day limit Google's terms place
  on caching this content. **First-party always wins**: `scraper.py::_resolved_hours` is the
  ladder, and a retailer's own week is never overridden -- which is a fact about the **row**
  and not about one call. `_google_may_write` is that distinction: a store page that 500s
  once leaves a run with no published hours, and without it a single bad afternoon would
  replace Safeway's week and its dated holidays with Google's and restamp the source.
  Raley's is the whole of the case this exists for.
  The search query leads with the retailer because that is what the old link lacked: it
  queried `store.name` alone, which for Target is "San Francisco Stonestown" -- a phrase with
  no supermarket in it -- so Maps resolved 285 Winston Dr instead of the Target on it. A
  branch that already names its retailer is not prefixed twice. A store known only by its ZIP
  still gets no link at all: a centroid places a store for ranking and is not a door.
  **Hours are a garnish on a price, and the code says so.** The whole details phase is
  wrapped and runs under its own timeout, so a store page that hangs or an adapter that
  raises costs that retailer its hours and nothing else; every attempt is stamped, including
  the ones that found nothing, or a store whose page 404s would be re-read every five
  minutes forever. `STORE_DETAILS_TTL_SECONDS=0` switches the feature off rather than meaning
  "no cache". Retailer strings are bounded to their columns before they are written: an
  over-length value is rejected by PostgreSQL *inside the ingest transaction*, which would
  roll back that retailer's prices for the sake of a store name.
  **A published week is not a weekly pattern.** Whole Foods publishes about seven days, so
  every weekday appears once; promoting each of them would make one holiday closure that
  weekday's standing hours, and a Friday would read "Closed" for a week after Christmas. A
  day joins the weekly pattern only when its window is the one the store usually keeps; a day
  that differs stays a dated exception, where it is exactly right.
