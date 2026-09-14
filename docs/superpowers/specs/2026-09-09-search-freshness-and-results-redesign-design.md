# Search freshness, canonical grouping and results redesign

Date: 2026-09-09
Repos: `storesplit-backend` (API, freshness, grouping, images) and `storesplit-frontend` (UX).
No database migration: every column this needs already exists.

## Problem

Verified against the live stack (Postgres volume with 1602 offers / 1011 canonical products
for ZIP 94105):

1. `GET /products/search?q=eggs&zip_code=94105` returns 60 products and 115 offers in one
   payload with no pagination, and the UI renders every offer row expanded. Two rows read
   `Smart & Final $17.49` because one retailer product is stocked at two stores.
2. Sixteen store chips fill the first viewport before a single price is visible.
3. `image_url` is carried end to end and never rendered. It is also the only URL field with
   no validation at any layer: `raleys`, `target` and `walmart` call `str()` on an
   unvalidated payload value, `wholefoods` and `safeway` pass a dict through raw, and
   `smartandfinal` falls back on the container instead of the value so images are silently
   dropped when the `image` dict has no `template` key.
4. There is no freshness concept. The only collection path is `POST /scrape`, a synchronous
   full-ZIP run the browser waits ~60s for, hidden behind a `<details>`.
5. `search_products` loads every offer for the category across all 16 stores, groups in
   Python, then slices to 60. `_count_offers` materializes rows only to call `len()`.

## Freshness

Refresh key is `(zip5, category)`, where category is a category key or `"*"` for all
categories. One key owns one refresh; automatic and manual refreshes share the mechanism.

`app/services/refresh.py` holds a process-local registry, `dict[key, RefreshEntry]` guarded
by an `asyncio.Lock`:

- `ensure_refresh(key, ...)` starts `run_scrape(zip, categories=[category])` as a tracked
  `asyncio.Task` and returns `started`; if a task for the key is live it returns
  `already_running` without starting a second; if the key is inside its cooldown it returns
  `cooling_down` and starts nothing.
- The done-callback records `finished_at` and `last_error`. Tasks are held in a module-level
  set so they are not garbage collected mid-flight, and cancelled by the FastAPI lifespan.

**Cooldown is one concept, `SEARCH_REFRESH_COOLDOWN_SECONDS` (default 300), and it is the
minimum spacing between refresh *starts* for one key.** It gates automatic and manual
refreshes alike, so a failing key cannot be retried on every keystroke and a manual button
cannot be re-armed by reloading the page.

The cooldown floor is durable, not only in memory: it is
`max(entry.started_at, latest scrape_runs.started_at for this zip whose categories contain
this key)`. `scrape_runs` rows are filtered by `started_at >= now - cooldown` first, so the
query is bounded by definition and the JSON `categories` list is matched in Python (portable
across SQLite and Postgres). A process restart therefore cannot reset a cooldown, and an
all-category run correctly cools down every category key.

A `scrape_runs` row left at `running` by a hard kill is never read as "refreshing" -- only a
live in-process task is. The DB is the cooldown floor, nothing more.

### Stale-while-revalidate

`search_products` returns existing data immediately and reports `freshness`:
`last_updated_at`, `age_seconds`, `ttl_seconds`, `is_stale`, `refreshing`,
`refresh_started_at`, `cooldown_seconds`, `refresh_available_in_seconds`, `can_refresh`,
`last_error`. `is_stale` is `age_seconds > SEARCH_FRESHNESS_TTL_SECONDS` (default 1800).

When stale, `SEARCH_AUTO_REFRESH` is on and the query resolved to a category, the search
calls `ensure_refresh` and reports `refreshing: true` in the same response. A free-text
query that matches no category reports freshness but never auto-refreshes: there is no
category to scope a scrape to.

`refresh_available_in_seconds` is a relative number on purpose. The client anchors it to its
own clock and ticks down, so a clock skew between browser and API cannot show a wrong
countdown.

### Manual refresh

`POST /products/refresh` with `{zip_code, query?}`. The query resolves to a category through
`category_for_query`; with no query, or a query that matches no category, the key is
`(zip, "*")` and the refresh covers every category -- which is what the "no prices collected
yet" empty state needs.

It answers **200 with a `state` discriminator** rather than an error status, because
`already_running` and `cooling_down` are expected outcomes of a correct request, not client
mistakes, and the body carries the same `freshness` object the search returns so the client
has one rendering path:

| state | meaning |
| --- | --- |
| `started` | a refresh was started for this key |
| `already_running` | a refresh for this key is in flight; none was started |
| `cooling_down` | inside the cooldown window; nothing was started |

Enforcement is real: in `already_running` and `cooling_down` no scrape begins.

`POST /scrape` stays as the admin/CLI endpoint. The frontend stops calling it, so no browser
request waits 180s any more.

## Grouping, pagination and query cost

`search_products` becomes three statements, with offers preloaded and no N+1:

- **A** `GROUP BY canonical_products.id` selecting
  `MIN(CASE WHEN availability='in_stock' THEN COALESCE(unit_price, price) END)` as the
  in-stock leader and `MIN(COALESCE(unit_price, price))` as the fallback, ordered
  in-stock-first then cheapest, with `LIMIT`/`OFFSET`. Returns the page's product ids.
- **B** `COUNT(*)` over that grouped subquery for `total_products`. This also replaces
  `_count_offers`, which counted by materializing rows.
- **C** one `select(Offer).where(canonical_product_id.in_(page_ids))` with
  `selectinload(Offer.store).selectinload(Store.retailer)` and
  `selectinload(Offer.retailer_product)`.

Pagination is by canonical product, never by offer: `page`, `page_size` (default 20, max 60)
in, `page {page, page_size, total_products, total_pages, has_next}` out.

`ProductOut` gains what a collapsed card needs so it requires no second lookup:
`best_offer` (the whole offer), `offer_count`, `retailer_count`, `in_stock_offer_count`,
`store_count`, `price_low`, `price_high`, `image_url`. `best_offer` is populated **only**
when the leading offer is `in_stock`; unknown and out-of-stock can never win it, unchanged
from today's `best_offer_id` rule. `offers` stays sorted in-stock first, then unit price.

`unknown_products` stays a capped footnote and is returned on page 1 only, so a section
nobody can act on does not repeat under every page.

## Images

- `app/retailers/images.py::image_url_from(value)` -- the recursive str/dict/list unwrapper
  `ranch99` already proved, promoted to one owner and used by every adapter.
- `app/retailers/urls.py::clean_image_url(raw)` -- the `clean_product_url` rules minus host
  equality, because image CDNs are legitimately other hosts (`target.scene7.com`,
  `d2lnr5mha7bycj.cloudfront.net`). Rejects non-strings, object reprs, backslashes,
  credentials, non-https, hostless or pathless URLs, and anything over the 500-char column.
  Protocol-relative `//host/path` is upgraded to https.
- Applied at ingest in `scraper.py` beside `_persistable_url`, logging `rejected_image_url`.
- Adapter fixes: drop `str()` in `raleys`/`target`/`walmart`, type-guard `wholefoods`/
  `safeway`/`kroger`/`sprouts`/`savemartco`/`traderjoes`, and fix the Smart & Final
  container-vs-value fallback so a missing `template` falls through to `primaryImage` and
  then `default` instead of yielding `None`.
- `tests/test_adapter_contract.py` gains an image rule asserted against every registered
  adapter, mirroring the product-URL rule.

## Frontend

Plain CSS modules and tokens in `globals.css`. No new runtime dependency.

Patterns taken from Google Shopping (thumb + title + price block, "from $X, N offers"),
Kayak and Google Flights (one row per comparable thing, best price right-aligned, "N more
options" expanding in place), Instacart and DoorDash (sticky search and location, category
chip rail, small square product images), Airbnb (filter pills, skeletons matching the final
geometry).

Components: `AppHeader`, `SearchPanel`, `ResultsToolbar`, `StoreContextBar` (the 16 chips
collapse to one disclosure), `RefreshControl`, `ProductCard`, `OfferTable`, `Pagination`,
`ProductSkeleton`, `Thumb`, `Pill`. `OfferRow`, `ScrapeButton`, `AvailabilityFilter` and
`Header` are replaced.

`RefreshControl` renders four states from `freshness` alone: idle (`Refresh prices`),
`Refreshing...` with a spinner, cooling down (`Refresh available in 4:32`, counted down from
`refresh_available_in_seconds` against the browser clock), and a compact non-blocking error
line when `last_error` is set -- the last valid results stay on screen throughout.

While `freshness.refreshing` is true the client re-queries `/products/search` every 5s, up
to ~3 minutes, and swaps the data in.

Accessibility: expand is a real `<button aria-expanded aria-controls>`, filters are a
`radiogroup`, images carry `alt`, and a live region announces refreshing and result counts.
`min-width: 0` and `overflow-wrap` throughout; no horizontal overflow at any width.

## Verification

Backend: `ruff check`, `ruff format --check`, `pyright`, `pytest` on SQLite and again against
Postgres. Frontend: `eslint`, `prettier --check`, `tsc --noEmit`, `vitest`, `next build`.
Then `docker compose up --build`, and Playwright at 1440 and 390 across search, filters,
expand, pagination, refresh cooldown and basket, asserting no console errors and no
horizontal overflow. Finally the `adversarial-jury` skill for independent review.
