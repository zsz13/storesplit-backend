# Freshness and concurrency

A search never waits for a scrape, and two refreshes over one ZIP never fight.


A search never waits for a scrape. If the newest offer behind it is older than
`SEARCH_FRESHNESS_TTL_SECONDS` (30 minutes by default) it returns what it already has and
starts a collection behind the answer, reporting `freshness.refreshing` so a client can poll
and swap the results in. Stale prices are still the best answer anybody has, so they are
shown rather than withheld.

One refresh exists per `(ZIP, category)`. The automatic path and the manual
`POST /products/refresh` go through the same registry, so pressing refresh while one is
already running joins it instead of starting a second — two overlapping runs over one ZIP
would each expire the offers the other had not confirmed yet.
`SEARCH_REFRESH_COOLDOWN_SECONDS` (5 minutes) is the minimum spacing between refresh
*starts* for a key, shared by both paths. Its floor is read back from `scrape_runs`, so it
survives a restart and cannot be re-armed by reloading the page or opening a second tab.


## Scrape semantics

Scrape semantics: each run refreshes the offers for (store, category) and removes offers the
retailer no longer lists; price history is append-only. Shelf prices are compared; loyalty
prices (Kroger promo) are reported separately.

## Scrape concurrency

Scrape concurrency: retailers are fetched in parallel and, inside a retailer, so are its
(store, category) requests. `SCRAPE_MAX_CONCURRENT_REQUESTS_PER_RETAILER` is one budget for
a whole retailer, shared with any fan-out an adapter does inside a single search, so it is
the real number of requests in flight. One slow or failing retailer is recorded as failed on
its own `scrape_runs` row and never cancels the others; within a retailer, the categories
that did come back are still written. Ingestion stays sequential in retailer order, because
matching a listing depends on the canonical products earlier retailers created, and runs are
serialized by default so two scrapes cannot expire each other's offers.
