# Store-location correctness, Whole Foods availability, and store details in the UI

**Date:** 2026-09-10
**Status:** approved, ready for implementation
**Repos:** `storesplit-backend` (primary), `storesplit-frontend` (offer detail row)

## Problem

Three defects, one of which turned out to have a different cause than reported.

1. **Raley's "does not use the selected ZIP".** The adapter does. `find_stores` ranks by
   great-circle distance from the ZIP's Census ZCTA centroid and returns different stores for
   different ZIPs; the store-selection cookie works (the same banana is $0.89 at store `01`
   and $0.99 at store `415`, and the page echoes the store back in
   `pageProps.currentStoreNumber` and `price.channel.key`). What is wrong is the *vendored
   store directory* and the *search-time store set*, described below.

2. **Stores leak across ZIPs at search time, for every retailer.**
   `services/stores.py::stores_near` selects stores by 3-digit ZIP prefix or an explicit
   `served_zip_codes` hit. That rule has nothing to do with the ranking the scrape used.
   Measured on the live database: Whole Foods **Ocean (94112)** and **Stonestown (94132)**
   were resolved and scraped only for **94014**, yet both match prefix `941` and are served
   to **94105** searches, whose own Whole Foods set is SoMa / Trinity / Franklin / Potrero
   Hill. The stores a ZIP genuinely resolves to qualify only by luck of `served_zip_codes`.

3. **Whole Foods maps a stated "Out of Stock" to `unknown`.** `/api/wwos/products` returns
   `availability: null` with `offerDetails: null` for ASIN `B0014GPSKQ` at stores 10151,
   10152 and 10432, and the product page renders exactly that record as **"Out of Stock"**
   plus **"Currently not sold in SoMa"**. The adapter treats every non-`IN_STOCK` value as
   `unknown`, so a first-party negative is discarded.

Two supporting defects found while investigating:

4. **Raley's vendored `stores.json` is corrupt for a meaningful fraction of stores.** The
   store sitemap slug runs the street suffix into the city with no separator
   (`nob-hill-foods-2531-blanding-avenue` + `alameda` + `ca`), and
   `scripts/discover_raleys_stores.py::split_slug` mis-splits it. Result: store names such as
   `Nob Hill Nuealameda`, `Raley's Reetfairfield`, `Raley's Ivereno`, `95A Nfernley`, wrong
   `city` values, and **17 of 114 stores with no coordinates at all** because the mangled
   address failed to geocode. A store with no coordinates is dropped from distance ranking
   entirely, so it can only ever reach a shopper through the buggy prefix path.

5. **`fetch_product_page` renders the default location.** It fetches
   `/grocery/product/<slug>` with no `?store=`, which the adapter's own docstring then
   generalises into the false claim that the product page's `availability` "is always null".
   With `?store=` the page is fully store-scoped: different `offerListingDiscriminator`,
   different `storeDetailsInitialData`, and different prices ($0.99 SoMa vs $0.89 Ocean).

## Constraints

- No new infrastructure, no new runtime dependency without a concrete reason.
- Acquisition preference unchanged: official API > site JSON/XHR > embedded page data >
  locator/sitemaps > browser. No robots-disallowed path. No browser request per product.
- Availability is never guessed. `unknown` stays the answer to an absent signal.
- Retailer logic stays inside `app/retailers/<slug>/`.
- The other eight adapters must not have to change for a capability only one of them has.

## Design

### 1. One deterministic ZIP -> store rule

`app/retailers/zipmatch.py` becomes the single authority and is made generic over anything
carrying `latitude` / `longitude` / `zip_code` / an id — both the adapters' `StoreLocation`
and the database's `Store`.

```
store_point(store) -> (lat, lng, precision) | None
    precision "exact"        retailer-published coordinates
    precision "zip_centroid" the ZCTA centroid of the store's own ZIP code
    None                     no coordinates and no usable ZIP
```

The `zip_centroid` tier exists because **Safeway, Sprouts, Save Mart/Lucky and Walmart
publish no coordinates** — 11 of the 26 stores in the live database have none. A pure
distance rule without this tier would delete those retailers from every search. Placing a
store at the centroid of its own ZIP is accurate to a few miles, comes from the same vendored
public dataset already in the repo, and is a fact about that store — not a default location.

`services/stores.py::stores_near(db, zip_code)` is rewritten to:

1. rank every store by distance from the searched ZIP's centroid, using `store_point`;
2. drop anything beyond `SEARCH_STORE_RADIUS_MILES` (new setting, default 30);
3. cap each retailer at `scrape_stores_per_retailer` (2), nearest first.

The 3-digit-prefix rule survives **only** when the searched ZIP itself has no centroid, and
logs that it did so. `services/basket.py` calls the same function, so the two ranked surfaces
cannot disagree about which stores a ZIP means.

A store is therefore shown for a ZIP if and only if that ZIP's own ranking would pick it,
whatever ZIP's scrape discovered it.

### 2. Store identity, hours and Google Maps

**Migration.** `stores` gains `timezone`, `hours` (JSON), `hours_source`, `hours_updated_at`,
`phone`.

**Optional adapter capability.** `fetch_store_details(store) -> StoreDetails | None`,
discovered with `getattr` exactly like the existing optional `unconfigured_reason`. It is
deliberately *not* added to the `RetailerAdapter` Protocol: nine adapters have no source for
it, and a Protocol method that eight implementations raise on is a worse contract than an
absent one. `StoreDetails` is a frozen dataclass: name, address, coordinates, timezone,
phone, and `hours` as seven weekday windows plus dated overrides.

Whole Foods implements it from `/stores/<folder>`, whose `detail-page-state` JSON island
carries `operationalDailyHours` (per-date UTC open/close windows), the postal address,
coordinates, `timeZone`, and `storeCode` — which proves the page is the store that was asked
for. The store folder slug comes from the published stores sitemap.

**Refresh policy.** Store details are fetched at most once per `STORE_DETAILS_TTL_SECONDS`
(default 7 days) per store, during a scrape, and cached in the database. Never per search,
never per product.

**API.** `StoreOut` gains `latitude`, `longitude`, `timezone`, `maps_url` and `hours_today`:

```
hours_today = {"state": "open" | "closed" | "unknown",
               "opens_at": "08:00" | null,
               "closes_at": "22:00" | null,
               "opens_day": "today" | "tomorrow" | null}
```

The state is computed server-side in the store's own timezone with `zoneinfo`, so DST and
past-midnight closing times are correct in one place. Formatting stays in the frontend.

**`maps_url`** is built server-side:

- retailer-exact coordinates -> `https://www.google.com/maps/search/?api=1&query=<lat>,<lng>`
- else a full street address -> `...&query=<urlencoded "name, street, city, state zip">`
- else `None`, and no link is rendered.

ZIP-centroid coordinates are never used for a map pin. They place a store for ranking; they
do not claim to be its address.

### 3. Whole Foods: prove the store, then classify

**Context before availability.** The per-store product page fetch that already reads
`offerListingDiscriminator` also reads `pageProps.overrideStoreId`,
`pageProps.isDefaultLocation` and `wfmccLocationData.cateringStoreContext.almAttributes
.storeId`. If the page's store is not the requested store, or `isDefaultLocation` is true,
the discriminator is refused and **every listing for that store stays `unknown`**.
`fetch_product_page` takes the store it is reading for. `locationCookie.name` reports "Lamar"
(Austin, TX) at every store and is documented as the decoy it is — `overrideStoreId`,
`isDefaultLocation` and `almAttributes.storeId` are the truth.

**Classification**, entirely from the batched `/api/wwos/products` record for the proven
store. No new per-product request: the batch already answers 50 ASINs per call and returns
the same record the product page renders from.

| Signal at the proven store | Result |
| --- | --- |
| `availability == "IN_STOCK"` | `in_stock` |
| mapped negative state (`OUT_OF_STOCK`, `NOT_AVAILABLE`, `UNAVAILABLE`) | `out_of_stock` |
| `availability` null **and** `offerDetails` null, ASIN present in the response | `out_of_stock` |
| `availability` null **and** a fulfillment signal (`offerListingId`, delivery promise) | `in_stock` |
| a price, but no fulfillment signal and no stated availability | `unknown` |
| ASIN absent from the response, request failed, or an unmapped state | `unknown` |

The third row is the retailer's own rendering rule, not an inference: the page prints
"Out of Stock" and "Currently not sold in `<store>`" for exactly that shape, verified at
three stores. The fourth row is a *fulfillment* signal, never a price — a price alone must
not promote an offer, which is why `offerDetails.price` is not in the positive column.

**Cross-adapter invariant.** `Offer.store_context` (new nullable column) records the
retailer's own echo of the store it priced: Whole Foods `storeId`, Raley's
`currentStoreNumber` / `price.channel.key`. The scrape service asserts it equals the
requested store's `external_id` and **drops the listing on a mismatch** — a price from the
wrong store is worse than no price. Exposed as `OfferOut.store_context`.

### 4. Raley's

The slug parser is fixed, but the durable repair is to stop trusting the slug. The Census
geocoder already returns a canonical matched address — street, city, state, ZIP — so
discovery takes the store's address and city from the geocoder's response and uses the slug
only as the query seed. That removes the `Nuealameda` / `Reetfairfield` / `Ivereno` class of
failure rather than patching one instance of it. Discovery is re-run for all 114 stores and
the corrected `stores.json` is committed.

The adapter gains the same store-context assertion as Whole Foods. Raley's publishes no store
hours on any robots-allowed surface — its store pages are client-rendered from the
disallowed `/api` — so `hours` stays null and its rows read "Hours unavailable", documented
with the reason so nobody re-litigates it.

### 5. Frontend

Inside expanded product details only, each offer row gains a compact second line:

```
retailer · exact store · price/unit price · availability · updated · today's hours · Maps · View at retailer
```

Collapsed cards are untouched — this is detail, not a badge on every card.

- `lib/links.ts` gains `mapsLinkHref`, host-checking the Maps URL the way `productLinkHref`
  checks product URLs. No URL reaches an `href` unhardened.
- `lib/format.ts` gains the hours label: `Open until 10:00 PM`, `Closed · Opens 8:00 AM`,
  `Hours unavailable`.
- Best Price and basket eligibility already require `in_stock`; the rule is pinned by a test
  rather than assumed.

## Testing

Backend:

- ZIP ranking: different ZIPs resolve to different, appropriate stores for Raley's and Whole
  Foods, from vendored data with no network.
- `stores_near`: the measured 94014 -> 94105 leak as a named regression; a coordinate-less
  store placed by its own ZIP centroid; a store outside the radius excluded whatever ZIP
  scraped it; prefix fallback only for a centroid-less ZIP.
- Whole Foods availability: table-driven over the recorded payload shapes, including
  `B0014GPSKQ` (null + null `offerDetails` -> `out_of_stock`) and `B0787Y4V6T` (`IN_STOCK`
  with `offerListingId` and a delivery promise -> `in_stock`), ASIN absent -> `unknown`,
  unmapped state -> `unknown`, price without fulfillment -> `unknown`.
- Store context: a product page whose `storeId` differs from the requested store refuses the
  discriminator and leaves listings `unknown`; a Raley's page whose channel differs drops the
  listing.
- Hours: `operationalDailyHours` -> today's window in the store's timezone; open, closed and
  unknown states; a past-midnight close; `maps_url` from exact coordinates, from an address,
  and absent.
- `test_adapter_contract.py`: every registered adapter ranks two distant ZIPs differently,
  and any adapter implementing `fetch_store_details` returns the store it was asked for.

Frontend: the details row renders store, address, hours and Maps; the hours-unavailable
state; a Maps URL on a non-Google host is not rendered; only `in_stock` offers are eligible
for Best.

## Verification

Live checks against at least 94014 and 94105 proving: Raley's changes stores with the ZIP;
Whole Foods uses the intended stores per ZIP; availability corresponds to the selected store;
an explicit "Out of Stock" never becomes `in_stock`; positive fulfillment at the correct
store becomes `in_stock`; ambiguous cases stay `unknown`; Maps links open the right stores;
hours match the store and day. Then full lint, typecheck, tests and build in both repos, the
Docker stack, and browser verification of the rendered detail row.

## Accepted consequences

- Whole Foods items whose record is null/null move from the "Availability unknown" section
  out of the default view entirely. That is the requested semantics and it matches the
  retailer's own page, but it is a visible change in what a default search shows.
- Re-running Raley's discovery makes roughly 114 requests to the public Census geocoder.
- `SEARCH_STORE_RADIUS_MILES` and the per-retailer cap now decide what a shopper sees. A ZIP
  with no store inside the radius shows that retailer nothing, deliberately, rather than
  showing a distant store.
