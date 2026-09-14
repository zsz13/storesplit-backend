---
paths:
  - "app/retailers/**"
  - "scripts/discover_*.py"
  - "scripts/probe_browser_retailer.py"
  - "scripts/start_chrome_cdp.py"
  - "tests/test_adapter_contract.py"
  - "tests/fixtures/**"
---

# Retailer adapters, acquisition and the browser fallback

Everything that only matters while touching an adapter, its vendored data, or the optional
browser layer. The acquisition order and the no-bypass rule are also stated in root
`CLAUDE.md`, because they constrain reviews regardless of which file is open.

## Adapter boundaries and contract

- **Retailer logic stays in its adapter package.** URLs, payload shapes, headers, quirks and
  fixtures belong under `app/retailers/<slug>/`. Adapters return `ProductListing` /
  `StoreLocation` only; they never touch the DB or category/unit normalization.
- **Adapter contract** (`app/retailers/base.py`): `slug`, `name`, `site_url` and
  `is_configured` (sync), plus the async `find_stores(zip)`, `search_products(query, store)`,
  `fetch_product(sku, store)`, `fetch_offers(sku, stores)`. An adapter is constructed with the
  `RetailerClients` pool and owns no resource: it never creates or closes a client. Register
  new adapters in `app/retailers/__init__.py` and add them to `tests/test_adapter_contract.py`,
  which asserts the URL and availability rules below against every registered retailer.

## URLs and images

- **A product URL is a real page on the retailer's own host, or it is `None`.** Adapters build
  it through `retailers/urls.py::clean_product_url(raw, base_url=SITE_URL)`, never with a bare
  f-string: it rejects non-strings (a nested payload *object* stringified into the field is
  what produced `{'id': ..., 'canonicalUrl': None}` links), placeholders, relative paths that
  the browser would resolve against StoreSplit's own origin, non-https schemes, other hosts,
  embedded credentials, and backslashes (where Python's parser and a browser's disagree about
  which host they name). The scrape service checks it again against the adapter's `site_url`
  before writing, so no path can persist a value these rules reject. Never invent a URL the
  retailer does not really serve.
- **An image URL is validated like every other URL.** It was the one field with no gate
  anywhere: `raleys`, `target` and `walmart` called `str()` on an unvalidated payload value,
  which turns a nested image object into `"{'url': None}"` -- a non-empty string a
  `String(500)` column accepts and a browser then resolves against StoreSplit's own origin.
  Adapters now read it with `retailers/images.py::listing_image_url`, the recursive
  str/dict/list unwrapper 99 Ranch proved, and `retailers/urls.py::clean_image_url` applies
  the `clean_product_url` rules minus host equality -- a retailer's images legitimately live
  on a CDN that is not its product host. The scrape service checks it again with
  `valid_image_url` before writing. An unsubstituted CDN template (`{size}`,
  `{width=}x{height=}`) is rejected: it is a 404, not a picture. Smart & Final's `template`
  serves `cell`/`detail`/`zoom` only -- `medium`, which this once substituted, 404s for every
  product. `tests/test_adapter_contract.py` asserts against every registered adapter that its
  images validate, carry no object repr, and that at least one listing has one.

## Acquisition order and the browser fallback

- **Acquisition preference:** official API > site JSON/XHR endpoint > embedded page data >
  store locator/sitemaps > browser automation (`retailers/browser.py`, an optional `browser`
  extra, off unless `BROWSER_FALLBACK_ENABLED`; no registered adapter needs a browser today).
  No CAPTCHA/anti-bot bypass, no auth bypass, no proxy rotation or fingerprint spoofing.
  The browser layer prefers **attaching** to a Chrome that is already running
  (`existing_chrome_cdp`) and falls back to launching the system Chrome on a persistent
  profile (`persistent_system_chrome`); `scripts/start_chrome_cdp.py` opens the first.
  Either way the session -- cookies, logins, chosen store, and any verification a human
  completed -- is reused rather than rebuilt, and an attached browser is never closed on
  shutdown, only disconnected from (leaving it one tab, or the next attach fails).
  **Nothing about the browser is disguised**: no stealth plugin, launch flag, user-agent
  override, `navigator.webdriver` patch, fingerprint or TLS tampering, proxy rotation or
  CAPTCHA solver -- `tests/test_browser_fallback.py` asserts their absence against the source.
  It never works around a challenge: it detects one -- in the rendered page, in first-party
  data responses that come back as a PerimeterX block (Target's shape), and in a main document
  the origin refused outright, which Playwright hands back as an ordinary 403 or 429 response
  rather than raising --
  raises `ManualVerificationRequiredError`, and waits **passively**, watching the page's own
  text and its own navigations without reloading, because a reload throws away a
  press-and-hold somebody is partway through. Each retailer gets its own page, so one
  checkpoint never stops the others. `scripts/probe_browser_retailer.py` is the operator's end
  of it, and `BrowserSession.diagnostics()` reports which mode is active, what the session
  cost (`page_loads`) and what it did not have to (`pages_reused_from_capture`,
  `pages_continued`), beside `data_responses` and `challenges`.
  **A page load waits for the page's own data, not for a fixed stretch of clock.** `settle_ms`
  is a ceiling: the settle watches the first-party responses the load is collecting and stops
  when they answer the caller or go quiet, and a page that fetched nothing waits the whole
  ceiling because that is what a challenge looks like. And when a kept capture does not satisfy
  the next caller, the page it came from is read *further* rather than loaded again, which is
  what makes a seven-category Target scrape seven page loads instead of eight.
  A host whose `robots.txt` disallows the
  endpoint is not scraped -- with one standing, owner-approved exception, the three Instacart
  white-label storefronts, recorded in the next bullet. Target redsky is `Disallow: /`, and
  Raley's `/api` and `/search` are disallowed, so the Raley's adapter reads only `/product/*`
  and `/sitemap/*`. Guest session cookies the site hands every visitor (Sprouts, Lucky, Save
  Mart), the site's own store-selection cookie (Raley's `FLDR.User`) and public constants in
  the site's JS (Trader Joe's locator app key, Sprouts/Save Mart Co persisted-query hashes,
  Safeway's `ocp-apim-subscription-key`) are fine. The retailer coverage matrix lives in
  README.md.

## The decided Instacart storefront exception

- **Sprouts, Lucky and Save Mart are a decided exception to that rule -- settled, not open.**
  `shop.sprouts.com`, `shop.luckysupermarkets.com` and `shop.savemart.com` are Instacart
  white-label storefronts whose `robots.txt` lists ~20 named crawlers permissively and then
  ends with a catch-all `User-Agent: *` / `Disallow: /` (verified live 2026-09-12). A generic
  client matches that catch-all on every path, including `/idp/v1/init`, `/idp/v1/shops`,
  `/store/<banner>/storefront` and `/graphql` -- which is the whole of how these three
  adapters read prices and stores. **The project has weighed this and keeps all three
  enabled.** Concretely: do not disable or gate the Sprouts, Lucky or Save Mart adapters over
  it, do not alter their price or store acquisition because of it, and do not resurface it as
  a pending blocker, open risk or follow-up in reviews, audits or status reports -- it has an
  owner's answer already. Reopen it only if the *behaviour* materially changes: a block, a
  rate limit, a legal or ToS notice addressed to the project, or a robots.txt change that
  goes past the catch-all. Two things this exception does **not** cover: Sprouts' hours,
  which come from `www.sprouts.com` -- a different host with its own permissive robots.txt,
  never in question; and every other retailer, where the rule stands unchanged (Target redsky
  and Raley's `/api` + `/search` stay unscraped).

## Vendored acquisition data

- **Vendored acquisition data** lives next to its adapter and is refreshed by a script, never
  at scrape time: `wholefoods/stores.json`, `safeway/seeds.json` (Safeway answers no keyword
  search, so a category starts from seed products whose shelf neighbours it lists),
  `raleys/stores.json` and `raleys/catalogue.json`. Each has a `scripts/discover_*.py`.
  **A discovery script must not decide anything a public database can decide for it.**
  Raley's sitemap slug glues the street type onto the city with no separator
  (`...-2531-blanding-avenue` + `alameda` + `ca`), and splitting it by rule produced store
  names like "Nob Hill Nuealameda", "Raley's Reetfairfield" and "Raley's Ivereno", plus
  addresses so wrong that 17 of 114 stores never geocoded -- and a store with no coordinates
  is dropped from distance ranking entirely. The slug is now only a search term: candidate
  readings go to the Census geocoder, longest street type first, and the store's street,
  city, state and ZIP come back from its canonical answer (108 of 115 stores now place; the
  seven that do not are addresses Census genuinely lacks, and they carry no coordinates and
  so never rank). `wholefoods/stores.json` also carries each store's `folder`, the slug its
  own page lives at, because the summary endpoint's three-letter `folder` 404s and a display
  name matches the published slug for only four stores in five.

## HTTP clients and hygiene

- **HTTP hygiene:** use `retailers/http.py` (timeouts, bounded retries on 429/5xx and
  transport errors, JSON request logs). Never download images/fonts/analytics.
- **One client per process, never per request.** `RetailerClients` is opened by the FastAPI
  lifespan and closed on shutdown; the shared client is built at startup so bad HTTP settings
  fail there. Most retailers take `clients.shared()`. A retailer gets `clients.own(slug)`
  only for session state of its own -- in practice a guest session cookie jar (Sprouts,
  Lucky, Save Mart). Sharing a jar is safe because cookies are domain-scoped, and Raley's
  per-request `Cookie` header survives it: `http.cookiejar` never injects a stored cookie
  over a header the caller already set (pinned by a test in `tests/test_http.py`).

## Maintenance commands

Vendored data is refreshed by script, never at scrape time. Each writes the JSON file that
lives beside its adapter.

```bash
uv run python scripts/discover_wholefoods_stores.py                 # WFM store directory
uv run python scripts/discover_wholefoods_stores.py --folders-only  # just the store-page slugs
uv run python scripts/discover_safeway_seeds.py                     # Safeway category seeds
uv run python scripts/discover_raleys_stores.py                     # Raley's store directory
uv run python scripts/discover_raleys_products.py                   # Raley's per-category catalogue
uv run python scripts/audit_offers.py --zip 94105                   # audit what a scrape wrote
uv run --extra browser python scripts/audit_offers.py --zip 94105 --check-urls
uv run python scripts/start_chrome_cdp.py                           # Chrome with its DevTools port open
uv run --extra browser python scripts/probe_browser_retailer.py --wait
```
