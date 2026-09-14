# Pricing model

What a price is a price *of*, and why the basis is never re-derived.


A number on a shelf label is not a price until you know what it buys. Every offer carries a
`price_basis` beside its amount — `package`, `lb`, `oz` or `each` — taken from the retailer's
own statement (Target's `formatted_unit_price_suffix`, Kroger's `soldBy`, Safeway's
`sellByWeight`), and **a per-unit price buys one unit of its own basis**. Nothing divides it
by a package size again.

That rule is a bug fix. Target sells `A-86676070` at **$2.59 per pound** in trays weighing
2.5–5.25 lb, and publishes `current_retail: 2.59` under the title "Fresh All Natural Boneless
& Skinless Chicken Breast Value Pack - 2.5-5.25lbs - price per lb". The old code read the
5.25 off the title and divided the rate by it: $2.59/lb was published as **$0.49/lb**, five
times too cheap, and the cheapest-per-pound ranking put it first. Every retailer shared the
defect in `listing_quantity`; Target was simply the one whose titles made it fire.

A variable-weight item's other facts are carried, never computed:

- `min_weight` / `max_weight` / `weight_unit` — the span the retailer published. A tray with
  no single weight has no single size, and its upper end is exactly the number that used to
  be divided in.
- `max_total_price` — copied from the retailer (`formatted_max_item_price`) or absent.
  Target's own ceiling for that tray is **$12.95**, which is $2.59 × 5.00, not the $13.60
  that $2.59 × 5.25 lb would give. A derived figure would be wrong in an authoritative tone.

The UI follows: a fixed package reads "$0.42 / egg" over "$4.99"; a variable-weight tray
reads "$2.59 / lb" over "up to $12.95" and "2.5–5.25 lb · final price based on weight", and
never "$2.59 for the pack".

Rows written before the fix do not heal on their own — a canonical product keeps its
SKU mapping deliberately, so it never re-derives its size — and migration `c3a1d0e7f482`
repairs those sizes. It deliberately leaves `offers` alone: nothing on an offer row says
which basis it was priced on, and the tempting test ("its unit price equals price ÷ the old
comparison quantity, so it was double-divided") is satisfied by every correctly computed
package offer as well. Offers carry their basis from the adapter and the next scrape of
their store and category rewrites them.

