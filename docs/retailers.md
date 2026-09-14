# Retailer integrations and acquisition policy

Which retailers are read, by what method, and what each one does and does not publish. This
file records the acquisition policy in full, including the cases where the project's behaviour
differs from a site's blanket `robots.txt` directive.


Acquisition order is official API > the site's own JSON endpoints > embedded page data >
store locator / sitemaps > browser automation. Nothing bypasses authentication, CAPTCHAs or
bot protection, and hosts whose `robots.txt` disallows the endpoint are not scraped -- with
one deliberate, documented exception, below.

**Decided exception: the three Instacart white-label storefronts.** `shop.sprouts.com`,
`shop.luckysupermarkets.com` and `shop.savemart.com` allow ~20 named crawlers and then end
their `robots.txt` with a catch-all `User-Agent: *` / `Disallow: /`, which a generic client
matches on every path (verified 2026-09-12). **Sprouts, Lucky Supermarkets and Save Mart stay
enabled anyway** -- a deliberate project decision, taken with that finding in hand, not an
oversight. Their acquisition is unchanged, and this is not an open item: it is revisited only
if the behaviour materially changes (a block, a rate limit, a notice addressed to the
project, or a robots.txt change beyond the catch-all). Sprouts' *hours* are unaffected either
way -- they come from `www.sprouts.com`, a separate host with its own permissive robots.txt.
Every other retailer follows the rule as written.

| Retailer | Method | Store lookup | Prices | Notes |
|---|---|---|---|---|
| Whole Foods | `/api/search` JSON | vendored directory + ZIP centroid | per store, sale | no UPC |
| Smart & Final | Mi9 gateway JSON | `/api/stores` + ZIP centroid | per store, TPR sale, stock | GTIN-14 SKUs; loyalty prices not exposed |
| Trader Joe's | site GraphQL | where2getit locator (ZIP, nearest first) | national, per-store assortment | no UPC, no sale prices; bananas priced each |
| Sprouts | storefront GraphQL + guest cookie | `idp/v1/shops` by ZIP | per shop (in-store = pickup = delivery) | persisted-query hashes rotate on deploy |
| 99 Ranch | `be-api` JSON | nearby-stores by ZIP | per store, sale, stock qty | online-order prices; Asian assortment |
| Kroger (Ralphs, Foods Co, ...) | official API | API | per store, loyalty | credentials required |
| Safeway | `xapi` JSON (public web key) | `storeresolver/all` by ZIP, nearest first | per store, sale, stock | keyword search is Imperva-blocked, so categories start from vendored shelf seeds; no brand field, no loyalty price |
| Lucky Supermarkets | Instacart storefront GraphQL + guest cookie | `DefaultShop` by ZIP + centroid | per shop, sale | package size on every item; no UPC, no store address |
| Save Mart | Instacart storefront GraphQL + guest cookie | `DefaultShop` by ZIP + centroid | per shop, sale | same as Lucky; returns nothing where the banner has no store |
| Raley's / Bel Air / Nob Hill | `/product/*` server-rendered `__NEXT_DATA__` | vendored directory (stores sitemap, addresses resolved by the Census geocoder) | per store via the site's own store cookie, verified against the page's `currentStoreNumber`, sale | GTIN-14 + brand + size; one request per product, so categories are capped |
| Target | not used | – | – | PerimeterX. In persistent headed Chrome the page shell renders and every `redsky.target.com` data call comes back a captcha block, so the page looks healthy and carries nothing; redsky's `robots.txt` also disallows all |
| Walmart | not used | – | – | PerimeterX. `/store/finder` answers; `/ip/` product pages answer until repeated automated visits trip a `/blocked` "Robot or human?" interstitial; `/browse/` category pages challenge immediately. `robots.txt` disallows `/search` and `/api/` |
| Costco | blocked | – | – | Akamai 403 on product pages, and headed Chrome cannot connect at all; warehouse grocery prices are not published online |
| Grocery Outlet | stores only | WordPress ajax | none online | verified: no product/category sitemap and no shop surface exists; the circular is a third-party Flipp iframe |
| Amazon Fresh, Mollie Stone's, Berkeley Bowl, Gus's | not used | – | – | browser-only/ToS-hostile, Instacart-only, or no grocery catalogue online |

Store ranking for retailers without a ZIP-aware endpoint uses the vendored Census ZCTA
centroids (`app/retailers/data/zip_centroids.csv`) and great-circle distance, falling back to
the ZIP-prefix heuristic when coordinates are missing. GTINs are normalised to 14 digits at
ingest so barcodes from different retailers match.



ZIP-to-store lookup for Whole Foods uses a vendored directory
(`app/retailers/wholefoods/stores.json`) built from the public store summary endpoint. Each
record also carries `folder`, the slug that store's own page lives at, resolved from the
published stores sitemap by reading each page's `storeCode` — the summary endpoint's own
three-letter `folder` 404s, and a display name matches the published slug for only four
stores in five. The folder is what lets a scrape read the store's address, phone, timezone
and opening hours (518 of 555 stores have one). Refresh it with:

```bash
uv run python scripts/discover_wholefoods_stores.py                  # full rebuild
uv run python scripts/discover_wholefoods_stores.py --folders-only   # just the page slugs
```

