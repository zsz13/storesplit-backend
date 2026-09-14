---
paths:
  - "app/normalize/availability.py"
  - "app/retailers/**"
  - "app/services/search.py"
  - "app/services/basket.py"
  - "app/api/products.py"
  - "tests/test_availability*.py"
  - "tests/test_*availability*.py"
---

# Availability semantics

The three states, how each retailer's signal maps onto them, and why a missing signal is
never `in_stock`. Root `CLAUDE.md` carries the one-line invariant; this is the whole rule.

- **Availability is first class, and never guessed.** `normalize/availability.py` defines the
  only three states -- `in_stock`, `out_of_stock`, `unknown`. Adapters set
  `ProductListing.availability` from the retailer's own field and keep its raw wording in
  `stock_status`. **A missing or unreadable signal is `unknown`, never `in_stock`**, and a
  field that cannot vary is not a signal: Whole Foods' `isAvailable` is true for every item a
  store lists, and Trader Joe's `availability` is `"1"` for the entire catalogue (344 of 344
  unfiltered; `availability: {match: "0"}` matches nothing), so both report carriage rather
  than stock and neither is used. Offers store the normalized state; search and baskets
  default to in-stock only and rank buyable offers first, so nothing unbuyable is ever badged
  "cheapest" -- the badge and `best_offer_id` go only to an `in_stock` leader, in every filter.
  **`unknown` is filtered out of the comparison, not out of the product.** A retailer that
  publishes no stock at all (Trader Joe's sells nothing online; Raley's `inventoryMode` is
  `"None"`) would otherwise vanish from StoreSplit entirely, prices and links included. The
  default search returns those products separately in `unknown_products`, capped and rendered
  under their own "Availability unknown" heading -- never mixed with confirmed offers, never
  badged, never in a basket. An explicit `unknown` or `all` filter returns them normally.
  **Why an offer is `unknown` is a fact about the retailer, and it is published.** Every
  adapter declares `stock_reporting` (`app/normalize/availability.py`), required by the
  Protocol rather than defaulted, because a default answers the question for an author who
  never considered it: `live` means the retailer states per-store inventory, so an `unknown`
  offer is a reading that failed; `not_published` means it states none anywhere, so
  `unknown` is the only state its offers can have. Only Trader Joe's and Raley's are
  `not_published`, and `tests/test_adapter_contract.py` pins that by name and asserts such
  an adapter never produces an `in_stock` listing. `retailers/__init__.py::stock_reporting`
  reads it off the class the way `product_hosts` does, and `StoreOut.stock_reporting`
  carries it to the client -- an unregistered slug reports `live`, so a missing adapter can
  never earn a retailer the "they do not publish it" excuse. **It changes no ranking**: the
  badge, `best_offer_id` and baskets stay `in_stock`-only either way. It exists so a client
  can stop saying "Stock unknown" over a Trader Joe's price, which reads as a fault of
  StoreSplit's when the honest statement is that the retailer publishes no shelf stock and
  the price, the store and the product page are all real.
  **Being listed or priced is not being in stock, and a hedge is not a signal.** A search
  result is `listed`; a price is `priced`; neither promotes an offer to `in_stock`. Words
  like `lowStock` / `limited` carry no portable meaning, so `normalize_availability` resolves
  them to `unknown` and **each retailer maps its own levels in its own adapter**: Kroger's
  documented `LOW` in `kroger/adapter.py`, Smart & Final's `plenty`/`low`/`out` in
  `smartandfinal/adapter.py` (its `low` really is low-but-on-the-shelf -- it keeps a separate
  `out`, and an item its search calls `low` comes back `high` from its own product endpoint),
  and the Instacart storefront banners (Sprouts, Lucky, Save Mart) in
  `retailers/instacart_storefront.py`, where `lowStock` is the shopper-visible **"Likely out
  of stock"** and so is never `in_stock`. A level no adapter has mapped never reaches
  `in_stock` on its own. When two first-party fields
  disagree, the one the product page itself displays wins, and anything still unclear is
  `unknown`: reading the token instead of the label is what showed Lucky product `19830961`
  as buyable while its own page said otherwise.
  **99 Ranch's `available` is a per-store count, zero included, and its product page is
  stock-blind.** `available: 0` is the retailer's own sold-out state, not a missing field:
  the site's own product tile computes `E = (0 === t.available)` and renders `E &&
  <SoldOutMask/>` -- shopper-visible "Sold Out / In stock soon" with the title greyed. Live
  check: 467 rendered tiles over ZIPs 94404 and 94014, the 13 with `available: 0` were the
  only 13 masked, zero mismatches. It is a count and not a flag (81-100 distinct positive
  values per store over 335 listings) and it is per store (Clover Whole Milk 20 / 0 / 39 at
  Foster City / Daly City / Richmond in one read). **`/product-details/...` never renders a
  negative for anything**, so its silence is not evidence: its Add to Cart gates on
  `variantId` alone (`n ? addCart(n,p) : warn("product_not_available")`) and nothing there
  reads `available` -- products 2001373 and 2075786, masked "Sold Out" in the grid, open with
  an enabled Add to Cart. So a report that "the 99 Ranch page does not say out of stock" says
  nothing about that product; check the grid, or `available`. The states mirror the
  retailer's own rule exactly: `> 0` is stock, **a stated zero** is its out-of-stock, and
  anything else -- absent, non-numeric, or negative, which its own tile would still render as
  buyable -- is `unknown`. `available <= 0` would be StoreSplit inventing a negative on a
  shape nobody has proved.
  **Whole Foods publishes a positive and nothing else, and its page's "Out of Stock" is an
  inference rather than a report.** Its `/api/wwos/products` record is what the product page
  renders from, and `availability` there is `"IN_STOCK"` or `null` -- no negative word has
  appeared in ~400 observations, nor in a full browser session with a store chosen. What the
  page prints over the null ("Out of Stock", "Currently not sold in Stonestown") is the page
  drawing a conclusion from the same absent answer, and it is not a stable one: `B07YFT8JTH`
  rendered "$9.99/lb, Pickup from Stonestown, Add to Cart" and "Currently not sold in
  Stonestown / Out of Stock" minutes apart at one store. **A record with no stated
  availability and no `offerDetails` is therefore `unknown`, at any number of reads.**
  `resolve_availability` takes a positive from either read (`IN_STOCK`, or a live
  `offerDetails.offerListingId` -- a price alone is never enough), takes a *stated* negative
  at once, and resolves everything else to `unknown`. **The stated word is read before the
  offer listing**, so a record carrying both `OUT_OF_STOCK` and a live `offerListingId` is
  `out_of_stock`: that is the precedence every other retailer already gets
  (`availability_from_flag`), and it matters most here, because a stated negative is the only
  route Whole Foods has left to `out_of_stock`. The second, separately batched read of the
  empty records survives only to *find* an offer the first read missed; it may never deny
  one, so a re-read batch that fails costs only its own ASINs -- the batches that answered
  are kept, and a rescued positive is stamped `on reread` so what the extra request buys
  stays measurable from the stored data. One extra request per 50, and no product page fetched per product.
  StoreSplit used to assert that empty shape as `out_of_stock` once a second read agreed,
  which made 346 of 869 Whole Foods offers wrong: measured at store 10717 over 12 reads of
  30 ASINs, 14 answered with a live offer on some reads and with nothing on others (one on 1
  read of 12, another on 11 of 12), 13 never positive, 3 always -- at the same rate whether
  the batch held 30 ASINs or 1, and whether reads were 1.5s or 15s apart. Two empty reads of
  an item whose offer appears on a fraction `p` of reads agree with probability `(1 - p)^2`,
  85% at p=0.08, so agreement measured the endpoint and not the shelf. Retrying cannot fix
  it either: even ten reads leave a p=0.08 item wrong 43% of the time. A negative here is
  not provable, so it is not asserted.
