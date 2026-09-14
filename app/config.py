"""Application settings loaded from environment variables (and a local .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://storesplit:storesplit@localhost:5432/storesplit"
    log_level: str = "INFO"
    # Every loopback spelling of the dev frontend's one port. A CORS origin is matched as an
    # exact string, and the dev server answers on 0.0.0.0, so a page opened at 127.0.0.1:3000
    # is a different origin than the same page at localhost:3000 and would be refused.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://[::1]:3000"

    # -- HTTP ------------------------------------------------------------------------
    http_timeout_seconds: float = 15.0
    http_connect_timeout_seconds: float = 10.0
    http_max_retries: int = 3
    # Pool limits for the long-lived AsyncClients. Keep-alive connections are what make the
    # concurrent scrape cheap: a retailer's requests reuse one TCP+TLS handshake.
    http_max_connections: int = Field(default=100, ge=1)
    http_max_keepalive_connections: int = Field(default=20, ge=1)
    http_keepalive_expiry_seconds: float = 30.0

    # -- Scrape concurrency ----------------------------------------------------------
    # Two runs over one ZIP would fight over offer expiry, each deleting what the other has
    # not confirmed yet, so runs are serialized by default.
    scrape_max_concurrent_runs: int = Field(default=1, ge=1, le=8)
    # Retailers run concurrently; inside one retailer its (store, category) requests do too.
    # Ten covers every registered adapter, so no retailer waits for a slot before starting.
    scrape_max_concurrent_retailers: int = Field(default=10, ge=1, le=32)
    # One budget for a whole retailer, shared with any fan-out an adapter does inside a single
    # search, so this is the real number of requests in flight, not a per-level allowance.
    # Measured over a full 94105 fetch, all retailers, zero errors at every step:
    #   4 -> 19.8s (the old default)      6 -> 15.2s      8 -> 12.9s      12 -> 10.1s
    # Eight is where it settles: the gain from 8 to 12 is under three seconds, and 12 would
    # point ~55 requests a second of full page loads at Raley's, the one retailer whose
    # catalogue needs hundreds of them. Raise it only with a fresh measurement.
    scrape_max_concurrent_requests_per_retailer: int = Field(default=8, ge=1, le=32)
    # A retailer that exceeds its own deadline is recorded as failed; the others keep going.
    scrape_retailer_timeout_seconds: float = Field(default=240.0, gt=0)
    # Hard ceiling for a whole run. Retailers still running when it fires are marked failed.
    scrape_deadline_seconds: float = Field(default=900.0, gt=0)

    # -- Search freshness ------------------------------------------------------------
    # How old collected prices may be before a search treats them as stale. Stale is not
    # hidden: the search answers from what it has and revalidates behind the answer, so this
    # is "when to go and look again", never "when to stop showing a price".
    search_freshness_ttl_seconds: int = Field(default=1800, ge=0)
    # Whether a stale search may start that background refresh by itself.
    search_auto_refresh: bool = True
    # The minimum spacing between refresh *starts* for one (ZIP, category) key, and the one
    # cooldown in the system: automatic and manual refreshes share it. It is what stops a
    # failing key being retried on every keystroke, and what a manual refresh button counts
    # down from. Enforced in the API, not in the browser, so reloading or opening a second
    # tab cannot shorten it.
    search_refresh_cooldown_seconds: int = Field(default=300, ge=0)
    # A ceiling on refreshes in flight at once, across every key.
    #
    # The per-key cooldown alone is not a budget: the key is chosen by the caller, so a
    # thousand distinct ZIPs are a thousand uncooled keys, and every one of them would queue
    # a full collection from ten real retailers. `/products/refresh` and the automatic path
    # are both unauthenticated by design (this is a local MVP), so the thing that has to be
    # bounded is the total, not the repeat rate. Beyond this, a refresh is refused rather
    # than queued -- the caller gets `cooling_down` and the retailers get nothing.
    search_max_concurrent_refreshes: int = Field(default=2, ge=1, le=16)
    # How many keys the registry remembers. Only there to keep an adversarially varied key
    # space from growing the dict without bound; the oldest idle entries are dropped, and
    # dropping one costs at most a cooldown that `scrape_runs` can still supply.
    search_refresh_registry_max_keys: int = Field(default=512, ge=16)

    # -- Database --------------------------------------------------------------------
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)

    kroger_client_id: str = ""
    kroger_client_secret: str = ""

    # Google Places, used only to identify the *business* at a store's published address
    # so its map link opens the shop rather than the street. Empty by default, and then
    # the feature is simply off: Target and Safeway publish their own Google listings, and
    # every other store falls back to an address search naming the retailer. A resolved
    # place is accepted only when its name carries the brand and its address matches, and
    # it is looked up once a week per store during the store-details pass -- never while a
    # search or a product page is being answered.
    google_maps_api_key: str = ""

    # The AI judge is a future-facing hook. It stays off unless explicitly enabled and
    # is never part of the request path.
    ai_judge_enabled: bool = False

    # Maximum stores per retailer that a scrape run collects for one ZIP code. Search caps
    # each retailer at the same number: offering more stores than a scrape ever visits would
    # show a shopper stores with no prices in them.
    scrape_stores_per_retailer: int = Field(default=2, ge=1, le=10)

    # How far from a ZIP's centroid a store may be and still be that ZIP's store. It decides
    # what a shopper sees: a ZIP with no store of a retailer inside the radius shows that
    # retailer nothing, deliberately, rather than a store across the bay.
    search_store_radius_miles: float = Field(default=30.0, gt=0, le=100)

    # How long a store's published details -- address, timezone, opening hours -- are
    # kept before a scrape re-reads them. They are a store's standing facts, not its prices:
    # re-reading them per scrape would be a request per store every five minutes for nothing.
    # **0 switches the feature off** rather than meaning "no cache": an operator reaching for
    # zero wants fewer requests, and the surprising reading of it is the expensive one.
    store_details_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=0)

    # -- Browser fallback ------------------------------------------------------------
    # The last acquisition step, for retailers that answer nothing else (Target, Walmart).
    # Off by default: no ordinary scrape should depend on a browser being installed, and a
    # headed window is not something to open on somebody without asking. See
    # `app/retailers/browser.py` for what it does and, more importantly, what it will not.
    browser_fallback_enabled: bool = False
    # Preferred: attach to a Chrome that is already running, over the DevTools protocol, so
    # the session used is the real one -- its cookies, its logins, its chosen store -- rather
    # than a copy. Nothing is injected or impersonated; this only opens a tab in a browser
    # that is already there. Chrome exposes the port only if it was started with
    # `--remote-debugging-port`, so when it is not listening this falls back quietly.
    browser_attach_cdp: bool = True
    browser_cdp_endpoint: str = "http://127.0.0.1:9222"
    # How long to wait on the attach before giving up and launching the fallback profile.
    browser_cdp_timeout_seconds: float = Field(default=5.0, gt=0)
    # The fallback: a persistent profile of our own, in the system Chrome. Still a real
    # browser and still the same session between runs -- just not the one already open.
    browser_profile_dir: str = str(Path.home() / ".storesplit" / "browser-profile")
    # The profile `scripts/start_chrome_cdp.py` opens the DevTools port on, and therefore the
    # one an `existing_chrome_cdp` run actually shops in. It has to be a *different* directory
    # from the one above -- a profile can only be open in one Chrome at a time, and that Chrome
    # is somebody's own window -- so the two modes are two identities: different cookies,
    # different store, different history. That is the thing to know about this setting rather
    # than to tune: which profile a run used decides what the retailer remembers of it, so the
    # session reports the one it is really on and says so when it changes. Named here rather
    # than derived inside the script so both halves read the same value -- and left beside
    # `browser_profile_dir` when nobody sets it, so moving that one moves this one too.
    browser_cdp_profile_dir: str = str(Path.home() / ".storesplit" / "chrome-cdp-profile")
    # The host's own Chrome rather than Playwright's bundled Chromium.
    browser_channel: str = "chrome"
    # Headed, so a challenge is something a person can actually answer.
    browser_headless: bool = False
    # Deliberately slower than the HTTP path: these are full page loads of a retailer that
    # has said it does not want to be crawled quickly. Measured, not guessed: six page loads
    # two seconds apart was enough to put both Target and Walmart back behind a challenge a
    # human had just cleared, so the floor sits well above that. A category page yields a
    # whole category, so a scrape needs few of them and can afford to be patient.
    browser_min_request_interval_seconds: float = Field(default=6.0, ge=0)
    # How long a retailer parked on a challenge waits for a human before it gives up for
    # this run. Its siblings are unaffected either way. A press-and-hold or an image CAPTCHA
    # takes real time to reach and finish, so this is generous on purpose.
    browser_manual_verification_timeout_seconds: float = Field(default=600.0, ge=0)
    # How often the parked page is *read* while waiting. This is a passive check -- it looks
    # at what the tab is already showing and never reloads it, because reloading mid-gesture
    # throws away a press-and-hold the person is partway through. The page is only re-asked
    # once the window above has expired, and only where nothing else could reveal the answer.
    browser_manual_verification_poll_seconds: float = Field(default=5.0, gt=0)
    # How often to log the time left, so a long wait is visible rather than silent.
    browser_manual_verification_report_seconds: float = Field(default=60.0, gt=0)
    # Reading a shelf that loads as you scroll. Small steps rather than one jump to the
    # bottom, with the pause between them varying a little, because a page read in two
    # instant leaps is not a page anybody read. The scroll stops the moment the data it came
    # for has arrived, so these are ceilings and not a script to run to the end.
    browser_scroll_step_pixels: int = Field(default=700, ge=100, le=2000)
    browser_scroll_pause_seconds: float = Field(default=1.1, gt=0)
    browser_scroll_pause_jitter_seconds: float = Field(default=0.7, ge=0)
    browser_scroll_max_steps: int = Field(default=10, ge=1, le=40)
    # How long a page load's own data calls are reused instead of loading the page again.
    # A browser page load is the most expensive request this application makes and the one a
    # retailer minds most, and two callers routinely want the same page: the store the session
    # is shopping is read off a category page, and that category is then searched.
    #
    # Deliberately short, and **not** matched to `search_freshness_ttl_seconds`. Ingest stamps
    # every offer it writes with the moment of the write, so a capture reused across two scrapes
    # would have the second one publish prices it never fetched as prices read just now --
    # inflating the age the search layer reports and pushing the next refresh out again. This is
    # sized to cover the gap between reading the session's store off a page and searching that
    # same page within one retailer's own fetch, which is the duplicate it exists to remove.
    # "Ask Target less often" is a different question, and it has its own answer:
    # `search_freshness_ttl_seconds` and `search_refresh_cooldown_seconds` decide how often a
    # scrape runs at all. **0 switches reuse off** and every ask becomes a page load again.
    browser_capture_ttl_seconds: int = Field(default=120, ge=0)
    # How long a retailer parked on a challenge answers "still parked" without loading the
    # page again. Reloading a challenge is the one thing that reliably makes it worse, and
    # after the first one the rest of a scrape's categories learn nothing by asking: they get
    # the same block and each one is another refused page load on the session. So the verdict
    # is reused until it is this old, then one ask is allowed in case a person has since
    # finished it. Matched to `search_refresh_cooldown_seconds`.
    browser_challenge_retry_interval_seconds: float = Field(default=300.0, ge=0)

    @model_validator(mode="after")
    def _keep_the_cdp_profile_beside_the_launch_profile(self) -> Self:
        """An operator who moves one profile moves both, unless they named the second.

        The CDP profile used to be derived from `browser_profile_dir`, and naming it as a
        setting of its own would otherwise silently strand it: point `BROWSER_PROFILE_DIR` at
        another disk and `scripts/start_chrome_cdp.py` would go on opening an empty directory
        under the old parent -- a brand-new visitor with no session, which is the state this
        whole layer exists to avoid.
        """
        if "browser_cdp_profile_dir" not in self.model_fields_set:
            beside = Path(self.browser_profile_dir).parent / "chrome-cdp-profile"
            self.browser_cdp_profile_dir = str(beside)
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
