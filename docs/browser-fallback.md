# Browser fallback

The last step of the acquisition chain, **optional and off by default**. No registered adapter
needs it. It never defeats a challenge: when a retailer asks for a human, a human answers.


`app/retailers/browser.py` is the last step of the acquisition chain — official API, then
site JSON/XHR, then embedded page data, then locator/sitemaps, then this.

It connects in one of two ways, and prefers the first:

| mode | what it is |
| --- | --- |
| `existing_chrome_cdp` | attaches over the DevTools protocol to a Chrome **already running**, and uses the session that browser already has — its cookies, its logins, its chosen store. Nothing is copied or synthesised; it opens a tab in a browser that is already there. |
| `persistent_system_chrome` | the fallback: launches the system's own Chrome on a profile directory that persists between runs. |

Attaching needs Chrome started with `--remote-debugging-port`, and since Chrome 136 that flag
is refused on the *default* profile directory — a deliberate restriction so a page cannot talk
a browser into exposing its own cookies, and not one to work around. So attaching means a
profile of its own, which `scripts/start_chrome_cdp.py` opens for you:

```bash
uv run python scripts/start_chrome_cdp.py      # starts Chrome with the port open
uv run --extra browser python scripts/probe_browser_retailer.py --wait
```

Sign in and pick your stores in that window once; it is a normal Chrome profile and it is
still there next run. Arriving as a brand-new visitor every scrape is what provokes challenges
in the first place.

**The two modes are two profiles, and that is the thing to watch.** An attached Chrome shops in
the directory it was started on (`BROWSER_CDP_PROFILE_DIR`); the fallback launches
`BROWSER_PROFILE_DIR`. Different cookies, different chosen store, different history. Whether a
run attaches depends only on whether that Chrome happens to be listening, so the profile a run
used is reported rather than assumed: `BrowserSession.diagnostics()` gives the profile **in
use** beside both configured paths, and a run that wanted to attach and could not says
`browser_profile_identity_changed` in the log, naming both. Start the CDP Chrome first if you
want the session you verified in.

**What actually survives a browser restart**, measured on a real Target profile: the persistent
cookies (PerimeterX's `_px3`/`_pxvid`, `refreshToken`, `UserLocation`), the whole of local
storage, IndexedDB and the registered service worker. What does not: every session-scoped
cookie, which for Target includes `accessToken`/`idToken` and **`sddStore`, the selected
store** -- so a restarted browser re-picks a store from the location it still remembers, and two
runs minutes apart legitimately reported two different nearby stores. Nothing here forges a
cookie to paper over that (that would be the disguise this layer refuses); the store each price
came from is read from the session and recorded, and a store too far from the ZIP is refused
rather than priced. The practical consequence is to keep one browser alive rather than restart
it per scrape, which is what `RetailerClients` already does: one session per process, closed
with the application.

**A page load is the expensive request, and it is not spent twice.** A load's own data calls are
kept for `BROWSER_CAPTURE_TTL_SECONDS` (120s) and handed to the next caller that asks for the
same page and is satisfied by what was captured. When it is *not* satisfied -- the store read
stops as soon as the store answers, so its capture has no shelf in it -- the page it came from
is still open on that URL and is read **further** rather than loaded again, which is why the
first category costs one load instead of two and a seven-category scrape is seven page loads,
not eight. A page that has since navigated, by us or by the site, is out of that path. That window is deliberately
short and is **not** the answer to "ask Target less often": ingest stamps every offer with the
moment it was written, so reuse spanning two scrapes would publish prices the second one never
fetched as prices read just now. How often Target is asked at all is set by
`SEARCH_FRESHNESS_TTL_SECONDS` and `SEARCH_REFRESH_COOLDOWN_SECONDS`, where a longer cache is an
operator's decision about price age rather than a browser-layer side effect. The Target adapter declares `max_concurrent_searches = 1` (an optional member documented on
`RetailerAdapter.search_products`): its categories share one page, so the scrape runs them one at
a time instead of queueing seven tasks on one lock where six spend the retailer's deadline
waiting for a page they never reach. Walmart has the same one-page constraint and does not
declare it yet. And a retailer parked on a challenge answers
from its standing verdict for `BROWSER_CHALLENGE_RETRY_INTERVAL_SECONDS` instead of loading the
challenge again -- reloading one teaches nothing and is the surest way to make it worse.

**A load waits for the page's own data, not for a stretch of clock.** `settle_ms` is a ceiling.
Inside it the loading page's first-party responses are watched, and the wait ends as soon as
they answer what the caller asked or stop arriving; a category whose shelf answers in a second
costs a second rather than the eight it used to. A page that has fetched *nothing* still waits
the whole ceiling, deliberately: fetching nothing is what a challenge looks like from here, and
calling one early would trade a wasted wait for a wrong verdict. A main document the origin
refuses outright -- 403, which is PerimeterX's answer, or 429 -- parks the retailer too, because
Playwright hands those back as an ordinary response rather than raising, and a page that says
nothing recognisable would otherwise be scraped as though it were a shelf. A 404 is not a
challenge: that is a category path that has moved. `BrowserSession.diagnostics()` reports
`page_loads` against `pages_reused_from_capture`, `pages_continued`, `data_responses` and
`challenges`, and `scripts/probe_browser_retailer.py` prints them.

**Nothing disguises the browser.** No stealth plugin, no `--disable-blink-features`, no
user-agent override, no `navigator.webdriver` patch, no canvas/WebGL/TLS tampering, no proxy
rotation, no CAPTCHA solver. `tests/test_browser_fallback.py` asserts their absence against
the source rather than trusting it. When a retailer decides a visitor should be confirmed, the
answer is a person confirming it. (Attaching does mean `navigator.webdriver` reads `false` —
not because anything patched it, but because the browser was not launched by automation.)

It does not defeat challenges and must never be made to — no captcha solving, no fingerprint
spoofing, no proxy rotation. When a retailer asks for a human it raises
`ManualVerificationRequiredError`, leaves the window open on the challenge, and says which
retailer is waiting. Detection reads the rendered page *and* the first-party data responses,
because Target blocks only the latter. Each retailer gets its own page, so one checkpoint
never stops the others.

**The wait is passive.** A press-and-hold is a gesture held for seconds and a CAPTCHA is a
puzzle to read; reloading the tab underneath either one throws the attempt away. So for
`BROWSER_MANUAL_VERIFICATION_TIMEOUT_SECONDS` (ten minutes by default) nothing navigates,
reloads or clicks — it watches two things that cost nothing: the page's own text, which stops
saying "Robot or human?" once answered, and the page's own navigations, because a solved
challenge shows up as the site handing the real page back by itself. Only after the window
expires, and only for a challenge that hid in the data calls rather than on the page, is the
page re-asked once. The window's opening and expiry are logged, with the time remaining
reported every `BROWSER_MANUAL_VERIFICATION_REPORT_SECONDS`.

```bash
uv sync --extra browser
uv run --extra browser python scripts/probe_browser_retailer.py --wait
```


## What Target and Walmart actually yield

Both adapters exist and are tested against real captured payloads
(`app/retailers/target/`, `app/retailers/walmart/`). **Neither collects prices in a default
run.** Target is registered in `_ADAPTER_CLASSES`, but its `is_configured()` returns False
unless `BROWSER_FALLBACK_ENABLED` is set and the `browser` extra is installed, so a scrape
skips it exactly as it skips Kroger without credentials. Walmart is not registered at all.
The reason for both is a measurement: over ten live runs against a human-verified session, Target was
stopped by a PerimeterX challenge twice — once inside an ordinary scrape. Everything it reads
is right and repeatable when it gets through; one run in five wanting somebody at a keyboard
is simply not a scheduled job. Walmart is further off again: on the same session it failed to
identify its own store once and was challenged the next run.

| | price | canonical URL | store | store-specific stock |
| --- | --- | --- | --- | --- |
| **Target** | ✅ `plp_search_v2` | ✅ its own `enrichment.buy_url` | ✅ `store_location_v1` | ✅ `store_options[]` per `location_id`, cross-checked against the store's own count |
| **Walmart** | ✅ nested `priceDetails.priceLines` | ✅ its own `canonicalUrl` | ✅ `fulfillmentSummary[].storeId` | ⚠️ partial — see below |

Two fields on these sites look like stock and are not. Target's
`shipping_options.availability_status` read `OUT_OF_STOCK` for every product on a captured
shelf, including ones with ten in the store being priced; it is whether Target will post the
item. And Walmart's browse pages report `IN_STOCK` for very nearly everything on them, because
they do not list what they have not got — so "it is on the shelf page" and "it is in stock"
are nearly the same statement. Walmart's adapter therefore requires the status, `canAddToCart`
(which really does vary — it was false for five marketplace rows out of 41) and the store id
to agree, and **never reports `out_of_stock` at all**, because a browse page omits absence
rather than marking it.

