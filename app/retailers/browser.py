"""An optional, persistent, headed browser for retailers that answer nothing else.

Most retailers here are reached over plain HTTPS, and that is the order acquisition is tried
in: official API, then the site's own JSON/XHR endpoint, then embedded page data, then a
store locator or sitemap, and only then this. Nothing in a normal scrape launches a browser,
and a scrape whose retailers all answer over HTTP never imports Playwright at all -- it is an
optional `browser` extra, and this module degrades to "not configured" without it.

Target and Walmart are the retailers that need it. Both sit behind PerimeterX, which answers
an ordinary client with a redirect or a challenge page long before any product data.

**What this is not.** It does not defeat a challenge, and it must never be made to. There is
no CAPTCHA solving, no fingerprint spoofing, no proxy rotation, no automated press-and-hold.
When a retailer decides a human should confirm, the only supported answer is that a human
confirms: `ManualVerificationRequiredError` is raised, the window is left open on the challenge
with its session intact, and the person running the scrape is told which retailer is waiting
and what to do. Nothing is retried behind their back.

**Why a real profile.** The context is launched against a persistent profile directory with
the host's own Chrome, so the cookies and local storage a normal visit accumulates -- and
whatever a human confirmed by hand -- survive between runs and are reused rather than
rebuilt. One browser identity per machine, not one per scrape: repeatedly arriving as a
brand-new visitor is both what triggers challenges and what makes them worth triggering.

**Which profile, though.** The two modes are two profiles, and therefore two identities: an
attached Chrome shops in whatever directory *it* was started on, which is not the directory
this process would launch. Measured on a real profile, what survives a browser restart is the
persistent cookies (PerimeterX's `_px3`/`_pxvid`, `refreshToken`, `UserLocation`), all of
local storage, IndexedDB and the registered service worker; what does not is every
session-scoped cookie, Target's selected store (`sddStore`) among them, so a restarted browser
re-picks a store from the location it remembers. None of that is ours to change -- forging a
session cookie would be exactly the disguise this module refuses -- so the answer is to be
honest about it: `diagnostics()` reports the profile actually in use, a run that changes
identity says so, and the session is kept alive rather than restarted.

**Page loads are the expensive thing.** One category page load yields a whole category, and it
is also the request a retailer minds most. So a load's own data calls are kept for
`browser_capture_ttl_seconds` and handed to the next caller that asks for the same page and is
satisfied by what was captured -- which is how reading the session's store off a category page
stops costing a second load of that category. When that capture is *not* enough for the next
caller, the page it came from is usually still open on that very URL, so it is read further
rather than loaded again: same answer, no second arrival. And a retailer parked on a challenge
answers from its standing verdict instead of loading the challenge again; reloading one is
both useless and the surest way to make it worse.

**A load waits for the page's own data, not for the clock.** `settle_ms` is a ceiling, not a
duration: the settle looks at the first-party responses the load is collecting and stops as
soon as they answer the caller's question or stop arriving. A category whose shelf answers in
a second costs a second. Waiting a fixed stretch instead is the mistake Playwright's own
guidance names -- too short is flaky, too long is slow -- and here it was eight seconds a
page, seven pages a scrape. A page that never answers still waits the whole ceiling, which is
the conservative direction: that is what a challenge looks like, and it must not be called
one early.

**Isolation.** Each retailer gets its own page in the one shared context. A retailer parked
on a verification checkpoint therefore blocks only itself: its siblings keep their own pages
and keep going, which is the rule the rest of the scrape already follows -- a failure costs
what it touched and no more. Requests are paced per host by
`browser_min_request_interval_seconds`, deliberately slower than the HTTP path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

from app.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from playwright.async_api import Page

log = logging.getLogger("storesplit.retailers.browser")

# Wording that means "a human should look at this", collected from the challenge pages the
# two retailers actually serve. Matched case-insensitively against the rendered text, and
# deliberately short: a false positive parks a retailer and tells someone, which is the safe
# direction. A false negative would have us scrape a challenge page as though it were data.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "robot or human",
    "press and hold",
    "press & hold",
    "verify you are a human",
    "verify you are human",
    "are you a human",
    "human verification",
    "security check",
    "unusual traffic",
    "captcha",
    "access denied",
    "additional verification required",
)

# A challenge does not always reach the page. Target renders its whole shell -- header,
# store name, category title -- and blocks only the data calls underneath it, each of which
# comes back as a PerimeterX block describing the CAPTCHA that should be shown. A page like
# that looks fine and contains nothing, so the responses are inspected too: these fragments
# appear in the block body and never in real product data.
BLOCKED_RESPONSE_MARKERS: tuple[str, ...] = (
    '"blockscript"',
    "px-cdn.net",
    "/captcha.js",
    "perimeterx.net/",
)
# Only bodies small enough to be a block notice are examined; real payloads are far larger
# and scanning them for every navigation would cost more than it is worth.
_MAX_BLOCK_BODY_BYTES = 8192

# How many page reads are kept for reuse at once. A scrape wants seven category pages per
# retailer, so this is generous; it exists because the entries are whole JSON payloads and a
# process that runs for weeks would otherwise keep every page it ever read.
_MAX_REMEMBERED_PAGES = 64

# How often a settling load looks at what the page has fetched, and how long its data calls
# must have been quiet before the load counts as finished. Both well inside `settle_ms`, which
# remains the ceiling: these only decide how much of it a page that has already answered pays.
_SETTLE_POLL_MS = 250
_SETTLE_QUIET_MS = 1000

# The main-document status that means "a human should look at this". 403 is what PerimeterX
# answers with, and Playwright hands it back as an ordinary response rather than raising, so
# without this a refused page with no recognisable wording is scraped as though it were a shelf.
# Two statuses are deliberately *not* here. A 404 is a category path that has moved -- a stale
# `categories.json`, not a challenge -- and sending somebody to press and hold in a browser
# window would send them to the wrong place. A 429 is the origin asking for less of us, which
# is also not something a person can complete; one that says so in words ("unusual traffic") is
# already caught by `CHALLENGE_MARKERS`, and one that does not is left to fail as a load rather
# than be labelled a checkpoint nobody can clear.
_TURNED_AWAY_STATUS = 403


# The modes a run can be in, reported verbatim so a log line answers "how did it connect,
# and what is it waiting for" without anyone reading the code.
MODE_EXISTING_CHROME_CDP = "existing_chrome_cdp"
MODE_PERSISTENT_SYSTEM_CHROME = "persistent_system_chrome"
MODE_MANUAL_VERIFICATION_REQUIRED = "manual_verification_required"
MODE_VERIFIED_SESSION = "verified_session"
MODE_CHALLENGE_DETECTED = "challenge_detected"


def user_data_dir_in(command_lines: Iterable[str], port: int) -> str | None:
    """The profile directory of the process serving `port`, read out of its command line.

    Pure, so the reading is testable without a browser.

    Three things it is careful about. The path is cut at the next flag rather than at the next
    space, because a profile directory may contain one and half a path is worse than no answer.
    Only a line whose executable is a Chrome counts -- any other process whose *arguments* merely
    mention these flags (a shell running a command like this one) is not a browser. And two
    browsers disagreeing about the profile behind one port, which a second Chrome that asked for
    a port already taken would produce, is an ambiguous answer: this value must not be
    confidently wrong, so it becomes no answer instead. Chrome's own helper processes inherit
    both flags and agree, which is why the answers are compared rather than counted.
    """
    marker = "--user-data-dir="
    found: set[str] = set()
    for line in command_lines:
        if f"--remote-debugging-port={port}" not in line or marker not in line:
            continue
        executable = line.split(" --", 1)[0]
        if "chrome" not in executable.lower() and "chromium" not in executable.lower():
            continue
        tail = line.split(marker, 1)[1]
        end = tail.find(" --")
        directory = (tail if end < 0 else tail[:end]).strip()
        if directory:
            found.add(directory)
    if len(found) != 1:
        return None
    return found.pop()


class BrowserUnavailableError(RuntimeError):
    """The browser layer was asked for and cannot be provided.

    Either the `browser` extra is not installed, the fallback is switched off, or Chrome
    could not be launched. A retailer that depends on it records this and is skipped; it is
    never a reason to fall back to guessing at data.
    """


@dataclass
class ManualVerificationRequiredError(RuntimeError):
    """A retailer is showing a human-verification challenge, and is waiting for a human.

    Raised instead of any attempt to get past it. The page is left open and untouched so the
    person can finish it in the window that is already on screen, and the session that
    results is the one the next request reuses.
    """

    retailer: str
    url: str
    marker: str

    def __str__(self) -> str:
        return (
            f"{self.retailer} needs manual human verification in the open Chrome window "
            f'(showing "{self.marker}" at {self.url}). Complete it and tell me "done".'
        )


@dataclass
class _HostPace:
    """The last time a host was hit, so the next request to it can wait its turn."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = 0.0


class BrowserSession:
    """The process's one persistent browser context, shared by the retailers that need it.

    Built lazily on first use and closed once, with the application. Construction is cheap
    and launches nothing, so a scrape that never reaches a browser retailer never pays for
    it.
    """

    def __init__(self, *, enabled: bool | None = None) -> None:
        """`enabled` overrides `BROWSER_FALLBACK_ENABLED`.

        The operator tools (`scripts/probe_browser_retailer.py`, `audit_offers.py --check-
        urls`) pass `True`: running one of them *is* the explicit request for a browser that
        the setting otherwise waits for. Everything else leaves it alone and stays off.
        """
        settings = get_settings()
        self._enabled = settings.browser_fallback_enabled if enabled is None else enabled
        self._profile_dir = settings.browser_profile_dir
        self._attach_cdp = settings.browser_attach_cdp
        self._cdp_endpoint = settings.browser_cdp_endpoint
        self._cdp_timeout = settings.browser_cdp_timeout_seconds
        self._channel = settings.browser_channel
        self._headless = settings.browser_headless
        self._interval = settings.browser_min_request_interval_seconds
        self._verification_timeout = settings.browser_manual_verification_timeout_seconds
        self._verification_poll = settings.browser_manual_verification_poll_seconds
        self._verification_report = settings.browser_manual_verification_report_seconds
        self._scroll_step = settings.browser_scroll_step_pixels
        self._scroll_pause = settings.browser_scroll_pause_seconds
        self._scroll_jitter = settings.browser_scroll_pause_jitter_seconds
        self._scroll_max_steps = settings.browser_scroll_max_steps
        self._cdp_profile_dir = settings.browser_cdp_profile_dir
        self._capture_ttl = settings.browser_capture_ttl_seconds
        self._challenge_retry = settings.browser_challenge_retry_interval_seconds
        self._launch_lock = asyncio.Lock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._browser: Any | None = None  # set only when attached over CDP
        self._mode: str | None = None
        # The profile the *attached* browser is on, when it can be established. The configured
        # directory is only what this process would launch itself; in `existing_chrome_cdp` the
        # session lives wherever that Chrome was started, and reporting the configured one
        # there is how a run reads as continuous while it has quietly changed identity.
        self._attached_profile: str | None = None
        # Tabs this process opened, so an attached Chrome gets its own tabs left alone.
        self._own_pages: set[str] = set()
        # Retailers whose session has been seen answering normally at least once.
        self._verified: set[str] = set()
        self._pages: dict[str, Page] = {}
        self._paces: dict[str, _HostPace] = {}
        # Data calls a retailer's page issued that came back as a challenge rather than
        # data, keyed by retailer and reset at each navigation.
        self._blocked: dict[str, list[str]] = {}
        # One navigation at a time per retailer. Two overlapping `visit`s for one retailer
        # would each build a page and each reset the block evidence, and the second reset
        # would erase the first's -- handing back a page whose data calls were all refused
        # as though it were data. That is precisely the failure this module exists to catch.
        self._nav_locks: dict[str, asyncio.Lock] = {}
        # Main-frame navigations this retailer's page has made, counted so a wait can tell
        # that the *site* moved on its own -- which is what a solved challenge looks like
        # from outside: PerimeterX hands the real page back without anyone asking it to.
        self._navigations: dict[str, int] = {}
        # Bodies of the page's own data calls, kept for the navigation that asked for them.
        # This is how a browser-backed adapter reads prices: not by scraping rendered text,
        # but by reading the JSON the retailer's own page fetched to render itself.
        self._captured: dict[str, list[tuple[str, Any]]] = {}
        # The filter belongs to the *page*, not to the operation reading it: it is armed when
        # the page is navigated and stays armed until the next navigation clears both it and
        # the list. Disarming it between the settle and the shelf read -- or between two reads
        # of one page -- drops the calls that land in the gap, and a page is not asked to fetch
        # them again. A category page answers `store_location_v1` before `plp_search_v2`, so a
        # load that stopped at the store and then disarmed lost the entire shelf: the read that
        # followed it scrolled a page whose first screen it no longer had, and the category came
        # back empty -- which ingest treats as "this store carries none of this" and expires the
        # offers that were there.
        self._capture_match: dict[str, Callable[[str], bool] | None] = {}
        # Response handlers run as tasks. Holding them keeps the event loop's only strong
        # reference alive, so a check is never collected half-finished.
        self._watchers: dict[str, set[asyncio.Task[None]]] = {}
        # Retailers currently parked on a challenge, so the API and the logs can say so,
        # and when each was parked -- the rest of a scrape is told "still parked" from that
        # rather than by loading the challenge again.
        self._awaiting: dict[str, ManualVerificationRequiredError] = {}
        self._parked_at: dict[str, float] = {}
        # What a successful page read captured, keyed by (retailer, url), with the moment it
        # was read. A page load is the costliest request this module makes, and two callers
        # want the same page as a matter of course: the store this session is shopping is read
        # off a category page, and that category is then searched. A cached capture is only
        # ever handed to a caller whose own `enough` is satisfied by it, so reuse cannot turn
        # into answering one question with another question's data.
        self._cache: dict[tuple[str, str], tuple[float, list[tuple[str, Any]]]] = {}
        # Where each retailer's page is standing: the URL it was last navigated to *and* the
        # navigation count and the moment that load passed its challenge check. All three
        # matter. The URL says the page is showing what a caller is about to ask for; the count
        # says nothing has moved it since -- a site that navigated itself (an interstitial, a
        # redirect, a challenge) bumps the counter and the page stops being continuable; the
        # moment bounds how long what it fetched may still be handed to a caller.
        self._page_at: dict[str, tuple[str, int, float]] = {}
        # Page loads performed, and page loads a capture stood in for, per retailer: what a run
        # needs to answer "how much did this actually navigate".
        self._loads: dict[str, int] = {}
        self._reused: dict[str, int] = {}
        # Reads that continued an already-loaded page instead of loading it again, first-party
        # data responses kept, and times a retailer was parked on a challenge. The three numbers
        # a run is judged on beside the loads: work avoided, work done, and work refused.
        self._continued: dict[str, int] = {}
        self._payloads: dict[str, int] = {}
        self._challenges: dict[str, int] = {}

    # ------------------------------------------------------------------ availability

    def is_configured(self) -> bool:
        """True when a retailer may ask for a browser at all."""
        if not self._enabled:
            return False
        try:
            import playwright.async_api  # noqa: F401  - probing that the extra is installed
        except ImportError:
            return False
        return True

    @property
    def mode(self) -> str | None:
        """How this session is connected, or None before anything has connected."""
        return self._mode

    def retailer_state(self, retailer: str) -> str:
        """Where one retailer stands: waiting on a person, challenged, or answering."""
        if retailer in self._awaiting:
            return MODE_MANUAL_VERIFICATION_REQUIRED
        if self._blocked.get(retailer):
            return MODE_CHALLENGE_DETECTED
        if retailer in self._verified:
            return MODE_VERIFIED_SESSION
        return "not_visited"

    @property
    def profile_dir_in_use(self) -> str | None:
        """The profile this session's browser is really on, so far as it can be established.

        In `persistent_system_chrome` that is the configured directory, because this process
        opened it. In `existing_chrome_cdp` it is the directory the attached Chrome was started
        on -- a different profile, with its own cookies and its own chosen store -- or None when
        that could not be read, which is reported as unknown rather than filled in with the
        configured value. Reporting the wrong profile is how a session that changed identity
        gets audited as one continuous session.
        """
        if self._mode == MODE_EXISTING_CHROME_CDP:
            return self._attached_profile
        if self._mode == MODE_PERSISTENT_SYSTEM_CHROME:
            return self._profile_dir
        return None

    def diagnostics(self) -> dict[str, Any]:
        """Everything a run should say about itself, in one place.

        `mode` is how it connected -- `existing_chrome_cdp` when it attached to a Chrome that
        was already running, `persistent_system_chrome` when it opened its own profile in the
        system Chrome. `profile_dir` is the profile that mode is actually shopping in, which is
        the configured one only in the second; the configured and CDP directories are reported
        beside it so a run that changed identity is visible rather than inferred.

        The five counts are how much the session cost. `page_loads` is what it navigated;
        `pages_reused_from_capture` and `pages_continued` are the loads it did not need,
        answered from a kept capture and by reading a page that was still open;
        `data_responses` is the first-party payloads it actually took data from, which is the
        work the loads were for; `challenges` is how often a retailer was parked for a human.
        `retailers` is where each one stands right now.
        """
        return {
            "mode": self._mode,
            "cdp_endpoint": self._cdp_endpoint if self._attach_cdp else None,
            "profile_dir": self.profile_dir_in_use,
            "configured_profile_dir": self._profile_dir,
            "cdp_profile_dir": self._cdp_profile_dir if self._attach_cdp else None,
            "headless": self._headless,
            "page_loads": dict(sorted(self._loads.items())),
            "pages_reused_from_capture": dict(sorted(self._reused.items())),
            "pages_continued": dict(sorted(self._continued.items())),
            "data_responses": dict(sorted(self._payloads.items())),
            "challenges": dict(sorted(self._challenges.items())),
            "retailers": {
                retailer: self.retailer_state(retailer)
                for retailer in sorted(set(self._pages) | set(self._awaiting) | self._verified)
            },
        }

    async def storage_census(self) -> dict[str, Any]:
        """What this session is actually carrying, counted: the answer to "did it survive".

        Cookies by host, split into the ones that outlive a browser restart and the ones that do
        not, plus each open page's own web storage. **Counts, hosts and storage keys only** -- a
        cookie value is a credential and this has no reason to read one.

        The split is the point. Target's selected store (`sddStore`) and its access tokens are
        session-scoped: they are gone the next time Chrome starts, while its PerimeterX cookies,
        `refreshToken` and remembered location are not. A restarted browser therefore re-picks a
        store rather than resuming one, and a session that looks continuous is partly new. This
        is how an operator sees that for themselves instead of taking a README's word for it.
        """
        context = self._context
        if context is None:
            return {"connected": False}
        hosts: dict[str, dict[str, int]] = {}
        cookies = await context.cookies()
        for cookie in cookies:
            host = str(cookie.get("domain") or "").lstrip(".")
            counted = hosts.setdefault(host, {"persistent": 0, "session_scoped": 0})
            # Playwright reports a session cookie as expires -1; Chrome drops those on exit.
            expires = cookie.get("expires", -1)
            counted["session_scoped" if expires in (-1, 0) else "persistent"] += 1
        pages: dict[str, Any] = {}
        for slug, page in self._pages.items():
            pages[slug] = await self._page_storage(page)
        return {
            "connected": True,
            "profile_dir": self.profile_dir_in_use,
            "cookies_total": len(cookies),
            "cookies_by_host": dict(sorted(hosts.items())),
            "pages": pages,
        }

    @staticmethod
    async def _page_storage(page: Page) -> dict[str, Any]:
        """One page's local/session storage, databases and service workers, as counts."""
        if page.is_closed():
            return {"closed": True}
        try:
            return await page.evaluate(
                """async () => {
                    const out = {origin: location.origin};
                    try { out.local_storage_keys = localStorage.length; }
                    catch (e) { out.local_storage_keys = 'unreadable:' + e.name; }
                    try { out.session_storage_keys = sessionStorage.length; }
                    catch (e) { out.session_storage_keys = 'unreadable:' + e.name; }
                    try { out.indexed_db = (await indexedDB.databases()).map(d => d.name); }
                    catch (e) { out.indexed_db = 'unreadable:' + e.name; }
                    try {
                        const regs = await navigator.serviceWorker.getRegistrations();
                        out.service_workers = regs.map(r => r.scope);
                        out.service_worker_controlling = !!navigator.serviceWorker.controller;
                    } catch (e) { out.service_workers = 'unreadable:' + e.name; }
                    return out;
                }"""
            )
        except Exception as exc:  # a page torn down mid-read must not fail the report
            return {"unreadable": f"{type(exc).__name__}"}

    def awaiting_verification(self) -> dict[str, str]:
        """Retailer slug -> the message a human needs to act on, for whoever is watching."""
        return {slug: str(pending) for slug, pending in self._awaiting.items()}

    # ------------------------------------------------------------------ lifecycle

    async def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context
        if not self._enabled:
            raise BrowserUnavailableError(
                "browser fallback is off; set BROWSER_FALLBACK_ENABLED=true to use it"
            )
        async with self._launch_lock:
            if self._context is not None:
                return self._context
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise BrowserUnavailableError(
                    "the browser fallback needs the optional extra: uv sync --extra browser"
                ) from exc
            self._playwright = await async_playwright().start()

            if self._attach_cdp:
                context = await self._attach_to_running_chrome()
                if context is not None:
                    self._context = context
                    self._mode = MODE_EXISTING_CHROME_CDP
                    log.info(
                        "browser_mode",
                        extra={
                            "mode": self._mode,
                            "endpoint": self._cdp_endpoint,
                            "profile_dir": self._attached_profile,
                        },
                    )
                    return context
                # Falling through is not merely a different connection: it is a different
                # profile, so the cookies, the logins and the chosen store are not the ones
                # verified in the window somebody set up. Said plainly, because a session that
                # changes identity quietly is indistinguishable in a log from one that did not.
                log.warning(
                    "browser_profile_identity_changed",
                    extra={
                        "wanted": MODE_EXISTING_CHROME_CDP,
                        "using": MODE_PERSISTENT_SYSTEM_CHROME,
                        "cdp_profile_dir": self._cdp_profile_dir,
                        "profile_dir": self._profile_dir,
                        "hint": (
                            "no Chrome was listening, so this run uses the fallback profile -- "
                            "a different identity. Run scripts/start_chrome_cdp.py first to "
                            "keep using the profile you verified in."
                        ),
                    },
                )

            self._context = await self._launch_persistent()
            self._mode = MODE_PERSISTENT_SYSTEM_CHROME
            log.info(
                "browser_mode",
                extra={
                    "mode": self._mode,
                    "channel": self._channel,
                    "profile_dir": self._profile_dir,
                    "headless": self._headless,
                },
            )
            return self._context

    async def _attach_to_running_chrome(self) -> Any | None:
        """Attach to a Chrome that is already running, and use the session it already has.

        This is the honest version of "use my browser": no profile is copied and no state is
        synthesised. Whatever that Chrome is already logged into, whatever store it has
        selected, whatever cookies it holds -- that is what the page sees, because it *is*
        that browser. All this does is open a tab in it and read what the tab fetches.

        Chrome only listens for this if it was started with `--remote-debugging-port`, and
        recent versions refuse that flag on the default profile directory unless a
        `--user-data-dir` is given too. So a closed port is the normal case, not an error:
        it falls through to the persistent profile below, which is still a real Chrome.
        """
        endpoint = self._cdp_endpoint
        if not await self._cdp_is_listening(endpoint):
            log.info(
                "browser_cdp_not_listening",
                extra={
                    "endpoint": endpoint,
                    "hint": (
                        "start Chrome with --remote-debugging-port=9222 "
                        "(and --user-data-dir=... on recent versions) to attach to it"
                    ),
                },
            )
            return None
        # A Chrome with no tabs open cannot do browser-context management, and the attach
        # fails on it with a protocol error rather than anything self-explanatory. Opening a
        # blank tab first is the documented DevTools way to give it one back.
        await self._ensure_cdp_target(endpoint)
        try:
            assert self._playwright is not None
            browser = await self._playwright.chromium.connect_over_cdp(
                endpoint, timeout=self._cdp_timeout * 1000
            )
        except Exception as exc:
            log.warning(
                "browser_cdp_attach_failed", extra={"endpoint": endpoint, "error": str(exc)[:200]}
            )
            return None
        self._browser = browser
        # The context that is already there carries the real cookies and storage. A fresh one
        # would be private and blank -- no cookies, no local storage, nothing a human ever
        # confirmed -- so a browser offering none is not a session to use: this reports it and
        # hands back nothing, and the persistent profile takes the run instead, which is at
        # least an identity that persists. `_ensure_cdp_target` has already given the browser a
        # tab, so an empty list here means something is wrong with it rather than that it is
        # merely idle.
        contexts = browser.contexts
        if not contexts:
            log.warning(
                "browser_cdp_no_context",
                extra={
                    "endpoint": endpoint,
                    "hint": (
                        "the attached browser reports no browser context; falling back to the "
                        "persistent profile rather than opening a blank private one"
                    ),
                },
            )
            with contextlib.suppress(Exception):
                await browser.close()
            self._browser = None
            return None
        self._attached_profile = await self._attached_user_data_dir(endpoint)
        return contexts[0]

    async def _attached_user_data_dir(self, endpoint: str) -> str | None:
        """The profile directory of the Chrome this process attached to, if it can be read.

        The first question an audit of session continuity asks, and in this mode the configured
        directory is the wrong answer to it. The browser will not say: Chrome refuses to report
        its own command line unless it was launched by automation, and this one deliberately was
        not. So it is read from the process holding the DevTools port -- a local, read-only look
        at a command line, nothing sent anywhere and nothing changed. Best effort by design:
        None means "not established", never "the configured one".
        """
        listing = None
        try:
            # `axww`, combined BSD style: `ps -ww ax` is rejected on macOS. The `ww` matters
            # because BSD ps otherwise truncates the last column to the terminal width, and the
            # documented workflow runs from a terminal -- a cut landing inside the profile path
            # would make this report half of the one value that must never be half right.
            listing = await asyncio.create_subprocess_exec(
                "ps",
                "axww",
                "-o",
                "command=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(listing.communicate(), timeout=self._cdp_timeout)
        except (OSError, TimeoutError, NotImplementedError):
            # No ps, no subprocess support on this loop, or it did not answer promptly. None of
            # those is a reason to fail an attach that otherwise worked.
            if listing is not None and listing.returncode is None:
                listing.kill()  # do not leave it running unreaped behind the timeout
                with contextlib.suppress(Exception):
                    await listing.wait()
            return None
        found = user_data_dir_in(
            stdout.decode("utf-8", "replace").splitlines(), urlsplit(endpoint).port or 9222
        )
        # A directory that is not there is a path that was read wrong, and reporting the wrong
        # profile is worse than reporting none.
        if found is not None and not Path(found).is_dir():
            log.warning("browser_cdp_profile_unreadable", extra={"read": found})
            return None
        return found

    async def _ensure_cdp_target(self, endpoint: str) -> None:
        """Make sure the attached Chrome has at least one tab.

        Closing our own tabs can leave it with none -- the window is gone but the process is
        still running and still listening -- and in that state it answers the attach with
        "Browser context management is not supported". A blank tab restores it.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._cdp_timeout) as client:
                listed = await client.get(f"{endpoint.rstrip('/')}/json/list")
                if listed.status_code == 200 and listed.json():
                    return
                await client.put(f"{endpoint.rstrip('/')}/json/new?about:blank")
        except Exception as exc:  # the attach below reports the real problem
            log.debug("browser_cdp_new_target_failed", extra={"error": str(exc)[:200]})

    async def _cdp_is_listening(self, endpoint: str) -> bool:
        """Is something answering the DevTools endpoint? Checked before attaching, so a
        closed port costs a connection refusal rather than a timeout."""
        parts = urlsplit(endpoint)
        host, port = parts.hostname or "127.0.0.1", parts.port or 9222
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._cdp_timeout
            )
        except (TimeoutError, OSError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def _launch_persistent(self) -> Any:
        """The fallback: the system's own Chrome, on a profile directory that persists.

        Deliberately plain. No launch arguments beyond the profile and the window, no
        `--disable-blink-features`, no user-agent override, no init script -- nothing that
        would change what the page can see about the browser. If a retailer decides this
        looks like automation, the answer is a person confirming it, not a disguise.
        """
        assert self._playwright is not None
        try:
            return await self._playwright.chromium.launch_persistent_context(
                self._profile_dir,
                headless=self._headless,
                channel=self._channel,
                viewport={"width": 1400, "height": 950},
            )
        except Exception as exc:  # pragma: no cover - depends on the host's Chrome
            await self.aclose()
            raise BrowserUnavailableError(self._launch_failure(exc)) from exc

    def _launch_failure(self, exc: Exception) -> str:
        """What to say when Chrome would not open our profile.

        One cause is worth naming, because Chrome's own wording does not mention profiles at
        all: a profile directory can be open in one Chrome at a time, so a second process
        asking for the same one is told it is "opening in existing browser session" and gets no
        browser. Two processes on one profile is the ordinary way to arrive here -- a reloading
        uvicorn, or a script run beside the API -- and the fix is to use the one that is already
        open, not to make a second profile, which would be a second identity.
        """
        detail = str(exc)
        if "existing browser session" in detail or "already in use" in detail:
            return (
                f"{self._profile_dir} is already open in another Chrome, and a profile can only "
                "be open in one at a time. Close the other window or scrape process and retry; "
                "pointing BROWSER_PROFILE_DIR elsewhere would start a second session from "
                f"scratch. Chrome said: {detail[:200]}"
            )
        return f"could not launch {self._channel}: {detail}"

    async def aclose(self) -> None:
        """Let go of the browser. Called once, on shutdown.

        An attached Chrome is somebody's actual browser: closing it would shut their windows.
        So in that mode this closes only the tabs this process opened and drops the
        connection, leaving the browser -- and the session it has earned -- running. A
        profile we launched ourselves is ours to close.
        """
        context, browser, playwright = self._context, self._browser, self._playwright
        mode = self._mode
        pages = list(self._pages.values())
        self._context = self._browser = self._playwright = None
        self._mode = None
        self._pages.clear()
        for tasks in self._watchers.values():
            for task in tasks:
                task.cancel()
        self._watchers.clear()
        self._awaiting.clear()
        self._parked_at.clear()
        self._blocked.clear()
        self._captured.clear()
        self._cache.clear()
        self._page_at.clear()
        self._attached_profile = None

        if mode == MODE_EXISTING_CHROME_CDP:
            # Close the tabs we opened, but never the last one: a Chrome left with no tabs
            # keeps running yet refuses the next attach, which turns a tidy shutdown into a
            # broken start. The survivor is parked on a blank page rather than a retailer.
            for index, page in enumerate(pages):
                with contextlib.suppress(Exception):
                    if index == len(pages) - 1 and len(page.context.pages) <= 1:
                        await page.goto("about:blank")
                    else:
                        await page.close()
            if browser is not None:
                # Disconnects the client; the browser itself keeps running.
                with contextlib.suppress(Exception):
                    await browser.close()
        elif context is not None:
            try:
                await context.close()
            except Exception:  # pragma: no cover - shutdown must not raise
                log.warning("browser_context_close_failed", exc_info=True)
        self._own_pages.clear()
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # pragma: no cover - shutdown must not raise
                log.warning("browser_playwright_stop_failed", exc_info=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ pages

    async def page_for(self, retailer: str) -> Page:
        """This retailer's own page in the shared context, created once and reused.

        Its own page is what keeps one retailer's checkpoint from stopping the others, and
        reusing it is what keeps the session (and the retailer's chosen store) in place.
        """
        # A page this retailer already holds is the session: reuse it without touching the
        # context, which is also what keeps its cookies and its chosen store in place.
        page = self._pages.get(retailer)
        if page is not None and not page.is_closed():
            return page
        context = await self._ensure_context()
        page = await context.new_page()
        self._own_pages.add(retailer)
        self._pages[retailer] = page
        self._blocked.setdefault(retailer, [])

        async def keep_payload(response: Any) -> None:
            """Keep the JSON of a data call this navigation was told to collect."""
            match = self._capture_match.get(retailer)
            if match is None or not match(response.url):
                return
            try:
                body = await response.json()
            except Exception:
                return
            self._captured.setdefault(retailer, []).append((response.url, body))
            self._payloads[retailer] = self._payloads.get(retailer, 0) + 1

        async def note_blocked(response: Any) -> None:
            """Record a first-party response that is a challenge notice, not data."""
            await keep_payload(response)
            if len(self._blocked.setdefault(retailer, [])) >= 5:
                return  # a handful is plenty of evidence; do not read a whole page of them
            headers = response.headers or {}
            # A block notice is small JSON or HTML. Anything else -- an image, a script, a
            # real payload -- is skipped without being read, which is the point of the cap:
            # a chunked response carries no content-length, so the type is what gates it.
            content_type = str(headers.get("content-type") or "").lower()
            if content_type and not any(
                kind in content_type for kind in ("json", "text/html", "text/plain")
            ):
                return
            try:
                length = int(headers.get("content-length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > _MAX_BLOCK_BODY_BYTES:
                return
            try:
                body = await response.text()
            except Exception:
                return
            if len(body) > _MAX_BLOCK_BODY_BYTES:
                return
            lowered = body.lower()
            if any(marker in lowered for marker in BLOCKED_RESPONSE_MARKERS):
                self._blocked.setdefault(retailer, []).append(response.url)

        def watch(response: Any) -> None:
            tasks = self._watchers.setdefault(retailer, set())
            task = asyncio.create_task(note_blocked(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def note_navigation(frame: Any) -> None:
            # Only the top frame: ad and widget iframes navigate constantly.
            if frame == page.main_frame:
                self._navigations[retailer] = self._navigations.get(retailer, 0) + 1

        page.on("response", watch)
        page.on("framenavigated", note_navigation)
        return page

    async def _settle_watchers(self, retailer: str) -> None:
        """Let this retailer's in-flight response checks finish before reading their verdict.

        The handlers run as tasks, so a block notice arriving at the end of the settle
        window can still be unread when `_guard` looks. Waiting closes that race; the
        timeout keeps a stuck body from holding the scrape, at the cost of the detection it
        would have contributed.
        """
        tasks = [task for task in self._watchers.get(retailer, set()) if not task.done()]
        if not tasks:
            return
        await asyncio.wait(tasks, timeout=5.0)

    # ------------------------------------------------------------------ navigation

    async def _pace(self, url: str) -> None:
        """Hold the next request to a host until its quiet interval has passed."""
        host = urlsplit(url).hostname or ""
        pace = self._paces.setdefault(host, _HostPace())
        async with pace.lock:
            wait = pace.last + self._interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            pace.last = time.monotonic()

    async def visit(
        self,
        retailer: str,
        url: str,
        *,
        settle_ms: int = 2500,
        timeout_ms: int = 60_000,
        capture: Callable[[str], bool] | None = None,
    ) -> Page:
        """Navigate this retailer's page to `url` and hand it back once it has settled.

        Raises `ManualVerificationRequiredError` if the retailer is asking for a human, having
        left the page exactly as it is so that human can answer it.
        """
        lock = self._nav_locks.setdefault(retailer, asyncio.Lock())
        async with lock:
            return await self._navigate(retailer, url, settle_ms, timeout_ms, capture)

    async def load_and_read(
        self,
        retailer: str,
        url: str,
        *,
        enough: Callable[[list[tuple[str, Any]]], bool],
        settle_ms: int = 2500,
        timeout_ms: int = 60_000,
        capture: Callable[[str], bool] | None = None,
    ) -> list[tuple[str, Any]]:
        """Load a page and return the data it fetched, as one indivisible operation.

        A retailer has one page here, and its categories are scraped concurrently. Navigating
        and *then* reading what the navigation produced is therefore two halves of a race:
        the second category's navigation resets the capture and steers the shared page away
        while the first is still reading it, and both come back with nothing. That is not
        hypothetical -- it is why a Target scrape of all seven categories wrote zero offers
        while a scrape of one wrote twelve.

        Holding the retailer's lock across load, settle, challenge check, scroll and snapshot
        closes it. The categories of a browser-backed retailer are effectively sequential,
        which is what a single browser window can honestly do anyway, and what the pacing
        wants regardless.

        A recent load of this same page is reused rather than repeated, but only when what it
        captured satisfies this caller's own `enough`: the same category page answers "which
        store is this session shopping" and "what is on this shelf", and the second question
        needs more of the page than the first, so reuse that ignored the predicate would hand
        back one question's data as the other's answer.

        When it does not satisfy it, that is still not a reason to load the page again. The
        first caller stopped as soon as its own question was answered, so the page it stopped
        on is still open, still on this URL and still unmoved -- it was simply not read far
        enough. Reading it further is the same answer as loading it again, minus the arrival:
        for the one category that answers "which store is this" *and* "what is on this shelf",
        that is the difference between eight page loads a scrape and seven.
        """
        lock = self._nav_locks.setdefault(retailer, asyncio.Lock())
        async with lock:
            # Before the cache, not after it. A retailer waiting on a human has not been
            # scraped, and answering from a capture would have the run write those prices with
            # this moment's timestamp -- reporting data it did not fetch as data it did.
            self._refuse_if_parked(retailer)
            reusable = self._reusable_capture(retailer, url)
            if reusable is not None and enough(reusable):
                self._reused[retailer] = self._reused.get(retailer, 0) + 1
                log.info(
                    "browser_capture_reused",
                    extra={"retailer": retailer, "url": url, "payloads": len(reusable)},
                )
                return reusable
            page = self._still_showing(retailer, url)
            if page is not None:
                self._continued[retailer] = self._continued.get(retailer, 0) + 1
                log.info(
                    "browser_page_continued",
                    extra={"retailer": retailer, "url": url, "payloads": len(reusable or [])},
                )
                # This caller's filter, for the rest of the shelf. The page has been collecting
                # under the last one all along, which is what makes continuing possible at all.
                self._capture_match[retailer] = capture
            else:
                page = await self._navigate(retailer, url, settle_ms, timeout_ms, capture, enough)
            captured = await self._read_shelf(retailer, page, enough)
            # Checked *after* the read, and on both paths. A challenge does not have to be
            # there when the page loads: Target's shape is a shell that renders and data calls
            # that are refused, and the calls a shelf read waits for are exactly the ones most
            # likely to be refused. Without this, a read whose payloads were all blocks returns
            # an empty shelf, the search succeeds, and ingest expires every offer the store had
            # in that category -- publishing a challenge as "this store carries none of these".
            await self._guard(retailer, page)
            self._remember_capture(retailer, url, captured)
            return captured

    def _still_showing(self, retailer: str, url: str) -> Page | None:
        """This retailer's page, if it is still standing on `url` exactly as it was left.

        "Exactly as it was left" is the whole condition, and it is three questions. The URL must
        be the one being asked for; the page must not have navigated since that load was cleared
        -- so a site that moved itself (a redirect, an interstitial, a challenge) takes its page
        out of this path rather than having a stale shelf read off it; and the load must still be
        inside the capture window, because continuing a page hands back what it fetched then, and
        that may no more outlive `browser_capture_ttl_seconds` than a kept capture may. The
        window is measured from the load, not from the last read: a chain of continuations must
        not walk data past it a step at a time.

        A retailer parked since then is out too: its page is showing a challenge, not a shelf.
        """
        if retailer in self._awaiting or not self._capture_ttl:
            return None
        standing = self._page_at.get(retailer)
        if standing is None:
            return None
        at_url, after, loaded_at = standing
        if at_url != url or after != self._navigations.get(retailer, 0):
            return None
        if time.monotonic() - loaded_at > self._capture_ttl:
            return None
        page = self._pages.get(retailer)
        return None if page is None or page.is_closed() else page

    def _remember_capture(self, retailer: str, url: str, captured: list[tuple[str, Any]]) -> None:
        """Keep what this page load produced, for a caller that asks for the same page.

        Bounded on the way in rather than trusted to stay small: the entries are whole payloads,
        and a process that lives for weeks would otherwise remember every page it ever read.
        Expired entries go first, then the oldest, which is also the least likely to be asked
        for again.
        """
        if not captured or not self._capture_ttl:
            return
        now = time.monotonic()
        self._cache[(retailer, url)] = (now, list(captured))
        for key, (read_at, _) in list(self._cache.items()):
            if now - read_at > self._capture_ttl:
                del self._cache[key]
        while len(self._cache) > _MAX_REMEMBERED_PAGES:
            del self._cache[min(self._cache, key=lambda key: self._cache[key][0])]

    def _reusable_capture(self, retailer: str, url: str) -> list[tuple[str, Any]] | None:
        """What a recent load of this exact page captured, while it is still fresh enough.

        `browser_capture_ttl_seconds` of 0 switches reuse off, and every ask becomes a load.
        """
        if not self._capture_ttl:
            return None
        entry = self._cache.get((retailer, url))
        if entry is None:
            return None
        read_at, captured = entry
        if time.monotonic() - read_at > self._capture_ttl:
            del self._cache[(retailer, url)]
            return None
        return list(captured)

    async def _navigate(
        self,
        retailer: str,
        url: str,
        settle_ms: int,
        timeout_ms: int,
        capture: Callable[[str], bool] | None,
        ready: Callable[[list[tuple[str, Any]]], bool] | None = None,
    ) -> Page:
        """The navigation itself. The caller holds this retailer's lock."""
        self._refuse_if_parked(retailer)
        if retailer in self._awaiting:
            # The one ask the interval allows. Stamped now, before the attempt, so that a load
            # which raises instead of reaching `_guard` does not leave the window open for the
            # next category to ask again -- which would be the repeated reloading of a
            # challenge this exists to prevent.
            self._parked_at[retailer] = time.monotonic()
        page = await self.page_for(retailer)
        self._blocked[retailer] = []
        self._captured[retailer] = []
        self._capture_match[retailer] = capture
        await self._pace(url)
        self._loads[retailer] = self._loads.get(retailer, 0) + 1
        # From here until the guard passes, this page is showing nothing anyone may keep
        # reading -- including if the load raises half-way through it.
        self._page_at.pop(retailer, None)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await self._settle(retailer, page, settle_ms, ready)
        await self._guard(retailer, page, status=getattr(response, "status", None))
        # Only a load that got past the guard is one a later caller may keep reading. The
        # navigation count is stamped with it, so any move the site makes afterwards ends that;
        # so is the moment, so that continuing cannot outlive the capture window either.
        self._page_at[retailer] = (url, self._navigations.get(retailer, 0), time.monotonic())
        return page

    async def _settle(
        self,
        retailer: str,
        page: Page,
        settle_ms: int,
        ready: Callable[[list[tuple[str, Any]]], bool] | None,
    ) -> None:
        """Give the page time to fetch its own data -- no more of it than it needs.

        `settle_ms` is the ceiling. Inside it the page's first-party responses are watched, and
        the wait ends as soon as either the caller's question is answered or those responses
        have gone quiet for `_SETTLE_QUIET_MS`. Quiet is what ends an ordinary load: a shelf
        that has arrived but whose stock is still to be scrolled for satisfies nobody's `ready`
        and would otherwise sit out the rest of the ceiling before the scrolling could start.

        A caller with no question to answer, and a page that has fetched nothing at all, both
        wait the full ceiling -- the second deliberately. Fetching nothing is what a challenge
        looks like from here, and calling one early would trade the fixed wait this replaces
        for a wrong verdict.
        """
        if not settle_ms:
            return
        if ready is None:
            await page.wait_for_timeout(settle_ms)
            await self._settle_watchers(retailer)
            return
        waited = 0
        quiet = 0
        seen = 0
        while waited < settle_ms:
            step = min(_SETTLE_POLL_MS, settle_ms - waited)
            await page.wait_for_timeout(step)
            waited += step
            await self._settle_watchers(retailer)
            captured = self._captured.get(retailer, [])
            if ready(list(captured)):
                return
            quiet = quiet + step if len(captured) == seen else 0
            seen = len(captured)
            if seen and quiet >= _SETTLE_QUIET_MS:
                return

    def _refuse_if_parked(self, retailer: str) -> None:
        """Hand back the standing verdict for a retailer whose challenge is too fresh to re-ask.

        Loading a challenge again is the one move that reliably makes things worse, and the
        categories still to come learn nothing by trying: each gets the same block and spends a
        refused page load on the session to find that out. So the verdict stands for
        `browser_challenge_retry_interval_seconds` -- long enough to cover the rest of a scrape
        -- and then one ask is allowed again, because by then a person may have finished it in
        the window that was left open.

        A fresh error carries the verdict rather than the stored instance being re-raised: one
        instance raised on every category of every run accumulates a traceback, and each frame
        holds this session and its page alive.
        """
        pending = self._awaiting.get(retailer)
        if pending is None:
            return
        if time.monotonic() - self._parked_at.get(retailer, 0.0) >= self._challenge_retry:
            return
        log.info(
            "browser_still_parked",
            extra={"retailer": retailer, "url": pending.url, "marker": pending.marker},
        )
        raise ManualVerificationRequiredError(
            retailer=pending.retailer, url=pending.url, marker=pending.marker
        )

    async def read_shelf(
        self,
        retailer: str,
        page: Page,
        *,
        enough: Callable[[list[tuple[str, Any]]], bool],
    ) -> list[tuple[str, Any]]:
        """Scroll a lazily-loaded shelf, taking this retailer's lock while doing so."""
        lock = self._nav_locks.setdefault(retailer, asyncio.Lock())
        async with lock:
            return await self._read_shelf(retailer, page, enough)

    async def _read_shelf(
        self,
        retailer: str,
        page: Page,
        enough: Callable[[list[tuple[str, Any]]], bool],
    ) -> list[tuple[str, Any]]:
        """Scroll a lazily-loaded shelf until it has given up what was asked for.

        Retailers load a category a screenful at a time, so a page that is never scrolled
        yields prices for the first handful of products and nothing for the rest. This reads
        it the way a person does: a screen at a time, pausing between, and **stopping the
        moment `enough` is satisfied** -- which on a short category is often immediately.

        The steps are small and the pauses vary a little because that is what reading looks
        like; a single jump from top to bottom is not. Nothing here clicks, and the page is
        never re-navigated: this is the page that was already loaded.
        """
        # The capture is already open -- it was armed by the navigation and stays armed until
        # the next one -- because scrolling is what fetches the rest of the shelf.
        captured = self.captured(retailer)
        if enough(captured):
            return captured
        seen = len(captured)
        idle = 0
        for step in range(self._scroll_max_steps):
            await page.mouse.wheel(0, self._scroll_step)
            # A little variation between steps; a metronome is not a reader.
            await page.wait_for_timeout(
                int((self._scroll_pause + random.uniform(0, self._scroll_jitter)) * 1000)
            )
            await self._settle_watchers(retailer)
            captured = self.captured(retailer)
            if enough(captured):
                log.debug("browser_shelf_read", extra={"retailer": retailer, "steps": step + 1})
                return captured
            # Nothing new for two steps running means the shelf has ended; scrolling an
            # exhausted page is pointless traffic.
            idle = idle + 1 if len(captured) == seen else 0
            seen = len(captured)
            if idle >= 2:
                break
        return captured

    def captured(self, retailer: str) -> list[tuple[str, Any]]:
        """The data calls the last `visit` collected, as (url, decoded JSON)."""
        return list(self._captured.get(retailer, []))

    async def _guard(self, retailer: str, page: Page, *, status: int | None = None) -> None:
        marker = await self._challenge_marker(page)
        if marker is None and self._blocked.get(retailer):
            # The page itself looks fine; its data calls were the ones turned away.
            marker = f"blocked data request ({self._blocked[retailer][0][:120]})"
        if marker is None and status == _TURNED_AWAY_STATUS:
            # The document itself was refused. A page that says nothing recognisable -- or
            # nothing at all -- would otherwise be scraped as though it were a shelf, and the
            # six categories behind it would each spend a load finding out the same thing.
            marker = f"document refused (HTTP {status})"
        if marker is None:
            self._parked_at.pop(retailer, None)
            if self._awaiting.pop(retailer, None) is not None:
                log.info("browser_verification_cleared", extra={"retailer": retailer})
            self._verified.add(retailer)
            log.debug(
                "browser_retailer_state",
                extra={"retailer": retailer, "state": MODE_VERIFIED_SESSION},
            )
            return
        pending = ManualVerificationRequiredError(retailer=retailer, url=page.url, marker=marker)
        self._awaiting[retailer] = pending
        self._parked_at[retailer] = time.monotonic()
        self._challenges[retailer] = self._challenges.get(retailer, 0) + 1
        self._verified.discard(retailer)
        log.info(
            "browser_retailer_state",
            extra={"retailer": retailer, "state": MODE_CHALLENGE_DETECTED, "marker": marker},
        )
        # Logged at error level because it is the one condition here a person must act on.
        log.error(
            "browser_manual_verification_required",
            extra={
                "retailer": retailer,
                "url": page.url,
                "marker": marker,
                "state": MODE_MANUAL_VERIFICATION_REQUIRED,
                "mode": self._mode,
            },
        )
        raise pending

    @staticmethod
    async def _challenge_marker(page: Page) -> str | None:
        """The challenge wording this page is showing, or None if it is showing data."""
        try:
            title = await page.title()
        except Exception:  # pragma: no cover - a page torn down mid-check
            return None
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        haystack = f"{title}\n{body[:4000]}".lower()
        for marker in CHALLENGE_MARKERS:
            if marker in haystack:
                return marker
        return None

    async def wait_for_manual_verification(
        self, retailer: str, *, poll_seconds: float | None = None
    ) -> bool:
        """Wait for a person to clear this retailer's challenge, without getting in their way.

        The window is `BROWSER_MANUAL_VERIFICATION_TIMEOUT_SECONDS` (ten minutes by default)
        and it is spent **watching, not touching**. A press-and-hold is a gesture held for
        several seconds and an image CAPTCHA is a puzzle read and answered; reloading the tab
        underneath either one throws the attempt away and starts the person over, which is
        worse than not helping at all. So nothing here navigates, reloads, or clicks while
        the window is open.

        What it watches instead, both free:

        * the page's own text -- a rendered challenge ("Robot or human?") simply stops
          saying so once it is answered, which is the whole signal for that shape;
        * the page's own navigations -- when a challenge is solved the site moves on by
          itself, and that is visible without asking it to.

        Only when the window has expired, and only for a challenge that never showed itself
        on the page -- one the data calls saw underneath a healthy-looking shell (Target's
        shape), or a document the origin refused without recognisable wording, both of which
        leave nothing in the DOM to change -- does it re-ask once to settle the answer.

        Only this retailer waits. Its siblings hold their own pages and their own locks, so
        a person taking ten minutes over Target costs Walmart nothing.

        Returns True once the retailer is answering normally, False if the window ran out --
        in which case the tab is left exactly as it is, so finishing the check later still
        counts: the profile is persistent and the session survives.
        """
        page = self._pages.get(retailer)
        pending = self._awaiting.get(retailer)
        if page is None or page.is_closed():
            return False
        interval = self._verification_poll if poll_seconds is None else poll_seconds
        # A challenge the page *displays* clears in the page: the wording simply stops being
        # there, and that is visible for free. One that was never in the page -- a data call
        # refused underneath a healthy-looking shell, or a document the origin refused with no
        # recognisable wording -- leaves a tab whose text will never change, so watching it
        # proves nothing and its answer has to be re-asked for. The test is whether the marker
        # is one of the rendered-text markers, because those are exactly the visible ones; a
        # marker this layer wrote itself never is.
        invisible = pending is not None and pending.marker not in CHALLENGE_MARKERS

        started = time.monotonic()
        deadline = started + self._verification_timeout
        log.warning(
            "browser_verification_window_open",
            extra={
                "retailer": retailer,
                "seconds": round(self._verification_timeout),
                "poll_seconds": interval,
                "mode": "passive: the page is watched, never reloaded",
                "url": page.url,
            },
        )

        seen_navigations = self._navigations.get(retailer, 0)
        last_report = started
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))
            if page.is_closed():
                log.info("browser_verification_page_closed", extra={"retailer": retailer})
                return False

            if time.monotonic() - last_report >= self._verification_report:
                last_report = time.monotonic()
                log.info(
                    "browser_verification_waiting",
                    extra={
                        "retailer": retailer,
                        "seconds_remaining": round(deadline - last_report),
                    },
                )

            navigations = self._navigations.get(retailer, 0)
            moved = navigations != seen_navigations
            if moved:
                # The site navigated itself, which is what solving one looks like. Read the
                # fresh page rather than the stale verdict: drop the old block evidence and
                # give its data calls a moment to answer.
                seen_navigations = navigations
                self._blocked[retailer] = []
                await page.wait_for_timeout(2500)
                await self._settle_watchers(retailer)

            if await self._challenge_marker(page) is not None:
                continue
            if invisible and (not moved or self._blocked.get(retailer)):
                # A clean-looking page proves nothing for these shapes until the site has
                # re-asked of its own accord and that attempt was not refused.
                continue
            self._awaiting.pop(retailer, None)
            self._parked_at.pop(retailer, None)
            log.info(
                "browser_verification_cleared",
                extra={"retailer": retailer, "seconds_waited": round(time.monotonic() - started)},
            )
            return True

        log.warning(
            "browser_verification_window_expired",
            extra={"retailer": retailer, "seconds": round(self._verification_timeout)},
        )
        if not invisible:
            # The page still shows the challenge and nothing else would tell us otherwise.
            return False
        return await self._recheck_after_window(retailer, page)

    async def _recheck_after_window(self, retailer: str, page: Page) -> bool:
        """One re-ask, only after the waiting window has closed.

        The single navigation this class performs on a parked page, and it happens when
        nobody can still be mid-gesture: the window is over either way.
        """
        log.info("browser_verification_recheck", extra={"retailer": retailer})
        lock = self._nav_locks.setdefault(retailer, asyncio.Lock())
        async with lock:
            self._blocked[retailer] = []
            self._page_at.pop(retailer, None)  # a reload leaves nothing anyone may keep reading
            try:
                await self._pace(page.url)
                self._loads[retailer] = self._loads.get(retailer, 0) + 1
                answered = await page.reload(wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2500)
                await self._settle_watchers(retailer)
            except Exception:
                log.warning("browser_verification_recheck_failed", extra={"retailer": retailer})
                return False
        # The status as well, and for the same reason the guard reads it: a document the origin
        # is still refusing renders the same nothing it rendered before, so the rendered text
        # cannot tell this apart from a page that came back.
        if getattr(answered, "status", None) == _TURNED_AWAY_STATUS:
            self._parked_at[retailer] = time.monotonic()
            return False
        if await self._challenge_marker(page) is not None or self._blocked.get(retailer):
            self._parked_at[retailer] = time.monotonic()
            return False
        self._awaiting.pop(retailer, None)
        self._parked_at.pop(retailer, None)
        log.info("browser_verification_cleared", extra={"retailer": retailer})
        return True
