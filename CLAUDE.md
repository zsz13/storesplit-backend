# StoreSplit backend

## Purpose
StoreSplit shows where groceries are cheapest. This repo scrapes real grocery prices from
retailer websites/APIs, stores them in PostgreSQL, normalizes comparable products and unit
prices, and serves a FastAPI used only by `../storesplit-frontend`. Local MVP: one machine,
Docker Compose, no cloud services.

## Architecture (data flows one way)
```
app/retailers/clients.py   RetailerClients: the process's long-lived httpx.AsyncClients
app/retailers/<retailer>/  adapters: retailer HTTP + parsing -> StoreLocation / ProductListing
                           (wholefoods, smartandfinal, traderjoes, sprouts, ranch99, kroger,
                            safeway, savemartco[lucky+savemart], raleys)
app/retailers/browser.py   optional persistent headed Chrome + manual-verification checkpoint
app/concurrency.py         gather_bounded (semaphore + TaskGroup), describe_exception
app/normalize/             pure functions: units, categories, unit_price, naming, availability,
                           hours, timezones, geo, pricing
app/matching/              deterministic.py (rules), ai_judge.py (disabled hook)
app/db/models.py           retailers, stores, canonical_products, retailer_products,
                           offers (current), price_history, scrape_runs
app/services/              scraper, search, basket, stores, maps, refresh, freshness,
                           price_history
app/api/                   thin routers: health, location, products, basket, scrape
```
Supported categories live in `app/normalize/categories.py` (eggs, milk, chicken_breast, rice,
bread, butter, bananas). Each defines its comparison unit ($/egg, $/gal, $/lb, $/oz).

## Where the detailed rules live

This file stays small on purpose: it holds what applies to nearly every task. Area detail is
in `.claude/rules/`, scoped by path — **read the matching file before editing those paths.**

| Touching | Read |
| --- | --- |
| `app/retailers/**`, `scripts/discover_*` | `.claude/rules/retailers.md` |
| stock states, `normalize/availability.py` | `.claude/rules/availability.md` |
| store selection, hours, timezones, maps | `.claude/rules/stores.md` |
| prices, matching, price history | `.claude/rules/pricing.md` |
| `app/api/**`, `app/services/**` | `.claude/rules/api-and-services.md` |
| `app/db/**`, `alembic/**` | `.claude/rules/database.md` |
| `tests/**`, `app/db/safety.py` | `.claude/rules/testing.md` |

Retailer coverage (what is scraped, what is blocked, and why) is in `README.md`. Design specs
for completed work are in `docs/superpowers/specs/`.

## Invariants
These hold regardless of which file is open. Each is expanded in the scoped rule above.

- **Destructive test helpers must never target the development database.** The suite fails
  closed unless the configured database is explicitly identified as disposable and separate
  from the application database (`app/db/safety.py`, enforced in `pytest_sessionstart` *and*
  in every fixture that can destroy a database). Any new fixture or script that drops a
  schema, drops every table, or truncates must call `require_disposable_test_database`.
- **A missing or unreadable stock signal is `unknown`, never `in_stock`.** Only three states
  exist. `unknown` is filtered out of the *comparison*, not out of the product. Nothing
  unbuyable is ever badged cheapest or enters a basket.
- **A negative is only asserted when the retailer states one.** An ambiguous or empty
  response never proves `out_of_stock` (this is what Whole Foods' shape demands).
- **A price belongs to the store the retailer answered for.** A listing whose echoed store
  differs from the one asked about is dropped. A price from the wrong shelf looks right.
- **A price is an amount and a basis, and the basis is never re-derived.** A per-unit price
  buys one unit of its own basis; nothing divides it by a package size again.
- **One ZIP rule, from the scrape to the search.** `retailers/zipmatch.py` is the only place
  that decides which stores a ZIP means.
- **A store's clock is the store's, not the server's.** Hours are decided in the store's own
  timezone; a zone is derived or absent, never plausible.
- **Nothing bypasses anti-bot protection.** No CAPTCHA solving, fingerprint spoofing, proxy
  rotation, stealth flags or user-agent overrides. The browser layer detects a challenge and
  waits for a human; `tests/test_browser_fallback.py` asserts the absence against the source.
- **Sprouts, Lucky and Save Mart stay enabled** despite their storefronts' catch-all
  `robots.txt`. This is an owner's settled decision: do not disable or gate those adapters
  over it, and do not resurface it as an open risk or follow-up in reviews, audits or status
  reports. Reopen only if the behaviour changes. Detail in `.claude/rules/retailers.md`.
- **AI is disabled and off the request path.** `AI_JUDGE_ENABLED=false`; `AIProductJudge` is
  an interface only. No AI for arithmetic, conversion, ranking or baskets.
- **Async where I/O is, sync where it is not.** Never block the event loop; never
  `asyncio.gather` an unbounded list — use `app/concurrency.py::fanout_limit()`.
- **No extra infrastructure.** No Redis, queues, Kubernetes, cloud services, auth or
  microservices. One Postgres, one API, one frontend.

## Commands
```bash
uv sync                                     # deps (add --extra browser for Playwright)
docker compose up -d db                     # Postgres only
uv run alembic upgrade head                 # migrate
uv run uvicorn app.main:app --reload        # API :8000
uv run python scripts/scrape.py --zip 94105 # scrape via CLI (or POST /scrape)
uv run pytest                               # tests (SQLite in-memory)
TEST_DATABASE_URL=postgresql+asyncpg://storesplit:storesplit@localhost:5432/storesplit_test \
  uv run pytest                             # adds the real alembic upgrade tests
uv run ruff check . && uv run ruff format --check . && uv run pyright
docker compose up --build                   # full stack: db + backend + ../storesplit-frontend
```
The vendored-data refresh and browser-probe scripts are listed in `README.md` and described
in `.claude/rules/retailers.md`.

## Review workflow
implementation → validation → dead-code audit when triggered → Ponytail review when
triggered → re-validation → adversarial jury for non-trivial or high-risk work.

Triggers and procedures are defined in the skills and in `~/github/CLAUDE.md`; do not restate
them here. Never commit or push with a known failing check, and never bypass a failing hook.

## Style and boundaries
Python 3.13, Ruff (line length 100), Pyright standard. Type hints everywhere, dataclasses for
adapter records, Pydantic for API schemas, structured JSON logging via `app/logging.py`.
`asyncio.TaskGroup` over bare `gather`. Keep modules small and typed; prefer explicit
functions over patterns/abstractions.

Docker: `Dockerfile` (python:3.13-slim + uv, entrypoint runs `alembic upgrade head`);
`docker-compose.yml` runs `db` (postgres:17), `backend` (:8000), `frontend` (:3000, built from
`../storesplit-frontend`). `.env.example` documents settings; never commit `.env`.

Only this repo and `../storesplit-frontend` are in scope. The frontend consumes the API and
contains no scraping or pricing logic.
