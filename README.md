# StoreSplit — backend

StoreSplit compares grocery prices across nearby retailers, so a shopper can see where a
basket of staples is actually cheapest — normalized to comparable units, tied to a specific
store, and honest about what it does not know.

This repository is the API and data pipeline. The web client lives in
[storesplit-frontend](https://github.com/zsz13/storesplit-frontend).

```
retailer adapters -> scrape -> PostgreSQL -> normalize + match -> FastAPI -> storesplit-frontend
```

**Stack:** Python 3.13, FastAPI, SQLAlchemy 2 (async), PostgreSQL, Alembic, httpx, pytest,
Ruff, Pyright, Docker. Optional Playwright for the browser fallback.

**Scale:** 11 retailer adapters (10 collecting by default) · ~15,700 lines of application Python · ~14,700 lines of tests · 1,238 tests · 11 migrations.

---

## What it does

- Collects prices for seven staples — eggs, milk, chicken breast, rice, bread, butter,
  bananas — from ten retailers' own public surfaces, **per physical store**, near a ZIP code.
- Normalizes every price to a comparable unit, from the basis the retailer itself publishes
  (`package`, `lb`, `oz`, `each`) rather than one inferred from a product title.
- Matches listings from different retailers onto canonical products, using GTIN-14
  normalization and deterministic attribute matching.
- Represents stock as `in_stock` / `out_of_stock` / `unknown`, and treats a missing signal as
  `unknown` rather than guessing.
- Resolves which stores a ZIP actually means, by great-circle distance from Census ZCTA
  centroids, and publishes each store's opening hours **in that store's own timezone**.
- Compares a single-store basket against a split-store basket and quantifies the difference.
- Keeps append-only per-store price history.

Retailers working out of the box: Whole Foods Market, Smart & Final, Trader Joe's, Sprouts
Farmers Market, 99 Ranch Market, Safeway, Lucky Supermarkets, Save Mart, and Raley's / Bel Air
/ Nob Hill — each from its own public JSON endpoints or server-rendered pages — plus Kroger
through its official developer API. Acquisition methods, per-retailer limitations, and the
acquisition policy are documented in [docs/retailers.md](docs/retailers.md).

## Why I built it

I wanted to know which nearby store was cheapest for a week's staples, and I could not find a
consumer web tool that combined the things I actually needed: prices for the *specific* store
I would drive to, normalized to comparable units, honest about stock, aware of opening hours,
and able to tell me whether splitting a basket across two stores was worth the second stop.

Plenty of grocery sites and apps exist, and several do parts of this well. This is not a claim
that nothing comparable exists anywhere — only that I had not found a web application that put
this particular workflow together in the way I wanted, so I built one.

It started as a genuinely useful personal problem. It became the project described next.

## An agent-engineering project

StoreSplit is a personal, hobby-scale engineering project built in my free time, and also a
real test environment for my local AI coding-agent setup.

**StoreSplit was implemented through coding agents working under my direction.** I did not
hand-type the implementation line by line, and this repository does not pretend otherwise.

What that means in practice: I identified the problem, defined the product and its
requirements, chose the architecture and the invariants the system has to hold, decomposed the
work into agent-executable tasks, designed the prompts and constraints, reviewed what came
back, **rejected what was wrong**, drove debugging and root-cause investigation through the
agents, and defined the validation every change had to survive before it landed.

**Agent-built does not mean unreviewed or blindly accepted.** Changes went through structured
review loops — adversarial review by independent reviewer agents, a dead-code audit, and a
complexity/simplification review — and through deterministic gates that do not care what
produced the diff: the test suite, Ruff, Pyright, and Alembic migration checks. Agent output
that failed a gate, contradicted an invariant, or proposed an abstraction the architecture did
not need was rejected rather than merged. Several decisions documented here exist *because* a
proposed implementation was wrong and the investigation found the real cause.

My role: product opportunity and requirements · architecture and module boundaries · task
decomposition · prompt and constraint design · review and rejection of agent output ·
debugging direction and root-cause investigation · validation requirements and review loops ·
database and data-integrity safeguards · test strategy · iteration across backend and frontend.

→ Full detail in [docs/agent-engineering.md](docs/agent-engineering.md). The reusable agent
configuration itself lives in [zsz13/claude-code-config](https://github.com/zsz13/claude-code-config).

### On measurements

Where this repository gives a number, it is a measurement of **StoreSplit's** behaviour
against real retailer data — concurrency throughput (4 → 19.8s, 6 → 15.2s, 8 → 12.9s,
12 → 10.1s over a whole 94105 fetch), the Target challenge rate over ten live runs, the
13-of-86 Whole Foods availability flap, the 8-of-1260 Lucky `lowStock` reclassification.

**Measuring StoreSplit is not the same as measuring agent effectiveness.** No claim is made
here that agents made development faster, produced better code than a human would have, or
improved quality by some percentage — I have not run the controlled comparison that would
support any of those. Benchmark methodology for the agent workflows belongs in
[claude-code-config](https://github.com/zsz13/claude-code-config).

---

## Key engineering challenges

| Challenge | Why it is hard | Detail |
|---|---|---|
| **Availability semantics** | Every retailer publishes a different signal, and several publish something that *looks* like stock and is not. A missing signal must never become "in stock" — `unknown` is better than a false `out_of_stock`, and a retailer that publishes nothing must not vanish from the comparison entirely | [availability.md](docs/availability.md) |
| **Price normalization** | A number on a shelf label is not a price until you know what it buys. A per-pound rate divided by a pack size read off the title was published five times too cheap — and ranked first | [pricing.md](docs/pricing.md) |
| **Variable-weight pricing** | A tray with no single weight has no single size; its facts are carried from the retailer, never computed | [pricing.md](docs/pricing.md) |
| **Exact-store and ZIP semantics** | Store selection at search time and at scrape time must use one rule, or stores leak between ZIPs. A price belongs to the store the retailer answered for — a price from the wrong shelf is worse than no price, because it looks right | [stores-and-hours.md](docs/stores-and-hours.md) |
| **Store-local hours and timezones** | Nine retailers publish hours in nine shapes, and one publishes no timezone at all. A wall clock with no zone is not a fact about a store | [stores-and-hours.md](docs/stores-and-hours.md) |
| **Freshness without blocking** | A search must never wait for a scrape, and two refreshes over one ZIP must not expire each other's offers. Stale prices are shown rather than withheld | [freshness-and-concurrency.md](docs/freshness-and-concurrency.md) |
| **Database safety** | Destructive test helpers must never reach the application's database. The guard fails closed and cannot be satisfied by a name alone | [testing-and-safety.md](docs/testing-and-safety.md) |
| **Unreliable retailer surfaces** | The last-resort acquisition path must detect a challenge without ever defeating one, and prove it does not disguise itself | [browser-fallback.md](docs/browser-fallback.md) |
| **Basket optimization** | Comparing one-store against split-store shopping requires every candidate offer to be buyable, comparable, and from a store the shopper can actually reach | [architecture.md](docs/architecture.md) |
| **Price history** | A series belongs to one exact store; averaging two branches of a chain shows a price nobody was charged | [architecture.md](docs/architecture.md) |

### The browser fallback, in brief

An optional last step, **off by default** — no registered adapter needs it. It runs only after
the official API, the site's own JSON, embedded page data and sitemaps have been tried.

It uses **no stealth plugin, no fingerprint or user-agent spoofing, no `navigator.webdriver`
patch, no proxy rotation and no CAPTCHA solver**, and `tests/test_browser_fallback.py` asserts
their absence against the source. It does not defeat challenges: when a retailer asks for a
human, it raises, leaves the window open, and waits passively for a person.

Target and Walmart adapters exist and are tested against captured payloads, but **neither
collects prices in a default run** — Target is gated off behind `BROWSER_FALLBACK_ENABLED`,
and Walmart is not registered at all. The reason is measured reliability: over ten live runs
against a human-verified session, Target was challenged twice, once inside an ordinary scrape.

→ [docs/browser-fallback.md](docs/browser-fallback.md)

---

## Quick start

**Prerequisites:** Python 3.13 with [uv](https://docs.astral.sh/uv/), and Docker.

```bash
docker compose up --build
# API:      http://localhost:8000  (docs at /docs)
# Frontend: http://localhost:3000  (built from ../storesplit-frontend)
# Postgres: localhost:5432         user/password/db = storesplit
```

The backend applies migrations on start. The database starts empty; collect prices for a ZIP:

```bash
curl -X POST http://localhost:8000/scrape -H 'content-type: application/json' \
  -d '{"zip_code": "94105"}'
```

Without Docker for the app itself:

```bash
uv sync                                   # dependencies
cp .env.example .env                      # local settings
docker compose up -d db                   # PostgreSQL only
uv run alembic upgrade head               # schema
uv run uvicorn app.main:app --reload      # API on :8000
```

```bash
curl 'localhost:8000/products/search?q=eggs&zip_code=94105'
curl -X POST localhost:8000/basket/compare -H 'content-type: application/json' \
  -d '{"zip_code":"94105","items":[{"query":"eggs","quantity":60,"unit":"count"}]}'
```

Every endpoint and setting is in [docs/architecture.md](docs/architecture.md).

## Validation and quality gates

```bash
uv run pytest                                          # 1215 pass, 23 Postgres-only skipped
TEST_DATABASE_URL=...storesplit_test uv run pytest     # 1238 pass, + real Alembic upgrades
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

Tests run against fixtures captured from the retailers and an in-memory fake adapter; they
**never hit live websites**. The PostgreSQL run adds the migration tests, which replay every
Alembic revision — and are the reason the fail-closed database guard exists.

→ [docs/testing-and-safety.md](docs/testing-and-safety.md)

## Documentation

| Document | Contents |
|---|---|
| [Architecture](docs/architecture.md) | Module layout, API reference, every configuration setting |
| [Availability semantics](docs/availability.md) | The three states, what each retailer publishes, and why ambiguity is never a negative |
| [Pricing model](docs/pricing.md) | Price basis, variable-weight items, the double-normalization defect |
| [Retailer integrations](docs/retailers.md) | Per-retailer acquisition methods, coverage, limitations, acquisition policy |
| [Browser fallback](docs/browser-fallback.md) | Session handling, challenge detection, manual verification, diagnostics |
| [Store selection and hours](docs/stores-and-hours.md) | ZCTA-centroid ranking, timezone derivation, the map-link ladder |
| [Freshness and concurrency](docs/freshness-and-concurrency.md) | Stale-while-revalidate, single-flight refresh, concurrency budgets |
| [Testing and safety](docs/testing-and-safety.md) | Quality gates, migration validation, the fail-closed database guard |
| [Agent engineering](docs/agent-engineering.md) | What agent-built means here, the review loops, what is and is not measured |

Design specifications written before implementation — problem, evidence, decision, rejected
alternatives — are in [`docs/superpowers/specs/`](docs/superpowers/specs/).

Agent instructions are in [`CLAUDE.md`](CLAUDE.md), with area detail in
[`.claude/rules/`](.claude/rules/); [`.claude/rules/README.md`](.claude/rules/README.md)
explains why they are split that way.

## Licensing

**Not open source.** This repository is public so the work can be read and evaluated — for
technical review, assessment, and study. Viewing, evaluating, and building it locally are
permitted. Copying it for reuse, modifying it, redistributing it, deploying it, or
incorporating it into another product or service require prior written permission.

Third-party dependencies are not redistributed here and keep their own licences. Retailer and
company names identify data sources only; StoreSplit is not affiliated with, endorsed by, or
connected to any retailer named here.

See [LICENSE](LICENSE) for the full terms.
