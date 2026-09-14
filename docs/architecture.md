# Architecture

How the backend is laid out, what each endpoint returns, and every setting that changes its
behaviour. See [README](../README.md) for the overview.

## Layout


```
app/
  main.py, config.py, logging.py   app factory, settings, JSON logging
  api/                             FastAPI routers (thin: validate, call service, return)
  db/                              SQLAlchemy models and session
  normalize/                       units, categories, unit price, naming (pure functions)
  matching/                        deterministic matcher; AI judge hook (disabled)
  retailers/                       adapter contract, HTTP client, one package per retailer
  services/                        scraper, search, basket, stores
alembic/                           migrations
scripts/                           scrape CLI, Whole Foods store discovery
tests/                             pytest suite + fixtures
```

---


## API


| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness + database check |
| `GET /location/zip?latitude=&longitude=` | The US ZIP whose Census centroid is nearest to a point, and how far that is. Read off the same vendored ZCTA table that then ranks stores, so a shopper who shares their location is placed at the point their store distances are measured from. Nothing leaves the machine and the coordinates are not stored. A point with no ZIP within 100 miles is a 404, not the closest one |
| `GET /products/search?q=&zip_code=&availability=&page=&page_size=` | Canonical products near a ZIP, one entry per product with every offer grouped inside it, plus unit prices, cheapest flags, a `freshness` block and a `page` block. `availability` is `in_stock` (default), `out_of_stock`, `unknown` or `all`. Pagination is **by canonical product**, never by offer: a product's own offers all travel together, so a page boundary never splits a comparison |
| `POST /products/refresh` | Collect one ZIP + category again. Returns at once with `state`: `started`, `already_running` (a refresh for this key is in flight) or `cooling_down` (inside `SEARCH_REFRESH_COOLDOWN_SECONDS` of the last start). All three are 200s — the request was correct, and in the latter two no scrape begins, which is the enforcement. Omit `query` to collect every category |
| `GET /products/{id}/offers?availability=` | All offers and price history for one product (defaults to `all`: a drill-down hides nothing) |
| `POST /scrape` | Dev/admin: collect prices for a ZIP (awaits the run; retailers fetched concurrently) |
| `GET /scrape/options` | Retailer and category keys |
| `POST /basket/compare` | Cheapest single-store vs split-store basket. Optional `availability` (default `in_stock`) — a basket never recommends an offer the shopper cannot buy |


## Configuration


See `.env.example`. Notable settings:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy URL (async driver: `asyncpg` for PostgreSQL, `aiosqlite` for SQLite) |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | Optional; enables the Kroger adapter |
| `AI_JUDGE_ENABLED` | Must stay `false`; there is no AI provider in the MVP |
| `HTTP_TIMEOUT_SECONDS` / `HTTP_CONNECT_TIMEOUT_SECONDS` / `HTTP_MAX_RETRIES` | Outbound HTTP limits |
| `HTTP_MAX_CONNECTIONS` / `HTTP_MAX_KEEPALIVE_CONNECTIONS` / `HTTP_KEEPALIVE_EXPIRY_SECONDS` | Connection pool for the long-lived clients |
| `SCRAPE_STORES_PER_RETAILER` | Stores collected per retailer per scrape (default 2) |
| `SCRAPE_MAX_CONCURRENT_RUNS` | Scrape runs at once (default 1; runs would fight over offer expiry) |
| `SCRAPE_MAX_CONCURRENT_RETAILERS` | Retailers fetched at once (default 10, one per adapter) |
| `SCRAPE_MAX_CONCURRENT_REQUESTS_PER_RETAILER` | Requests in flight for one retailer, nested fan-outs included (default 8; measured 4 -> 19.8s, 6 -> 15.2s, 8 -> 12.9s, 12 -> 10.1s over a whole 94105 fetch, no retailer errors at any step) |
| `SCRAPE_RETAILER_TIMEOUT_SECONDS` / `SCRAPE_DEADLINE_SECONDS` | Per-retailer and whole-run deadlines |
| `SEARCH_FRESHNESS_TTL_SECONDS` | How old prices may be before a search revalidates behind its own answer (default 1800) |
| `SEARCH_AUTO_REFRESH` | Whether a stale search starts that background refresh itself (default true) |
| `SEARCH_REFRESH_COOLDOWN_SECONDS` | Minimum spacing between refresh starts for one (ZIP, category); the manual refresh button counts down from it and the API enforces it (default 300) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT_SECONDS` | Async engine connection pool |

