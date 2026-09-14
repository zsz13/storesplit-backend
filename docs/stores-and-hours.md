# Store selection, hours and map links

One rule decides which stores a ZIP means, shared by the scrape and the search. Hours are
decided in each store's own timezone, or not claimed at all.


One rule, shared by the scrape and by search: great-circle distance from the ZIP's Census
ZCTA centroid to the store's point, inside `SEARCH_STORE_RADIUS_MILES` (30), capped per
retailer at `SCRAPE_STORES_PER_RETAILER` (2). A store's point is its retailer-published
coordinates, or the centroid of its own ZIP when the retailer publishes none (Safeway,
Sprouts, Lucky, Walmart) — good enough to rank a store, and never used as a map pin.

This replaced a 3-digit-ZIP-prefix rule at search time that had nothing to do with how the
scrape picked stores, so stores leaked between ZIPs: Whole Foods Ocean (94112) and Stonestown
(94132) were scraped only for 94014 and were served to 94105 searches, whose own nearest two
are SoMa and Trinity.



`StoreOut` carries `hours_today` (decided in the store's own timezone) and `maps_url`. Both
are read at most once a week per store (`STORE_DETAILS_TTL_SECONDS`) during a scrape and
cached on the `stores` row; no search or product render ever fetches either.

Hours come from the retailer's own store data, each in the shape that retailer publishes:

| Retailer | Hours from | Shape | Timezone | Google place |
| --- | --- | --- | --- | --- |
| Whole Foods | `/stores/<folder>` page island | ~7 dated days, absolute UTC | `locationFacets[].timeZone` | – |
| Target | `/sl/<slug>/<id>` page (robots-allowed, no browser needed) | 14 dated days, local wall clock | `iso_time_zone_code` | `miscellaneous.google_cid` |
| Safeway | `localPage` on `local.safeway.com` (Yext profile) | weekly pattern + dated holidays | `timezone` | `googlePlaceId` |
| Smart & Final | `/api/stores`, already fetched by every scrape | one sentence, parsed strictly | `timeZone` | – |
| Sprouts | `www.sprouts.com/wp-json/…/store/<n>` — its own site, not the storefront | one window, all seven days | `timezone` | – |
| 99 Ranch | `/be-api/store/web/nearby/stores`, already fetched by every scrape | inclusive day ranges (the *door*, not the delivery window) | `timeZone` | – |
| Lucky, Save Mart | Instacart storefront store record | weekly pattern | `timeZone` | – |
| **Trader Joe's** | where2getit locator, the same call its own store-search page makes | full week, `monday_open` .. `sunday_close` | **none published** — derived from the store's coordinates | – |
| Kroger, Raley's | nothing on a surface this is allowed to read | – | – | – |

A retailer with no readable source shows "Hours unavailable" rather than a plausible
invention. Smart & Final's sentence is the only prose source; a wording the parser does not
fully understand yields no hours at all.

**Trader Joe's is the one retailer whose zone is derived rather than published.** It states a
complete week for every store and a timezone on no surface it has, and a wall clock with no
zone is not a fact about a store — so its week used to be carried as `UnzonedHours` and shown
as "Hours not published". The zone was never really missing, only unread: the same locator
record carries the store's coordinates, and a point lies in exactly one timezone.
`normalize/timezones.py` reads it offline from the timezone-boundary polygons (`tzfpy`), so
no third party is asked for a fact the published address already implies, and Trader Joe's
stays the recorded source of its own hours — only the frame the clock is read in is borrowed.
A store the locator places nowhere still shows "Hours not published".

`maps_url` opens a *business*, down a ladder that gives up certainty explicitly: a place the
retailer published (validated, then cached on `stores.maps_place_*`), then a place resolved
through the Places API and checked for brand and street address when `GOOGLE_MAPS_API_KEY` is
set, then an address search naming the retailer, then a coordinate pin, then nothing. The
search query leads with the retailer because that is what it used to lack — it queried the
branch label alone, and "San Francisco Stonestown, 285 Winston Dr" resolved to the street
rather than to the Target standing on it. A store known only by its ZIP still gets no link:
a centroid places a store for ranking and is not a door.

