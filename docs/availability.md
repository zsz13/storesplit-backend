# Availability semantics

Three states, normalized at ingest from whatever each retailer publishes. **A missing signal
is `unknown`, never `in_stock`** — guessing is what puts an unbuyable item at the top of a
price comparison.


Every offer carries one of three states, normalized at ingest from whatever the retailer
publishes and stored on `offers.availability`; the retailer's own wording is kept beside it in
`offers.stock_status` so a surprising state can be traced back to the payload.

| State | Meaning |
| --- | --- |
| `in_stock` | The retailer says this store has it |
| `out_of_stock` | The retailer says this store does not |
| `unknown` | The retailer published nothing trustworthy for this store |

**A missing signal is `unknown`, never `in_stock`** — guessing is what puts an unbuyable item
at the top of a price comparison. Search, the basket and the UI default to `in_stock` only.

What each retailer actually publishes:

| Retailer | Source | Can report |
| --- | --- | --- |
| Lucky, Save Mart, Sprouts | Instacart `availability {available, viewSection.stockLevelLabelString}` — the sentence the product page shows; `stockLevel` alone goes stale | all three |
| 99 Ranch | per-store quantity (`available`) | all three |
| Smart & Final | `attributes["Stock Status"]` / `available` | all three |
| Safeway | `inventoryAvailable` | all three |
| Kroger | `items[].inventory.stockLevel` (HIGH and LOW both sell) | all three |
| Trader Joe's | nothing usable — `availability` is `"1"` for the whole catalogue | `unknown` only |
| Whole Foods | `/api/wwos/products?asins=` — the availability its product page renders from, read twice for a negative | `in_stock` / `out_of_stock` / `unknown` |
| Raley's | commercetools `discontinued`; `inventoryMode` is `"None"`, so stock is not tracked | `out_of_stock` / `unknown` |


## `unknown` is hidden from the comparison, not from the product

Trader Joe's publishes no stock anywhere, so every offer it has is `unknown` — and the default
in-stock filter removed it from the product completely, prices and links included. Right
filter, wrong outcome. The search response now carries a second list, `unknown_products`: the
same query over offers whose retailer never published stock, returned *beside* the confirmed
results rather than among them, capped at a dozen, and rendered under an "Availability
unknown" heading that says plainly why. Those offers carry no "Best for this product" badge,
never win `cheapest_offer_id`, and never build a basket. Switching the filter to **Unknown**
or **All** returns them normally.

Whole Foods' `/api/product?store=`.isAvailable looks like stock but is *carriage* — true for
everything a store lists — so it is not used; see the adapter docstring. Its real signal,
`/api/wwos/products`, is the record its product page renders from, and it states a negative
by omission: no availability *and* no `offerDetails` is what the page prints as "Out of
Stock" (for ASIN `B0014GPSKQ` at SoMa, "Currently not sold in SoMa"). That omission flaps —
13 of 86 such negatives reported a live offer again on a later read, at every batch size
including one ASIN per call — so **a negative is only believed when a second, separately
batched read agrees**, and one nobody could confirm stays `unknown`. A positive is
`IN_STOCK` or a live `offerDetails.offerListingId`, never a price on its own. Availability
costs one page fetch per store plus one batched call per 50 items, plus one more per 50
negatives; no product page is ever fetched per product. Trader Joe's
`availability` is the same trap and was making the same mistake: it is `"1"` on all 344
products an unfiltered search returns, and `availability: {match: "0"}` matches nothing at
all, so it says "published", not "on the shelf". Trader Joe's sells nothing online and
publishes no per-store inventory, so **every Trader Joe's offer is `unknown`** and none
appears in the default in-stock-only view. That is a real loss of coverage, and it is the
honest reading: the alternative was calling a constant a stock signal.

The storefront banners spell their hedge `stockLevel: "lowStock"`, which the product page
renders as **"Likely out of stock"**. Reading the token as "low but buyable" is what showed
Lucky product `19830961` as in stock while its own page said otherwise, so the label decides
and `lowStock` is `unknown`. Measured live across all seven categories, this moved 8 of 1260
Lucky items and 8 of 840 Save Mart items out of `in_stock`, and nothing in the other
direction; Sprouts emitted no `lowStock` at all.

**After upgrading:** the migration that adds the column sets every existing offer to
`unknown`, because nothing recorded what those scrapes saw. Until the next scrape, the
default in-stock view will therefore be empty; run `POST /scrape` (or
`scripts/scrape.py`) for the ZIPs you care about.

