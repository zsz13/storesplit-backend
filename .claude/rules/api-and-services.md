---
paths:
  - "app/api/**"
  - "app/services/**"
  - "app/concurrency.py"
  - "tests/test_search_grouping.py"
  - "tests/test_freshness_and_refresh.py"
  - "tests/test_concurrency.py"
  - "tests/test_scrape_and_api.py"
---

# API shape, freshness and scrape concurrency

How a page is built, what the filters may and may not do, and the concurrency rules the
scrape runs under.

## Search shape and pagination

- **Search returns canonical products, one per card, paginated by product.** A product
  carried by four stores is one result with four offers inside it, never four results, and it
  takes one page slot -- paging by offer would cut a product's own offers across a page
  boundary and break the comparison. `app/services/search.py` builds a page with a fixed
  number of statements whatever the result size: one aggregate for freshness and the
  pre-filter offer count, one `COUNT` over the grouped products, one grouped/ordered/`LIMIT`ed
  query for the page's product ids, one for those products' offers with stores, retailers and
  retailer products eagerly loaded, and one for the single cheapest offer across the whole
  search (so "Cheapest" means cheapest for the query, not for the page you are reading).
  `ProductOut.best_offer` carries what a collapsed card shows and is populated **only** when
  the leading offer is `in_stock`. `tests/test_search_grouping.py` pins the statement count:
  a page of one and a page of sixty must cost the same.

## "Open now" narrows which stores are compared

- **"Open now" narrows which stores are compared, and nothing else.** `open_now` is a query
  parameter on `/products/search` and `/products/{id}/offers` and a field on
  `BasketRequest`, decided by the backend for the same reason the availability filter is: it
  also decides which offer is cheapest, so hiding a store's rows in a browser would strand
  the badge on one that is no longer shown. Because `store_ids` is already threaded through
  every search, offers and basket query, the filter is a *narrowing of that list*
  (`services/stores.py::open_now_stores`) and needs no clause of its own -- and cannot
  reorder anything, so the cheapest offer among the shops that are open is still the
  cheapest offer. **Only a confirmed closure removes a store.** A store whose retailer
  publishes no hours is not evidence of a locked door, and dropping it would quietly delete
  every Raley's and every Kroger from the comparison on the strength of a fact nobody
  has; it stays, ranked after the shops that really are open. `stores` is **never** narrowed
  by the filter: a client that filtered down to nothing has to be able to say which shops are
  shut and when they open, and `HoursTodayOut.next_open_at` -- the next opening as an
  absolute instant -- is what lets it order them. `opens_at` is the wall clock to print and
  the wrong thing to sort by: two stores in different zones print the same "8:00 AM".

## Freshness and the shared refresh

- **Freshness is reported, never enforced, and a refresh is shared.** A search older than
  `SEARCH_FRESHNESS_TTL_SECONDS` (default 1800) answers from what it has and revalidates
  behind the answer -- stale prices are the best answer anyone has, so they are shown. The
  automatic path and the manual `POST /products/refresh` both go through
  `services/refresh.py::ensure_refresh`, which guarantees one in-flight scrape per
  `(zip5, category)` key: two overlapping `run_scrape`s over one ZIP would each expire the
  offers the other had not confirmed. `SEARCH_REFRESH_COOLDOWN_SECONDS` (default 300) is the
  minimum spacing between refresh *starts* for a key and is the system's one cooldown, shared
  by both paths, so a failing key cannot be retried on every keystroke and a manual button
  cannot be re-armed by reloading. Its floor is durable: `max(in-memory start, the newest
  `scrape_runs` row for this ZIP whose categories contain this key)`, so a restart cannot
  reset it. `already_running` and `cooling_down` are 200s with a `state`, not errors -- the
  request was correct and no scrape began, which is the enforcement. A query matching no
  staple never auto-refreshes: there is no category to scope a scrape to.

## What the current offers are

- **Current offers = what the latest scrape saw.** After each (store, category) search the
  scraper deletes that store's offers in the category it did not confirm; `price_history`
  keeps the past. Multipacks and items without a parseable size are skipped, not guessed
  (Whole Foods per-pound items use the payload's `uom`).

## Bounded structured concurrency

- **Bounded structured concurrency.** Retailers run as tasks in an `asyncio.TaskGroup`,
  capped by `SCRAPE_MAX_CONCURRENT_RETAILERS`; concurrent runs are capped by
  `SCRAPE_MAX_CONCURRENT_RUNS` (1 by default, because two runs over one ZIP would each expire
  the offers the other had not confirmed).
  `SCRAPE_MAX_CONCURRENT_REQUESTS_PER_RETAILER` (8) is **one budget for a whole retailer**,
  held in a context variable and taken in `request_with_retry`. The defaults are measured, not
  guessed: over a full 94105 fetch, 4 -> 19.8s, 6 -> 15.2s, 8 -> 12.9s, 12 -> 10.1s with no
  retailer errors at any step; 8 is where the curve flattens against how hard it is fair to
  push a regional grocer. Re-measure before changing them. An adapter that fans out inside a
  search draws on the same slots, so sizing a nested fan-out from the setting directly would
  square it -- use `app/concurrency.py::fanout_limit()`, never `get_settings()`, and never
  `asyncio.gather` an unbounded list.

## Failure isolation and ingest ordering

- **A failure costs what it touched, and no more.** A (store, category) search that raises or
  runs past `SCRAPE_RETAILER_TIMEOUT_SECONDS` is recorded on its own `SearchResult`; its
  siblings finish and are written, and the run is reported `failed` naming the first error.
  A failed search is never ingested -- an empty result would expire every offer that store
  has in the category. A retailer never cancels another; `SCRAPE_DEADLINE_SECONDS` closes out
  whatever is still running, and so does any other escape, so no run row is left `running`.
- **Fetch concurrently, ingest sequentially.** Matching depends on what earlier retailers
  wrote, so ingestion keeps the requested retailer order and each retailer ingests in its own
  short-lived `AsyncSession` (one transaction per store+category, covering both the writes
  and that category's expiry). Fetching overlaps with it. `AsyncSession` is not concurrency
  safe: never share one between tasks. Because ingest is serialized, a `scrape_runs` row's
  elapsed time includes waiting for earlier retailers -- read per-request timings from the
  `http_request` logs, not from that column.
