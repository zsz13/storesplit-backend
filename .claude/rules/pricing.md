---
paths:
  - "app/normalize/**"
  - "app/matching/**"
  - "app/services/scraper.py"
  - "app/services/price_history.py"
  - "app/services/basket.py"
  - "tests/test_pric*.py"
  - "tests/test_unit_price.py"
  - "tests/test_units.py"
  - "tests/test_matching.py"
  - "tests/test_gtin.py"
  - "tests/test_variable_weight_api.py"
---

# Prices, matching and price history

What a price is a price *of*, which store it belongs to, how listings become canonical
products, and how history is recorded. Root `CLAUDE.md` states the two invariants that
constrain every task (basis is never re-derived; a price belongs to the store the retailer
answered for); the reasoning is here.

## A price belongs to the store the retailer answered for

- **A price belongs to the store the retailer answered for.** Adapters report the retailer's
  own echo in `ProductListing.store_context` -- Whole Foods' `storeId`, Raley's
  `currentStoreNumber` and its commercetools price channel -- and `ingest_listing` drops any
  listing whose echo names a different store than the one it asked about. A price from the
  wrong shelf is worse than no price, because it looks right. Whole Foods proves its context
  before believing any availability at all: the per-store page fetched for the
  `offerListingDiscriminator` must show `isDefaultLocation: false` and the same store in both
  `overrideStoreId` and `almAttributes.storeId`, or the discriminator is refused and every
  listing for that store stays `unknown`. That page is read **once per store under a lock**:
  a scrape searches every category for a store concurrently, and a plain check-then-fill
  around the `await` let each of them start its own fetch -- so the page was read once per
  category and whichever finished last decided the store's availability for the run.
  Raley's refuses a page that names *no* store as firmly as one naming another: a page
  stating no store is what a store cookie that stopped taking looks like, and the site then
  prices for its own default. `locationCookie` is never read -- it reports
  "Lamar", Austin TX on every page, San Francisco ones included, and reading it is what made
  a correctly store-scoped page look like a default one. `fetch_product_page` is always
  store-scoped for the same reason: without `?store=` the page really does render for a
  default location with no availability and no price, which is what once made "the product
  page's availability is always null" look like a fact about the page.

## A price is an amount and a basis

- **A price is an amount and a basis, and the basis is never re-derived.** `ProductListing`
  and `offers` carry `price_basis` (`package`, `lb`, `oz`, `each`) beside the amount, from
  the retailer's own statement -- Target's `formatted_unit_price_suffix`, Kroger's `soldBy`,
  Safeway's `sellByWeight`. **A per-unit price buys one unit of its own basis and nothing
  else is read** (`normalize/pricing.py::basis_quantity`, used by `listing_quantity`). This
  is the whole of the double-normalization fix: the old rule reached for a parsed package
  size first and fell back to one pound only when no size could be found, which is silent for
  a retailer that keeps the weight out of the title and catastrophic for one that puts it in.
  Target publishes "Boneless & Skinless Chicken Breast Value Pack - 2.5-5.25lbs - price per
  lb" with `current_retail: 2.59`, so 5.25 lb came off the title and divided a rate that had
  already accounted for it -- $2.59/lb was published as $0.49/lb, five times too cheap and
  therefore ranked first. Every retailer shared the bug; Target is only the one whose titles
  made it fire. Kroger was one size string away (it alone kept `size` on a weight item, and
  no longer does).
  **A variable-weight item's facts are carried, never computed.** `weight_range` is the span
  the retailer published and is kept beside the price because a tray with no single weight has
  no single size -- collapsing it to one end is exactly the number that used to be divided in.
  `max_total_price` is copied from the retailer (`formatted_max_item_price`) or absent:
  Target's own ceiling for that 2.5-5.25 lb tray at $2.59/lb is **$12.95**, not the $13.60
  multiplying gives, so a derived figure would be wrong in an authoritative tone. `size_text`
  is `None` for every per-unit price, in every adapter, so nothing downstream has a package
  size to divide by. `migrations`: `c3a1d0e7f482` repairs the *sizes* written before the fix
  -- a canonical product never re-derives its size, so those never heal on their own -- and
  deliberately leaves offers alone, because nothing on an offer row distinguishes a
  double-divided rate from a correctly divided package total.
  **A weighed product never merges with a packaged one** either: every per-unit price now
  normalizes against one pound, so size alone stopped telling a tray apart from a 16 oz
  pack of the same brand, and `matching/deterministic.py` compares the basis as well.


## Matching, units and identifiers

- **Deterministic matching only.** `matching/deterministic.py`: exact GTIN -> auto; same
  package + same brand + high title similarity -> auto; ambiguous -> `unresolved` (no merge);
  low -> `new`. Different package sizes are always distinct canonical products; comparison
  across sizes happens on unit price. Never silently merge materially different products.
- **AI is disabled by default and off the request path.** `AI_JUDGE_ENABLED=false`;
  `AIProductJudge` is an interface only. No AI for arithmetic, conversion, ranking or baskets.
- **Arithmetic is Decimal** (`normalize/units.py`), quantized to cents / 4 dp for unit prices.
- **Shelf price is the comparison price.** Loyalty/card prices (Kroger promo) go in
  `loyalty_price` so retailers compare like-for-like.
- **GTINs are normalised to 14 digits at ingest** (`normalize/gtin.py`; PLU codes dropped) so
  Kroger's 13-digit UPCs match Smart & Final's GTIN-14s and Sprouts'/99 Ranch's UPCs.

## Price history

### Price history
`price_history` is append-only and never rewritten. A row is written by
`services/scraper.record_price_history` **only when a scrape produces a price that differs
from the newest row for the same (retailer product, store)** -- that pair is the identity of
a series, and nothing merges two of them: two branches of one chain price differently, and a
line averaging them shows a price nobody was charged.

- **The change test covers what is plotted**: price, regular price, loyalty price,
  `unit_price` and `price_basis`. The last two are why a pack that shrinks from 16 oz to 12 oz
  at the same $3.99, and a retailer that switches a product from a package total to a rate per
  pound, are recorded; on the older three-column test neither wrote anything at all.
- **A cached or repeated scrape writes nothing**, because a reused capture replays the prices
  it was captured with and they match. `offers.scraped_at` is the column that means "confirmed
  again just now"; a history row means "changed then".
- **The change test compares what the columns will hold, not what is in memory.**
  `price_snapshot` quantizes every field first. `price` is `Numeric(10, 2)`, so a retailer
  publishing `4.999` is read back as `5.00` while the in-memory value is still `4.999`; the
  two never compared equal and a byte-identical payload wrote a row on every scrape. Kroger
  and Whole Foods both build prices straight from their payloads without rounding.
- **A product listed again after being delisted records a row even at the same price.**
  History outlives the offer it described, so the old row would otherwise match and the chart
  would draw one unbroken line across months the product was not sold.
- **A row carries its own `price_basis` and `unit_price_unit`** (migration `d2b8c1a5e307`).
  They are copied, not joined to: the offer that produced a row is deleted when the retailer
  delists the product and is overwritten with *today's* basis while it survives, so a history
  row has to be self-describing. **`price_history.price_basis` is nullable and does not
  default to `package`,** unlike `offers.price_basis`. On an offer "nobody said" really is a
  package total -- the adapter was there to ask. On a row backfilled years later there was
  nobody to ask, and a client prints `package` as "for the pack", which over a per-pound rate
  is the one sentence `8fc71eae3f69` exists to stop. NULL says "not recorded"; a guess is not
  recoverable once written.
- **`scraped_at` is never rewritten** -- not by a refresh and not by a migration. History is
  a record of what was collected and when; editing it to match today's understanding is how a
  record stops being evidence.
- `services/price_history.py` serves `GET /products/{id}/price-history?days=30` in four
  queries whatever the number of stores. Because a series is a list of *changes*, a window
  query alone is not enough -- the newest observation *before* the window comes back flagged
  `before_window`, or a price that last moved forty days ago would render as "no history". The
  right-hand end of a line is the live offer (`current`), which is null for a delisted product
  so its last price is not drawn forward to today.

