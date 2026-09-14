# Store hours coverage, an "Open now" filter, basket unit rules, and the app shell

**Date:** 2026-09-12
**Status:** approved, ready for implementation
**Repos:** `storesplit-backend` (hours, filter), `storesplit-frontend` (filter UI, basket, shell)

## Problem

Four defects, investigated live on 2026-09-12 before anything was designed.

### 1. Four retailers show "Hours not published"

A probe that ran every registered adapter's `find_stores` and `fetch_store_details` against
94105 gives the current baseline:

| Retailer | `fetch_store_details` | Result |
| --- | --- | --- |
| wholefoods | yes | `tz=America/Los_Angeles weekly=7 dates=7` (Trinity returned `None`) |
| smartandfinal | yes | `tz=America/Los_Angeles weekly=7` |
| sprouts | yes | `tz=America/Los_Angeles weekly=7` |
| safeway | yes | `tz=America/Los_Angeles weekly=7 dates=1` |
| **lucky** | **yes** | **`tz=America/Los_Angeles weekly=7 src=lucky:stores/storeDetailsV2`** |
| **ranch99** | **no** | no capability |
| **traderjoes** | **no** | no capability |
| **raleys** | **no** | no capability |

So the four reported retailers are three different problems, not one:

- **Lucky already works.** The adapter reads `storeDetailsV2` from the banner's own site and
  returns a full week in a real IANA zone. Rows are empty because the weekly store-details
  phase has not populated them for this database, not because a source is missing. The
  frontend's `CLAUDE.md` claim that "Lucky publishes neither an address nor coordinates" is
  stale and is corrected as part of this work.
- **99 Ranch publishes everything already, in a payload the scrape has in hand.**
  `POST /be-api/store/web/nearby/stores` returns, per store, `timeZone:
  "America/Los_Angeles"` and two separate schedules:

  ```json
  "offlineBusinessTimes": [{"dayOfWeeks": "Friday - Sunday", "startTime": "08:00", "endTime": "22:00"},
                           {"dayOfWeeks": "Monday - Thursday", "startTime": "08:00", "endTime": "21:00"}],
  "onlineBusinessTimes":  [{"dayOfWeeks": "Monday - Sunday",  "startTime": "08:00", "endTime": "22:00"}]
  ```

  `offlineBusinessTimes` is the physical shop; `onlineBusinessTimes` is the delivery window
  and is **not** what a shopper standing outside the door needs. `find_stores` already makes
  this exact request, so hours cost no additional traffic.
- **Trader Joe's publishes the whole week and no zone.** The locator
  (`hosted.where2getit.com/traderjoes/rest/locatorsearch`, already called by `find_stores`)
  returns `monday_open: "09:00" … sunday_close: "21:00"` for every weekday, plus unusable
  free-text `holidayhours` / `Temp Hours Note` fields. There is no timezone anywhere in the
  record. Under the standing rule -- a wall clock with no zone is not a fact about a store --
  these hours cannot be stored as they are.
- **Raley's / Nob Hill / Bel Air publish nothing StoreSplit may read.** `robots.txt`
  disallows `/api` and `/search`. The store page is allowed and was fetched
  (`/store/448/raley_s-1601-west-capitol-ave-west-sacramentoca`, 200, 28 KB); its
  `__NEXT_DATA__` `pageProps` contains only `session` and `_nextI18Next`, i.e. interface
  strings and no store record. The only hours-shaped string on the page is
  `customerServiceHours`, which is the call centre. `local.raleys.com` and
  `stores.raleys.com` do not resolve; `www.nobhillfoods.com` and `www.belair.com` are dead
  (500 / no DNS). There is no first-party surface.

### 2. There is no "Open now" filter

Search and basket have an availability filter and no notion of whether a store is open. The
data to decide it already exists: `normalize/hours.py::hours_today` answers in the store's own
timezone and `StoreOut.hours_today` already carries the answer to the client.

### 3. The basket allows impossible quantity/unit pairs

`lib/basket.ts` renders one global 12-unit list on every row, and its own `STAPLES` table
defaults **bread to `count`**. That default is the reported bug: it produces

```
bread is compared per oz; quantity unit 'count' cannot be converted
```

from `services/basket.py::resolve_item`, as a 422 that fails the **entire** basket rather
than the one bad line.

Separately, `basketReducer` merges an added item only when *both* the query and the unit
match, so `eggs/count` and `eggs/dozen` are two rows -- and `egg` vs `eggs` is a third,
because `normalizeQuery` lowercases and trims but does not singularise. The backend
deliberately tracks basket items by position and supports a repeated query, so nothing there
is wrong; the duplicate is created by the editor.

### 4. The footer is squashed to an 18px sliver

Measured in the browser on `/`, viewport 1512x720:

```
header  top 0    bottom 54    h 54
main    top 54   bottom 671   h 618
footer  top 701  bottom 720   h 19      <- computed padding-top/bottom: 0px
```

Two independent causes, both confirmed by reading the live CSSOM:

- **`.container` clobbers the footer's vertical padding.** `globals.css` declares
  `.container { padding: 0 var(--space-4) }` -- a *shorthand*, which sets `padding-top` and
  `padding-bottom` to `0`. `Footer.module.css` declares `.inner { padding-top: var(--space-4);
  padding-bottom: var(--space-4) }`. Both selectors have specificity (0,1,0), so source order
  decides, and in the emitted stylesheet `.container` comes after the footer module. It comes
  *before* `layout.module.css`, which is why `main`'s `padding-top` survives at 22.5px and the
  footer's does not: `layout.tsx` imports `AppHeader` and `Footer` (pulling their modules)
  *before* `./globals.css`, and `./layout.module.css` after it. The footer therefore renders
  with no vertical padding, flush against the viewport edge.
- **The footer is shrinkable in the flex shell.** `.body` is `display: flex; flex-direction:
  column; min-height: 100dvh` and `.main` is `flex: 1 1 0%`. `main`'s automatic minimum size
  holds it at its 618px content height, so the 48px left over is less than the footer's
  natural height, and the footer -- at the default `flex-shrink: 1` -- absorbs the whole
  deficit instead of the page scrolling.

`AppHeader`'s `.inner` is unaffected: it sizes itself with `min-height: 3.5rem` and declares
no vertical padding, so there is nothing for the shorthand to reset.

## Design

### Hours: one ladder, first-party first, Google only in the gaps

`StoreDetails` gains exactly one optional field, so that hours a retailer published without a
zone can be carried without ever being mistaken for a schedule:

```python
@dataclass(frozen=True)
class UnzonedHours:
    """Weekday windows a retailer published without naming a zone."""

    weekly: dict[int, DayHours]


# on StoreDetails
unzoned_hours: UnzonedHours | None = None
```

`normalize/hours.py::hours_from_unzoned(unzoned, timezone)` is the only way it becomes a
`StoreHours`, and it returns `None` without a zone. The rule that keeps Trader Joe's honest is
therefore enforced by the type rather than by remembering it.

Per retailer:

- **ranch99** implements `fetch_store_details` from the record `find_stores` already read
  (cached per run in the adapter, as savemartco does). `offlineBusinessTimes` only. A new
  `parse_day_range("Friday - Sunday")` handles inclusive ranges, single days, and the wrap
  case (`Saturday - Monday`). A day range nobody can read yields nothing rather than a guess.
- **traderjoes** implements `fetch_store_details` returning `unzoned_hours` from
  `<weekday>_open` / `<weekday>_close`. `holidayhours` and `Temp Hours Note` are free text and
  are not parsed: half a week read wrongly is worse than a week admitted unknown.
- **raleys** gets no capability. It has no source.

**The Google rung** extends `services/maps.py`, beside the existing verified-place resolver,
and is reached only for a place that the existing check already accepted (brand in the name,
house number and street matching what the retailer published). A generic address search never
produces hours. It is cost-aware rather than one blanket call:

| Store state after first-party details | Field mask | SKU |
| --- | --- | --- |
| hours and zone known | *no request* | -- |
| hours known, zone missing (Trader Joe's) | `timeZone` | Places Details Pro |
| no hours (Raley's) | `regularOpeningHours,timeZone` | Places Details Enterprise |

`regularOpeningHours.periods` is mapped to `DayHours` with Google's `day` (0 = Sunday)
translated to `date.weekday()` (0 = Monday). A period with an `open` and no `close` is a
24-hour day and becomes `DayHours("00:00", "00:00")`, which `hours_today` already reads as a
day with no shut moment in it. `timeZone.id` is the IANA name.

Off unless `GOOGLE_MAPS_API_KEY` is set, exactly as `resolve_place` already is. Results are
written with `hours_source = "google:places/details"` and the existing `hours_updated_at`,
under the existing `STORE_DETAILS_TTL_SECONDS` (7 days) -- inside the 30-day limit Google's
terms place on caching Places content, and place ids remain cacheable indefinitely as today.
**First-party always wins**: Google is consulted only where the retailer published nothing.

### Open now

`HoursToday` and `HoursTodayOut` gain `next_open_at: datetime | None` -- the absolute instant
of the next opening, computed in the store's own zone. The modal has to sort openings across
stores, and the frontend is not allowed to compute open/closed itself.

`store_ids` is already threaded through every search, offers and basket query, so the filter is
a **narrowing of that list** and touches no SQL and no ranking:

```
stores_near(...) -> [Store]  ->  open_now ? drop state == "closed" : keep all  ->  store_ids
```

- `open_now: bool = False` on `GET /products/search`, `GET /products/{id}/offers`, and
  `BasketRequest`; echoed on the responses so a client can render what it asked for.
- **Only `closed` is excluded.** `unknown` stores stay, and rank **after** confirmed-open ones
  in store listings (`StoreContextBar`, the basket's single-store table), under their own
  marker. Nothing is silently presented as open.
- **Offers inside a product card stay price-ordered.** Re-ranking them by open state would
  stop "cheapest" meaning cheapest; `StoreLine` already shows each one's open/closed dot.
- **The modal** fires when the filter is on and no store is confirmed open. Native `<dialog>`
  with `showModal()`: focus trap, `Esc`, and `::backdrop`, with no dependency. It lists
  `Trader Joe's — opens 8:00 AM` sorted by `next_open_at`, and names unknown-hours stores
  separately at the bottom rather than inventing a time for them.
- Overnight windows and "Open 24 hours" already work in `hours_today` (a close at or before
  its open runs past midnight, and `_open_window` looks at yesterday as well as today). The new
  work is mapping Google's closeless period onto that, above.

### Basket units

`lib/basket.ts` gains a dimension per staple and derives the unit list from one map, so a row
offers 2-6 units instead of 12:

| Staple | Dimension | Units | Default |
| --- | --- | --- | --- |
| eggs | count | count, dozen | 1 dozen |
| milk | volume | gal, qt, pt, fl oz, L, mL | 1 gal |
| bread | mass | oz, lb, g, kg | 1 lb |
| chicken breast | mass | oz, lb, g, kg | 2 lb |
| rice | mass | oz, lb, g, kg | 5 lb |
| butter | mass | oz, lb, g, kg | 1 lb |
| bananas | mass | oz, lb, g, kg | 3 lb |

`bread + count` becomes unreachable, since `count` is no longer in bread's list and no longer
its default. A test asserts every staple's default unit converts to its category's comparison
unit, so the table cannot drift back into the same defect.

**Validation blocks comparison; it never edits the basket.** A row whose query is not a
supported staple, whose unit is incompatible with it, or whose quantity is not a positive
number, is marked invalid in place with a short inline message, and `Compare` is disabled while
any row is invalid. Nothing is silently dropped from the request: a shopper fixes it or removes
it. This replaces the current behaviour, where a bad row reaches the API and 422s the whole
basket.

**Duplicates** merge on the normalized query alone. Adding a staple already present converts
the incoming quantity into the existing row's unit and adds it. `normalizeQuery` also
singularises a trailing `s` against the known staple list, so `egg` and `eggs` are one row. The
backend's positional item tracking is unchanged.

### App shell

- `.container` becomes `padding-inline: var(--space-4)`. A longhand cannot reset vertical
  padding whatever the load order, which fixes the class of bug and not just this instance.
- `.body` becomes `display: grid; grid-template-rows: auto 1fr auto; min-height: 100dvh`.
  A grid row track sized `auto` is not squeezed the way a `flex-shrink: 1` item is, so the
  footer keeps its height on a short page and follows content on a long one, with no
  page-specific positioning anywhere.

## Testing

Backend: `parse_day_range` including the wrap case; 99 Ranch and Trader Joe's parsing against
captured fixtures; `hours_from_unzoned` returning `None` without a zone; Google period mapping
including a closeless 24-hour day and the Sunday-0 to Monday-0 shift; `next_open_at` across a
zone boundary; the `open_now` narrowing over an open, a closed and an unknown store; and the
adapter contract test extended over the two new capabilities.

Frontend: unit compatibility per staple and the default-converts guard; the merge reducer over
`egg`/`eggs` and across units; validation marking rather than dropping, and `Compare` disabled
while invalid; the dialog's contents and ordering; and a browser check at 375 and 1280 that
asserts the footer's real rendered height and that nothing scrolls horizontally.

## Out of scope

Whole Foods Trinity returning no details, and the Raley's stores that Census cannot geocode.
Both are pre-existing and unrelated to hours coverage.
