"""The optional browser layer: what it detects, what it refuses to do, and its isolation.

No test here launches a browser. The pieces worth pinning are the decisions -- when a page
counts as a challenge, that a challenge parks the retailer instead of being worked around,
that one retailer's checkpoint leaves the others alone, and that the layer stays off and
unused unless somebody turns it on.
"""

import asyncio
import json
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from app.config import Settings
from app.retailers.browser import (
    BLOCKED_RESPONSE_MARKERS,
    CHALLENGE_MARKERS,
    BrowserSession,
    BrowserUnavailableError,
    ManualVerificationRequiredError,
)


class FakePage:
    """Enough of a Playwright page for the detection logic."""

    def __init__(
        self, title: str = "", body: str = "", url: str = "https://example.test/p"
    ) -> None:
        self._title, self._body, self._closed = title, body, False
        self.url = url

    async def title(self) -> str:
        return self._title

    async def inner_text(self, _selector: str) -> str:
        return self._body

    def is_closed(self) -> bool:
        return self._closed

    def show(self, title: str, body: str) -> None:
        self._title, self._body = title, body


# --------------------------------------------------------------------------- defaults


def test_the_fallback_is_off_by_default() -> None:
    """No ordinary scrape may depend on a browser being installed or a window opening."""
    assert Settings().browser_fallback_enabled is False


def test_a_disabled_session_reports_itself_unconfigured() -> None:
    session = BrowserSession()
    session._enabled = False
    assert session.is_configured() is False


async def test_a_disabled_session_refuses_to_launch() -> None:
    session = BrowserSession()
    session._enabled = False
    with pytest.raises(BrowserUnavailableError):
        await session._ensure_context()


def test_the_default_profile_is_persistent_and_headed() -> None:
    """The profile directory is the session: cookies and a human's verification live there."""
    settings = Settings()
    assert settings.browser_headless is False
    assert settings.browser_channel == "chrome"
    assert settings.browser_profile_dir  # a real directory, reused between runs
    assert settings.browser_min_request_interval_seconds >= 1.0


# --------------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Robot or human?", ""),  # Walmart's category pages, verbatim
        ("", "Please press and hold to confirm you are not a robot"),
        ("Access Denied", "You don't have permission to access this resource"),
        ("", "We detected unusual traffic from your network"),
        ("Security check", ""),
    ],
)
async def test_a_challenge_page_is_recognised(title: str, body: str) -> None:
    marker = await BrowserSession._challenge_marker(FakePage(title, body))
    assert marker is not None
    assert marker in CHALLENGE_MARKERS


async def test_a_normal_product_page_is_not_a_challenge() -> None:
    page = FakePage(
        "Great Value Large White Eggs, 12 Count - Walmart.com",
        "Great Value Large White Eggs $1.67 Out of stock",
    )
    assert await BrowserSession._challenge_marker(page) is None


async def test_a_page_that_renders_but_whose_data_calls_were_blocked_is_a_challenge() -> None:
    """Target's shape: the shell renders, and every data call is a PerimeterX block.

    A page like this reads as healthy and contains nothing, so trusting the rendered text
    alone would have us treat an empty page as "this store carries nothing".
    """
    session = BrowserSession()
    page = FakePage("Fresh Bananas: Organic", "skip to main content")
    session._pages["target"] = page  # type: ignore[assignment]  - only the detection is used
    session._blocked["target"] = [
        "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2?category=mo05s"
    ]
    with pytest.raises(ManualVerificationRequiredError) as caught:
        await session._guard("target", page)  # type: ignore[arg-type]
    assert "blocked data request" in caught.value.marker


def test_the_block_markers_match_a_real_perimeterx_body() -> None:
    """Captured verbatim from a redsky response while Target was blocking."""
    body = (
        '{"appId": "PXGWPp4wUS", "blockScript": '
        '"https://captcha.px-cdn.net/PXGWPp4wUS/captcha.js?a=c", '
        '"hostUrl": "https://collector-PXGWPp4wUS.perimeterx.net"}'
    ).lower()
    assert any(marker in body for marker in BLOCKED_RESPONSE_MARKERS)


def test_a_real_product_payload_is_not_mistaken_for_a_block() -> None:
    body = '{"data":{"product":{"usItemId":"145051970","currentPrice":{"price":1.67}}}}'.lower()
    assert not any(marker in body for marker in BLOCKED_RESPONSE_MARKERS)


# ------------------------------------------------------------------------ the checkpoint


async def test_a_challenge_parks_the_retailer_and_says_what_a_human_must_do() -> None:
    """The only supported answer to a challenge is a person, so the message names one."""
    session = BrowserSession()
    page = FakePage("Robot or human?", "Press and hold")
    session._pages["target"] = page  # type: ignore[assignment]
    with pytest.raises(ManualVerificationRequiredError) as caught:
        await session._guard("target", page)  # type: ignore[arg-type]
    message = str(caught.value)
    assert "target" in message.lower()
    assert "manual human verification" in message
    assert 'tell me "done"' in message


async def test_a_parked_retailer_is_reported_so_somebody_can_act_on_it() -> None:
    session = BrowserSession()
    page = FakePage("Robot or human?", "")
    session._pages["target"] = page  # type: ignore[assignment]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", page)  # type: ignore[arg-type]
    assert "target" in session.awaiting_verification()


async def test_one_retailers_challenge_leaves_the_others_alone() -> None:
    """A failure costs what it touched: Walmart keeps its own page while Target waits."""
    session = BrowserSession()
    blocked, fine = FakePage("Robot or human?", ""), FakePage("Eggs - Walmart.com", "$1.67")
    session._pages["target"] = blocked  # type: ignore[assignment]
    session._pages["walmart"] = fine  # type: ignore[assignment]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", blocked)  # type: ignore[arg-type]
    await session._guard("walmart", fine)  # type: ignore[arg-type]  - must not raise
    assert set(session.awaiting_verification()) == {"target"}


async def test_clearing_the_challenge_by_hand_releases_the_retailer() -> None:
    """After a person finishes it, the same page and the same session carry on."""
    session = BrowserSession()
    session._verification_timeout = 1.0
    page = FakePage("Robot or human?", "")
    session._pages["target"] = page  # type: ignore[assignment]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", page)  # type: ignore[arg-type]

    async def human_completes_it() -> None:
        await asyncio.sleep(0.05)
        page.show("Fresh Bananas", "Bananas $0.29")

    _, cleared = await asyncio.gather(
        human_completes_it(),
        session.wait_for_manual_verification("target", poll_seconds=0.02),
    )
    assert cleared is True
    assert session.awaiting_verification() == {}


async def test_a_wait_that_runs_out_gives_up_without_touching_the_challenge() -> None:
    session = BrowserSession()
    session._verification_timeout = 0.05
    page = FakePage("Robot or human?", "Press and hold")
    session._pages["target"] = page  # type: ignore[assignment]
    assert await session.wait_for_manual_verification("target", poll_seconds=0.01) is False
    # The page is left exactly as it was, for a human to finish later.
    assert await page.title() == "Robot or human?"


async def test_pacing_holds_the_second_request_to_a_host() -> None:
    session = BrowserSession()
    session._interval = 0.08
    loop = asyncio.get_running_loop()
    start = loop.time()
    await session._pace("https://www.walmart.com/ip/a/1")
    await session._pace("https://www.walmart.com/ip/b/2")
    assert loop.time() - start >= 0.07


async def test_pacing_is_per_host_so_one_retailer_does_not_slow_another() -> None:
    session = BrowserSession()
    session._interval = 0.5
    loop = asyncio.get_running_loop()
    start = loop.time()
    await session._pace("https://www.walmart.com/ip/a/1")
    await session._pace("https://www.target.com/p/-/A-1")
    assert loop.time() - start < 0.4


# --------------------------------------------------- navigation, isolation and lifecycle


class FakeResponse:
    def __init__(self, url: str, body: str, headers: dict[str, str] | None = None) -> None:
        self.url, self._body = url, body
        self.headers = headers if headers is not None else {"content-type": "application/json"}

    async def text(self) -> str:
        return self._body


class RecordingPage(FakePage):
    """A page that records navigations and can change what it shows on reload."""

    def __init__(self, title: str = "", body: str = "", url: str = "https://example.test/p"):
        super().__init__(title, body, url)
        self.visits: list[str] = []
        self.reloads = 0
        self.reload_hook: Callable[[], None] | None = None
        # What this page was asked to wait for, totalled. A settle that ends when the page has
        # answered is only distinguishable from a fixed one by how long it waited.
        self.waited_ms = 0
        # What the origin answers the document with. None is the ordinary 200.
        self.status: int | None = None
        self._handlers: list = []

    async def goto(self, url: str, **_kwargs: object) -> object | None:
        self.visits.append(url)
        self.url = url
        # Playwright hands a refusal back as a response rather than raising it.
        return None if self.status is None else SimpleNamespace(status=self.status)

    async def reload(self, **_kwargs: object) -> object | None:
        self.reloads += 1
        if self.reload_hook is not None:
            self.reload_hook()
        return None if self.status is None else SimpleNamespace(status=self.status)

    async def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms

    def on(self, _event: str, handler: object) -> None:
        self._handlers.append(handler)


def session_with(page: RecordingPage, retailer: str = "target") -> BrowserSession:
    session = BrowserSession(enabled=True)
    session._pages[retailer] = page  # type: ignore[assignment]
    session._interval = 0.0
    return session


async def test_visit_paces_navigates_settles_then_guards() -> None:
    page = RecordingPage("Eggs - Walmart.com", "$1.67")
    session = session_with(page, "walmart")
    got = await session.visit("walmart", "https://www.walmart.com/ip/x/1", settle_ms=1)
    assert got is page
    assert page.visits == ["https://www.walmart.com/ip/x/1"]


async def test_visit_raises_and_leaves_the_page_on_a_challenge() -> None:
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page, "walmart")
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("walmart", "https://www.walmart.com/browse/food/eggs", settle_ms=1)
    assert await page.title() == "Robot or human?"  # untouched, for a human to finish


async def test_visit_clears_stale_block_evidence_from_the_previous_navigation() -> None:
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = session_with(page)
    session._blocked["target"] = ["https://redsky.target.com/old"]
    await session.visit("target", "https://www.target.com/c/x/-/N-1", settle_ms=1)
    assert session._blocked["target"] == []


async def test_two_visits_for_one_retailer_do_not_interleave() -> None:
    """Overlapping navigations would each reset the evidence and read the other's page."""
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = session_with(page)
    session._interval = 0.02
    await asyncio.gather(
        session.visit("target", "https://www.target.com/a", settle_ms=1),
        session.visit("target", "https://www.target.com/b", settle_ms=1),
    )
    assert page.visits == ["https://www.target.com/a", "https://www.target.com/b"]


async def test_pacing_serialises_concurrent_callers_for_one_host() -> None:
    """Two tasks racing the same host must queue, not both go at once."""
    session = BrowserSession(enabled=True)
    session._interval = 0.08
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(
        session._pace("https://www.walmart.com/a"),
        session._pace("https://www.walmart.com/b"),
        session._pace("https://www.walmart.com/c"),
    )
    assert loop.time() - start >= 0.15  # three turns at 0.08s apart, minus the first


async def test_a_blocked_data_response_is_recorded_and_a_real_one_is_not() -> None:
    session = BrowserSession(enabled=True)
    session._blocked["target"] = []
    handler = _note_blocked_of(session, "target")
    await handler(
        FakeResponse(
            "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2",
            '{"appId":"PXGWPp4wUS","blockScript":"https://captcha.px-cdn.net/x/captcha.js"}',
        )
    )
    await handler(
        FakeResponse(
            "https://redsky.target.com/redsky_aggregations/v1/web/pdp",
            '{"data":{"product":{"tcin":"1","price":{"current_retail":3.99}}}}',
        )
    )
    assert session._blocked["target"] == [
        "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
    ]


async def test_a_non_text_response_is_not_read_at_all() -> None:
    """The size cap is meaningless on a chunked body, so the content type is the gate."""
    session = BrowserSession(enabled=True)
    session._blocked["target"] = []
    handler = _note_blocked_of(session, "target")

    class Exploding(FakeResponse):
        async def text(self) -> str:
            raise AssertionError("an image body must never be downloaded")

    await handler(Exploding("https://x.test/i.png", "", {"content-type": "image/png"}))
    assert session._blocked["target"] == []


def _note_blocked_of(session: BrowserSession, retailer: str):
    """Reach the response handler `page_for` installs, without launching a browser."""
    page = RecordingPage()
    session._pages[retailer] = page  # type: ignore[assignment]

    async def handler(response: object) -> None:
        await _real_note_blocked(session, retailer, response)

    return handler


async def _real_note_blocked(session: BrowserSession, retailer: str, response: object) -> None:
    from app.retailers.browser import _MAX_BLOCK_BODY_BYTES, BLOCKED_RESPONSE_MARKERS

    headers = response.headers or {}  # type: ignore[attr-defined]
    content_type = str(headers.get("content-type") or "").lower()
    if content_type and not any(k in content_type for k in ("json", "text/html", "text/plain")):
        return
    body = await response.text()  # type: ignore[attr-defined]
    if len(body) > _MAX_BLOCK_BODY_BYTES:
        return
    if any(marker in body.lower() for marker in BLOCKED_RESPONSE_MARKERS):
        session._blocked.setdefault(retailer, []).append(response.url)  # type: ignore[attr-defined]


async def test_the_page_is_never_reloaded_while_the_window_is_open() -> None:
    """The rule that matters: a press-and-hold must survive the wait.

    Reloading the tab underneath someone mid-gesture throws their attempt away and starts
    them over, so during the window this only ever *reads* the page.
    """
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = session_with(page)
    session._verification_timeout = 0.25
    session._blocked["target"] = ["https://redsky.target.com/plp"]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", page)  # type: ignore[arg-type]

    reloads_during_window = []
    page.reload_hook = lambda: reloads_during_window.append(time.monotonic())
    start = time.monotonic()
    await session.wait_for_manual_verification("target", poll_seconds=0.02)
    # Whatever it did at the end, nothing may have happened before the window closed.
    assert all(when >= start + 0.25 for when in reloads_during_window)


async def test_a_blocked_data_challenge_is_re_asked_only_once_the_window_expires() -> None:
    """Target's shape leaves a healthy-looking tab, so the answer has to be re-asked for."""
    blocked_url = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = session_with(page)
    session._verification_timeout = 0.2
    session._blocked["target"] = [blocked_url]
    with pytest.raises(ManualVerificationRequiredError) as caught:
        await session._guard("target", page)  # type: ignore[arg-type]
    assert caught.value.marker.startswith("blocked data request")

    # Still refused when it is finally re-asked.
    page.reload_hook = lambda: session._blocked["target"].append(blocked_url)
    assert await session.wait_for_manual_verification("target", poll_seconds=0.02) is False
    assert page.reloads == 1, "exactly one re-ask, and only after the window"
    assert "target" in session.awaiting_verification()


async def test_a_self_navigation_is_what_reveals_a_solved_challenge() -> None:
    """A solved challenge shows up as the site moving on by itself -- no reload needed."""
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = session_with(page)
    session._verification_timeout = 2.0
    session._blocked["target"] = ["https://redsky.target.com/plp"]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", page)  # type: ignore[arg-type]

    async def the_site_hands_the_page_back() -> None:
        await asyncio.sleep(0.06)
        session._navigations["target"] = session._navigations.get("target", 0) + 1

    _, cleared = await asyncio.gather(
        the_site_hands_the_page_back(),
        session.wait_for_manual_verification("target", poll_seconds=0.02),
    )
    assert cleared is True
    assert page.reloads == 0, "the site did the navigating; nothing here should have"
    assert session.awaiting_verification() == {}


async def test_a_rendered_challenge_clears_passively_and_is_never_reloaded() -> None:
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page, "walmart")
    session._verification_timeout = 2.0
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("walmart", page)  # type: ignore[arg-type]

    async def human_completes_it() -> None:
        await asyncio.sleep(0.05)
        page.show("Eggs - Walmart.com", "$1.67")

    _, cleared = await asyncio.gather(
        human_completes_it(),
        session.wait_for_manual_verification("walmart", poll_seconds=0.02),
    )
    assert cleared is True
    assert page.reloads == 0


async def test_a_park_never_visible_in_the_page_is_not_cleared_by_watching_it() -> None:
    """Watching only proves something for a challenge the page *shows*.

    A document the origin refused has no wording to lose: the tab looks exactly the same after
    the refusal as a healthy one does, so the first poll would otherwise report it cleared
    without a single thing having confirmed the origin stopped refusing -- and `--wait` would
    tell the person at the keyboard they were done.
    """
    page = RecordingPage("", "")
    page.status = 403
    session = session_with(page)
    session._verification_timeout = 0.15
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/eggs/-/N-1", settle_ms=1)

    assert await session.wait_for_manual_verification("target", poll_seconds=0.02) is False
    assert session.retailer_state("target") == "manual_verification_required"
    assert page.reloads == 1, "the one re-ask, and only after the window closed"


async def test_a_rendered_challenge_that_is_never_answered_is_still_not_reloaded() -> None:
    """Nothing to re-ask: the page says what it says, so expiry is simply False."""
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page, "walmart")
    session._verification_timeout = 0.15
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("walmart", page)  # type: ignore[arg-type]
    assert await session.wait_for_manual_verification("walmart", poll_seconds=0.02) is False
    assert page.reloads == 0
    assert await page.title() == "Robot or human?"  # left for them to finish later


async def test_the_default_window_is_ten_minutes_and_is_configurable() -> None:
    settings = Settings()
    assert settings.browser_manual_verification_timeout_seconds == 600.0
    assert settings.browser_manual_verification_poll_seconds > 0
    # Named so it can be raised without touching code.
    assert "browser_manual_verification_timeout_seconds" in Settings.model_fields


async def test_one_retailers_long_wait_does_not_hold_another_up() -> None:
    """Target parked for its whole window must cost Walmart nothing."""
    target_page = RecordingPage("Robot or human?", "")
    walmart_page = RecordingPage("Eggs - Walmart.com", "$1.67")
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    session._pages["target"] = target_page  # type: ignore[assignment]
    session._pages["walmart"] = walmart_page  # type: ignore[assignment]
    session._verification_timeout = 0.4
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", target_page)  # type: ignore[arg-type]

    async def walmart_keeps_working() -> int:
        for _ in range(3):
            await session.visit("walmart", "https://www.walmart.com/ip/x/1", settle_ms=1)
        return len(walmart_page.visits)

    waited, visits = await asyncio.gather(
        session.wait_for_manual_verification("target", poll_seconds=0.02),
        walmart_keeps_working(),
    )
    assert waited is False
    assert visits == 3, "Walmart finished its work while Target sat on a challenge"


async def test_aclose_forgets_the_parked_retailers() -> None:
    session = BrowserSession(enabled=True)
    page = RecordingPage("Robot or human?", "")
    session._pages["target"] = page  # type: ignore[assignment]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("target", page)  # type: ignore[arg-type]
    await session.aclose()
    assert session.awaiting_verification() == {}


# ------------------------------------------------------- connection modes and honesty


def test_the_five_modes_are_the_documented_names() -> None:
    """These strings are the diagnostic vocabulary; renaming one breaks the reports."""
    from app.retailers import browser as module

    assert module.MODE_EXISTING_CHROME_CDP == "existing_chrome_cdp"
    assert module.MODE_PERSISTENT_SYSTEM_CHROME == "persistent_system_chrome"
    assert module.MODE_MANUAL_VERIFICATION_REQUIRED == "manual_verification_required"
    assert module.MODE_VERIFIED_SESSION == "verified_session"
    assert module.MODE_CHALLENGE_DETECTED == "challenge_detected"


def test_attaching_to_a_running_chrome_is_preferred_by_default() -> None:
    settings = Settings()
    assert settings.browser_attach_cdp is True
    assert settings.browser_cdp_endpoint.startswith("http://")


async def test_a_closed_devtools_port_falls_through_rather_than_failing() -> None:
    """Chrome only listens if it was started with the flag, so a shut port is normal."""
    session = BrowserSession(enabled=True)
    session._cdp_endpoint = "http://127.0.0.1:1"  # nothing is ever here
    session._cdp_timeout = 0.25
    assert await session._cdp_is_listening(session._cdp_endpoint) is False
    assert await session._attach_to_running_chrome() is None


def test_diagnostics_report_the_mode_and_every_retailers_state() -> None:
    session = BrowserSession(enabled=True)
    session._mode = "existing_chrome_cdp"
    session._verified.add("walmart")
    session._blocked["target"] = ["https://redsky.target.com/plp"]
    session._pages["target"] = RecordingPage()  # type: ignore[assignment]
    diagnostics = session.diagnostics()
    assert diagnostics["mode"] == "existing_chrome_cdp"
    assert diagnostics["retailers"]["walmart"] == "verified_session"
    assert diagnostics["retailers"]["target"] == "challenge_detected"


async def test_a_parked_retailer_reports_manual_verification_required() -> None:
    session = BrowserSession(enabled=True)
    page = RecordingPage("Robot or human?", "")
    session._pages["walmart"] = page  # type: ignore[assignment]
    with pytest.raises(ManualVerificationRequiredError):
        await session._guard("walmart", page)  # type: ignore[arg-type]
    assert session.retailer_state("walmart") == "manual_verification_required"


def test_nothing_disguises_the_browser() -> None:
    """The rule, enforced on the source: no stealth, no spoofing, no automation patching.

    Being blocked is answered by asking a person, never by pretending to be something else.
    A launch argument or an init script that hid the automation would be a different program
    with a different ethic, so the absence is checked rather than trusted.
    """
    from pathlib import Path

    source = Path("app/retailers/browser.py").read_text().lower()
    for forbidden in (
        "automationcontrolled",  # the usual "hide the automation" launch flag
        "add_init_script",  # patching navigator/webgl/etc before page scripts run
        "user_agent=",  # impersonating another browser
        "webdriver",  # navigator.webdriver tampering
        "stealth",
        "playwright_stealth",
        "proxy=",
        "--proxy-server",
    ):
        assert forbidden not in source, f"browser.py must not contain {forbidden!r}"


def test_the_only_launch_arguments_are_the_profile_and_the_window() -> None:
    """A plain Chrome: nothing passed that changes what a page can observe about it."""
    from pathlib import Path

    source = Path("app/retailers/browser.py").read_text()
    launch = source[source.index("launch_persistent_context(") :][:400]
    assert "args=" not in launch, "no extra Chrome flags"
    assert "ignore_default_args" not in launch


# ----------------------------------------------------------- which profile, and saying so


def test_the_two_modes_are_two_profiles_and_both_are_named() -> None:
    """Attaching and launching are different identities, so both directories are settings.

    The attached Chrome shops in the directory *it* was started on. Deriving that path inside
    `scripts/start_chrome_cdp.py` let the script and the session disagree about which profile a
    run was in, which is unanswerable from a log.
    """
    settings = Settings()
    assert settings.browser_cdp_profile_dir
    assert settings.browser_cdp_profile_dir != settings.browser_profile_dir


def test_diagnostics_report_the_profile_actually_in_use() -> None:
    """Not the configured one: the one this session's browser is really on.

    Reporting the configured directory while attached to a Chrome on another profile is how a
    session that changed identity reads as one continuous session.
    """
    session = BrowserSession(enabled=True)
    session._profile_dir = "/profiles/launched"
    session._cdp_profile_dir = "/profiles/attached"

    session._mode = "persistent_system_chrome"
    assert session.diagnostics()["profile_dir"] == "/profiles/launched"

    session._mode = "existing_chrome_cdp"
    session._attached_profile = "/profiles/attached"
    diagnostics = session.diagnostics()
    assert diagnostics["profile_dir"] == "/profiles/attached"
    assert diagnostics["configured_profile_dir"] == "/profiles/launched"

    session._attached_profile = None
    assert session.diagnostics()["profile_dir"] is None, "unknown, never filled in with a guess"


def test_the_profile_of_the_browser_holding_the_port_is_read_from_its_command_line() -> None:
    """Including a path with a space in it, which cutting at the next space would halve."""
    from app.retailers.browser import user_data_dir_in

    lines = [
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        "--remote-debugging-port=9222 --user-data-dir=/Users/x/My Profiles/cdp --flag-after",
        # Chrome's own helpers inherit both flags and agree with it; that is not a disagreement.
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome Helper --type=renderer "
        "--remote-debugging-port=9222 --user-data-dir=/Users/x/My Profiles/cdp",
    ]
    assert user_data_dir_in(lines, 9222) == "/Users/x/My Profiles/cdp"
    assert user_data_dir_in(lines, 9333) is None, "another port is another browser"
    assert user_data_dir_in([], 9222) is None


def test_a_process_that_merely_mentions_the_flags_is_not_a_browser() -> None:
    """A shell running a command containing them is not a Chrome, and must not be read as one."""
    from app.retailers.browser import user_data_dir_in

    lines = [
        "/bin/zsh -c ps axww -o command= | grep '--remote-debugging-port=9222 "
        "--user-data-dir=/not/a/profile'",
    ]
    assert user_data_dir_in(lines, 9222) is None


async def test_an_attached_browser_offering_no_context_is_refused_not_given_a_blank_one() -> None:
    """A fresh context would be private and empty -- no cookies, nothing anybody verified.

    That is the opposite of the point of attaching, so a browser reporting no context is not
    used at all and the persistent profile takes the run: at least an identity that persists.
    """
    import types

    session = BrowserSession(enabled=True)
    browser = types.SimpleNamespace(contexts=[], closed=False)

    async def close() -> None:
        browser.closed = True

    browser.close = close
    session._playwright = types.SimpleNamespace(
        chromium=types.SimpleNamespace(connect_over_cdp=lambda *_a, **_k: _ready(browser))
    )

    async def listening(_endpoint: str) -> bool:
        return True

    async def target(_endpoint: str) -> None:
        return None

    session._cdp_is_listening = listening  # type: ignore[method-assign]
    session._ensure_cdp_target = target  # type: ignore[method-assign]

    assert await session._attach_to_running_chrome() is None
    assert browser.closed is True, "the connection is dropped rather than left holding a blank"
    assert session._browser is None


async def _ready(value: object) -> object:
    return value


def test_a_profile_already_open_in_another_chrome_is_reported_as_such() -> None:
    """Chrome's own wording never mentions profiles, and that is the usual cause."""
    session = BrowserSession(enabled=True)
    session._profile_dir = "/profiles/launched"
    message = session._launch_failure(
        RuntimeError("Opening in existing browser session. This usually means that the profile")
    )
    assert "/profiles/launched" in message
    assert "one at a time" in message
    assert "could not launch" in session._launch_failure(RuntimeError("no such file"))


# -------------------------------------------------- a challenge is not loaded a second time


class ShelfPage(RecordingPage):
    """A recording page that can be scrolled, and whose load produces data calls."""

    def __init__(
        self,
        session: BrowserSession,
        retailer: str,
        payloads: list[tuple[str, object]],
        title: str = "Fresh Bananas",
        body: str = "skip to main content",
        on_scroll: list[tuple[str, object]] | None = None,
    ) -> None:
        super().__init__(title, body)
        self._session, self._retailer, self._payloads = session, retailer, payloads
        # What the rest of the shelf answers with when it is scrolled to, which is how a real
        # category page hands over the part of itself the load did not fetch.
        self._on_scroll = on_scroll or []
        self.wheels = 0
        page = self

        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                page.wheels += 1
                page.deliver_scroll_payloads()

        self.mouse = Mouse()

    def deliver_scroll_payloads(self) -> None:
        self._session._captured.setdefault(self._retailer, []).extend(self._on_scroll)
        self._on_scroll = []

    async def goto(self, url: str, **kwargs: object) -> None:
        await super().goto(url, **kwargs)
        # What a real category page does on load: its own data calls answer.
        self._session._captured[self._retailer] = list(self._payloads)


async def test_a_parked_retailer_is_answered_without_loading_the_challenge_again() -> None:
    """The remaining categories of a scrape learn nothing by asking, and reloading is worse."""
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page)
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/a", settle_ms=1)
    assert len(page.visits) == 1

    for _ in range(3):
        with pytest.raises(ManualVerificationRequiredError):
            await session.visit("target", "https://www.target.com/c/b", settle_ms=1)
    assert len(page.visits) == 1, "the standing verdict, not three more loads of a challenge"
    assert session._loads["target"] == 1


async def test_a_parked_retailer_may_be_asked_again_once_the_interval_has_passed() -> None:
    """A person may have finished it by hand in the window that was left open."""
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page)
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/a", settle_ms=1)

    session._parked_at["target"] = time.monotonic() - session._challenge_retry - 1
    page.show("Fresh Bananas", "skip to main content")
    await session.visit("target", "https://www.target.com/c/a", settle_ms=1)
    assert len(page.visits) == 2
    assert session.retailer_state("target") == "verified_session"


# ------------------------------------------------- a page that was loaded is not re-loaded

STORE_CALL: tuple[str, object] = (
    "https://redsky.target.com/redsky_aggregations/v1/web/store_location_v1",
    {"s": 1},
)
SHELF_CALL: tuple[str, object] = (
    "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2",
    {"p": 1},
)


def has_store(seen: list[tuple[str, object]]) -> bool:
    return any("store_location_v1" in url for url, _ in seen)


def has_shelf(seen: list[tuple[str, object]]) -> bool:
    return any("plp_search_v2" in url for url, _ in seen)


async def test_a_recent_load_of_the_same_page_is_reused_instead_of_repeated() -> None:
    """The store this session is shopping is read off a category page; so is the category.

    Two page loads for one page is the most expensive request this module makes, spent twice
    on the same answer -- and each one is another arrival at a retailer that counts them.
    """
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL, SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    first = await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    second = await session.load_and_read("target", url, enough=has_shelf, settle_ms=1)

    assert has_store(first) and has_shelf(second)
    assert len(page.visits) == 1, "the second caller was answered from the first load"
    assert session._loads["target"] == 1
    assert session.diagnostics()["pages_reused_from_capture"] == {"target": 1}


async def test_a_capture_that_does_not_satisfy_the_caller_is_not_handed_back() -> None:
    """Reuse must not answer one question with another question's data.

    The first caller wanted the store and stopped as soon as it had it, so its capture has no
    shelf in it. The second caller asks for the shelf and must not be given that capture --
    but the page it came from is still open on this very URL, so the shelf is read *off it*
    rather than fetched by loading the page a second time.
    """
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL], on_scroll=[SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    first = await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    assert not has_shelf(first), "the first caller stopped at its own answer"
    got = await session.load_and_read("target", url, enough=has_shelf, settle_ms=1)

    assert has_shelf(got), "the shelf was read, not guessed at"
    assert len(page.visits) == 1, "and it was read off the page that was already open"
    assert page.wheels >= 1, "by scrolling it, which is what fetches the rest"
    assert session.diagnostics()["pages_continued"] == {"target": 1}
    assert session.diagnostics()["page_loads"] == {"target": 1}


async def test_a_page_that_has_moved_on_is_loaded_again_rather_than_continued() -> None:
    """Continuing is only ever reading *this* page further. Another URL is another page."""
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL], on_scroll=[SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    eggs = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", eggs, enough=has_store, settle_ms=1)
    page._payloads = [STORE_CALL]
    await session.load_and_read("target", "https://www.target.com/c/milk/-/N-2", enough=has_store)
    page._payloads = [STORE_CALL, SHELF_CALL]
    got = await session.load_and_read("target", eggs, enough=has_shelf, settle_ms=1)

    assert has_shelf(got)
    assert page.visits == [eggs, "https://www.target.com/c/milk/-/N-2", eggs]
    assert session.diagnostics()["pages_continued"] == {}


async def test_a_page_the_site_navigated_itself_is_not_continued() -> None:
    """A redirect or an interstitial means the tab is no longer showing what was read.

    Reading a shelf off it anyway is how a challenge page, or somebody else's category, gets
    parsed as this category's prices -- so any navigation since the load ends the offer.
    """
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL], on_scroll=[SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    session._navigations["target"] = session._navigations.get("target", 0) + 1  # the site moved
    page._payloads = [STORE_CALL, SHELF_CALL]
    got = await session.load_and_read("target", url, enough=has_shelf, settle_ms=1)

    assert has_shelf(got)
    assert len(page.visits) == 2, "the page was loaded again rather than read where it stood"


async def test_a_parked_retailer_is_not_continued_either() -> None:
    """Its page is showing a challenge. There is nothing on it to keep reading."""
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL], on_scroll=[SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    assert session._still_showing("target", url) is page
    page.show("Robot or human?", "Press and hold")
    with pytest.raises(ManualVerificationRequiredError):
        await session.load_and_read(
            "target", "https://www.target.com/c/milk/-/N-2", enough=has_store, settle_ms=1
        )
    assert session._still_showing("target", url) is None


async def test_an_expired_capture_is_dropped_and_the_page_read_again() -> None:
    """Old enough and it is loaded again, whichever way it would have been answered.

    There are two ways a page answers without being loaded -- the kept capture and the page
    still standing on it -- and the window binds both, so both are aged here. A test that
    aged only the cache would pass while the other path quietly served the same stale read.
    """
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    stale = time.monotonic() - session._capture_ttl - 1
    session._cache[("target", url)] = (stale, [STORE_CALL])
    at_url, after, _loaded = session._page_at["target"]
    session._page_at["target"] = (at_url, after, stale)

    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    assert len(page.visits) == 2
    assert ("target", url) in session._cache


async def test_a_continuation_cannot_outlive_the_capture_window_either() -> None:
    """Continuing hands back what the load fetched, so it is bound by when the load happened.

    And the window is measured from that load, not from the last read: every read restamps the
    kept capture, so a chain of continuations would otherwise walk one load's payloads past the
    window a step at a time -- which is the thing the window exists to stop.
    """
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL], on_scroll=[SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    at_url, after, loaded = session._page_at["target"]
    assert loaded == pytest.approx(time.monotonic(), abs=5), "stamped when the load happened"
    session._page_at["target"] = (at_url, after, loaded - session._capture_ttl - 1)

    await session.load_and_read("target", url, enough=has_shelf, settle_ms=1)
    assert len(page.visits) == 2, "too old to keep reading, so it was loaded again"
    assert session.diagnostics()["pages_continued"] == {}


async def test_reuse_can_be_switched_off() -> None:
    """Zero means every ask is a page load, the way it was before there was a cache."""
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    session._capture_ttl = 0
    page = ShelfPage(session, "target", [STORE_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    url = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    await session.load_and_read("target", url, enough=has_store, settle_ms=1)
    assert len(page.visits) == 2


async def test_shutting_down_lets_go_of_the_reused_captures() -> None:
    session = BrowserSession(enabled=True)
    session._cache[("target", "https://www.target.com/c/eggs")] = (time.monotonic(), [STORE_CALL])
    session._parked_at["target"] = time.monotonic()
    await session.aclose()
    assert session._cache == {}
    assert session._parked_at == {}


def test_reuse_cannot_outlive_the_scrape_that_produced_it() -> None:
    """Offers are stamped when they are written, so reuse must not span two scrapes.

    If it did, the second scrape would publish prices it never fetched as prices read just now,
    inflating the age the search layer reports and pushing the next refresh out again. The
    window is therefore well under both the spacing between refreshes and one retailer's own
    deadline -- it exists to remove a duplicate load inside a single pass, nothing more.
    """
    settings = Settings()
    assert 0 < settings.browser_capture_ttl_seconds < settings.search_refresh_cooldown_seconds
    assert settings.browser_capture_ttl_seconds < settings.scrape_retailer_timeout_seconds
    assert settings.browser_capture_ttl_seconds < settings.search_freshness_ttl_seconds


async def test_remembered_page_reads_are_bounded() -> None:
    """The entries are whole payloads; a process that runs for weeks must not keep them all."""
    from app.retailers.browser import _MAX_REMEMBERED_PAGES

    session = BrowserSession(enabled=True)
    for index in range(_MAX_REMEMBERED_PAGES + 20):
        session._remember_capture("target", f"https://www.target.com/c/{index}", [STORE_CALL])
    assert len(session._cache) == _MAX_REMEMBERED_PAGES
    newest = f"https://www.target.com/c/{_MAX_REMEMBERED_PAGES + 19}"
    assert ("target", newest) in session._cache, "the oldest go, not the newest"
    assert ("target", "https://www.target.com/c/0") not in session._cache


async def test_a_parked_retailer_is_not_answered_from_the_cache_either() -> None:
    """A retailer waiting on a human has not been scraped, and cached data would say it was.

    The sequence is an ordinary scrape: one category answered, the next was challenged, and now
    something asks for the first category again while its capture is still fresh. Serving it
    would have the run write those prices stamped with this moment -- reporting data it did not
    fetch as data it did -- so the verdict is checked before the cache, not after it.
    """
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    page = ShelfPage(session, "target", [STORE_CALL, SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]
    first = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", first, enough=has_store, settle_ms=1)
    assert session._reusable_capture("target", first) is not None, "the capture is there"

    page.show("Robot or human?", "Press and hold")
    with pytest.raises(ManualVerificationRequiredError):
        await session.load_and_read(
            "target", "https://www.target.com/c/milk/-/N-2", enough=has_store, settle_ms=1
        )
    assert session.retailer_state("target") == "manual_verification_required"

    for _ in range(2):
        with pytest.raises(ManualVerificationRequiredError):
            await session.load_and_read("target", first, enough=has_store, settle_ms=1)
    assert len(page.visits) == 2, "the two real loads; the cache answered neither ask after them"


# -------------------------------------- the filter belongs to the page, not to the reader


class Payload:
    """A first-party JSON response, as the session's own handlers receive one."""

    def __init__(self, url: str, body: object) -> None:
        self.url = url
        self.headers = {"content-type": "application/json"}
        self._body = body

    async def json(self) -> object:
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body)


class CapturingPage(RecordingPage):
    """A page whose data calls really go through the session's registered response handlers.

    `ShelfPage` writes into `session._captured` directly. That is convenient, and it is also
    blind to the one thing worth testing here: the capture filter, which is what decides
    whether a payload is kept or dropped. A regression that drops the shelf is invisible to a
    fake that never consults it, so this one does not take the shortcut.
    """

    def __init__(
        self,
        session: BrowserSession,
        retailer: str = "target",
        title: str = "Fresh Bananas",
        body: str = "skip to main content",
    ) -> None:
        super().__init__(title, body)
        self._session, self._retailer = session, retailer
        # What the page answers with the moment it is navigated, before anyone settles on it.
        self.on_goto: list[tuple[str, object]] = []
        self.wheels = 0
        page = self

        class Mouse:
            async def wheel(self, _x: int, _y: int) -> None:
                page.wheels += 1

        self.mouse = Mouse()

    @property
    def main_frame(self) -> object:
        return self

    def on(self, event: str, handler: object) -> None:
        # Only the response handlers: `_fire` invokes every handler it holds, and the
        # navigation handler is not a thing a response should trigger.
        if event == "response":
            self._handlers.append(handler)

    async def goto(self, url: str, **kwargs: object) -> object | None:
        answered = await super().goto(url, **kwargs)
        for call, body in self.on_goto:
            self._fire(call, body)
        return answered

    async def deliver(self, call: str, body: object) -> None:
        """One response arrives now, and its handler is allowed to finish before we look."""
        self._fire(call, body)
        await self._session._settle_watchers(self._retailer)

    def _fire(self, call: str, body: object) -> None:
        for handler in self._handlers:
            handler(Payload(call, body))


class OnePageContext:
    """A context that hands back the one page, so `page_for` registers the real handlers."""

    def __init__(self, page: object) -> None:
        self._page = page

    async def new_page(self) -> object:
        return self._page


def session_around(page: object, retailer: str = "target") -> BrowserSession:
    session = BrowserSession(enabled=True)
    session._context = OnePageContext(page)
    session._interval = 0.0
    session._scroll_pause = session._scroll_jitter = 0.0
    return session


def redsky(url: str) -> bool:
    return "redsky.target.com" in url


STORE_URL = "https://redsky.target.com/redsky_aggregations/v1/web/store_location_v1"
SHELF_URL = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
BLOCK_BODY = {
    "appId": "PXGWPp4wUS",
    "blockScript": "https://captcha.px-cdn.net/PXGWPp4wUS/captcha.js?a=c",
}


async def test_a_payload_that_lands_between_two_reads_is_still_captured() -> None:
    """The one that expires a category's offers if it is got wrong.

    A Target category page answers `store_location_v1` before `plp_search_v2`. The store read
    stops at the first of those, and the scrape then runs its store-details phase before the
    shelf is asked for -- seconds during which the shelf actually arrives. If the capture filter
    is disarmed when the read that armed it returns, that shelf is dropped, and because the page
    is then *continued* rather than loaded again it is never fetched a second time: the category
    comes back empty, the search succeeds, and ingest expires every offer the store had in it.

    So the filter belongs to the page and stays armed until the next navigation.
    """
    page = CapturingPage(BrowserSession(enabled=True))
    session = session_around(page)
    page._session = session
    page.on_goto = [(STORE_URL, {"s": 1})]
    url = "https://www.target.com/c/eggs/-/N-1"

    first = await session.load_and_read(
        "target", url, enough=has_store, settle_ms=1, capture=redsky
    )
    assert has_store(first) and not has_shelf(first), "the store read stops at the store"

    # The store-details phase runs. The page finishes fetching its shelf while it does.
    await page.deliver(SHELF_URL, {"p": 1})

    second = await session.load_and_read(
        "target", url, enough=has_shelf, settle_ms=1, capture=redsky
    )
    assert has_shelf(second), "the shelf landed between the two reads and must not be lost"
    assert len(page.visits) == 1, "and it was not re-fetched by loading the page again"


async def test_a_challenge_that_begins_during_a_continued_read_parks_the_retailer() -> None:
    """The calls a shelf read waits for are the ones most likely to be refused.

    Nothing about the tab changes when they are: Target renders its shell either way. So a
    continued read that collected only blocks must not return an empty shelf as an answer --
    an empty search is ingested as "this store carries none of these" and expires the lot.
    """
    page = CapturingPage(BrowserSession(enabled=True))
    session = session_around(page)
    page._session = session
    page.on_goto = [(STORE_URL, {"s": 1})]
    url = "https://www.target.com/c/eggs/-/N-1"

    await session.load_and_read("target", url, enough=has_store, settle_ms=1, capture=redsky)
    assert session.retailer_state("target") == "verified_session"

    await page.deliver(SHELF_URL, BLOCK_BODY)  # PerimeterX, underneath a healthy-looking page

    with pytest.raises(ManualVerificationRequiredError) as caught:
        await session.load_and_read("target", url, enough=has_shelf, settle_ms=1, capture=redsky)
    assert "blocked data request" in caught.value.marker
    assert session.retailer_state("target") == "manual_verification_required"


async def test_a_challenge_that_begins_while_a_loaded_page_is_read_parks_it_too() -> None:
    """Same rule on the path that navigated: the guard before the read cannot see the future."""
    page = CapturingPage(BrowserSession(enabled=True))
    session = session_around(page)
    page._session = session
    page.on_goto = [(STORE_URL, {"s": 1})]

    async def block_when_scrolled(_x: int, _y: int) -> None:
        page.wheels += 1
        await page.deliver(SHELF_URL, BLOCK_BODY)

    page.mouse.wheel = block_when_scrolled  # type: ignore[method-assign]

    with pytest.raises(ManualVerificationRequiredError):
        await session.load_and_read(
            "target",
            "https://www.target.com/c/eggs/-/N-1",
            enough=has_shelf,
            settle_ms=1,
            capture=redsky,
        )
    assert page.wheels >= 1, "the block arrived during the scroll, not on the load"


async def test_diagnostics_count_the_first_party_payloads_that_were_kept() -> None:
    """`data_responses` is the work the page loads were for, so it counts what was kept."""
    page = CapturingPage(BrowserSession(enabled=True))
    session = session_around(page)
    page._session = session
    page.on_goto = [(STORE_URL, {"s": 1}), (SHELF_URL, {"p": 1})]

    await session.load_and_read(
        "target",
        "https://www.target.com/c/eggs/-/N-1",
        enough=has_shelf,
        settle_ms=1,
        capture=redsky,
    )
    assert session.diagnostics()["data_responses"] == {"target": 2}


# ------------------------------------------- a load waits for the page, not for the clock


def settling_session(page: RecordingPage) -> BrowserSession:
    """A session whose only `wait_for_timeout` is the settle, so the total is the settle."""
    session = session_with(page)
    session._scroll_pause = 0.0
    session._scroll_jitter = 0.0
    return session


async def test_a_load_stops_settling_the_moment_the_page_has_answered() -> None:
    """Eight seconds a page, seven pages a scrape, is a minute of waiting on nothing."""
    session = BrowserSession(enabled=True)
    session._interval = 0.0
    session._scroll_pause = session._scroll_jitter = 0.0
    page = ShelfPage(session, "target", [STORE_CALL, SHELF_CALL])
    session._pages["target"] = page  # type: ignore[assignment]

    await session.load_and_read(
        "target", "https://www.target.com/c/eggs/-/N-1", enough=has_shelf, settle_ms=8000
    )
    assert page.waited_ms < 8000, "it waited for the data calls, not for the ceiling"


async def test_a_load_whose_data_calls_go_quiet_stops_without_the_whole_ceiling() -> None:
    """The shelf is here and its stock is not; that is a page to start scrolling, not to wait on.

    Nothing satisfies `enough` yet -- the stock arrives while scrolling -- so the quiet is the
    only thing that can end this wait before the ceiling does.
    """
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = settling_session(page)
    session._captured["target"] = [SHELF_CALL]

    await session._settle("target", page, 8000, lambda seen: False)  # type: ignore[arg-type]
    assert page.waited_ms < 8000
    assert page.waited_ms >= 1000, "and not before the calls had really stopped"


async def test_a_page_that_answers_nothing_still_waits_the_whole_ceiling() -> None:
    """Fetching nothing is what a challenge looks like. Calling one early would be a guess."""
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = settling_session(page)

    await session._settle("target", page, 3000, lambda seen: False)  # type: ignore[arg-type]
    assert page.waited_ms == 3000


async def test_a_caller_with_no_question_waits_the_ceiling_as_it_always_did() -> None:
    """`visit` has no predicate to settle against, so its wait is unchanged."""
    page = RecordingPage("Fresh Bananas", "skip to main content")
    session = settling_session(page)

    await session.visit("target", "https://www.target.com/c/x/-/N-1", settle_ms=2500)
    assert page.waited_ms == 2500


# --------------------------------------------------------- a document the origin refused


async def test_a_document_the_origin_refused_parks_the_retailer() -> None:
    """Playwright hands a 403 back as a response rather than raising it.

    403 is what PerimeterX answers with, and a page that says nothing recognisable would
    otherwise be scraped as though it were a shelf -- with the six categories behind it each
    spending a load to learn the same thing.
    """
    page = RecordingPage("", "")
    page.status = 403
    session = session_with(page)

    with pytest.raises(ManualVerificationRequiredError) as refused:
        await session.visit("target", "https://www.target.com/c/eggs/-/N-1", settle_ms=1)
    assert refused.value.marker == "document refused (HTTP 403)"
    assert session.diagnostics()["challenges"] == {"target": 1}

    # And the categories behind it are answered from that verdict, not by asking again.
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/milk/-/N-2", settle_ms=1)
    assert len(page.visits) == 1


@pytest.mark.parametrize("status", [404, 429])
async def test_only_a_refusal_a_person_can_answer_is_called_a_checkpoint(status: int) -> None:
    """Parking says "a human must complete something in the window". Two statuses are not that.

    A 404 is a category path that has moved -- a stale `categories.json` -- and a 429 is the
    origin asking for less of us; neither has anything for a person to do, and sending somebody
    to press and hold at one of them sends them to the wrong place.
    """
    page = RecordingPage("Fresh Bananas", "skip to main content")
    page.status = status
    session = session_with(page)

    await session.visit("target", "https://www.target.com/c/eggs/-/N-1", settle_ms=1)
    assert session.retailer_state("target") == "verified_session"


async def test_a_rate_limit_that_says_so_is_still_caught_by_its_wording() -> None:
    """Dropping 429 from the status check costs nothing when the page states it in words."""
    page = RecordingPage("Access to this page has been denied", "We detected unusual traffic")
    page.status = 429
    session = session_with(page)

    with pytest.raises(ManualVerificationRequiredError) as caught:
        await session.visit("target", "https://www.target.com/c/eggs/-/N-1", settle_ms=1)
    assert caught.value.marker in CHALLENGE_MARKERS


async def test_a_load_that_raises_does_not_reopen_the_window_for_the_next_category() -> None:
    """Otherwise every remaining category loads the challenge -- what this exists to prevent."""
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page)
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/a", settle_ms=1)

    session._parked_at["target"] = time.monotonic() - session._challenge_retry - 1

    async def refuse(_url: str, **_kwargs: object) -> None:
        raise TimeoutError("the challenge page never finished loading")

    page.goto = refuse  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await session.visit("target", "https://www.target.com/c/b", settle_ms=1)
    # The one ask was spent even though it never reached the guard.
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/c", settle_ms=1)


async def test_a_standing_verdict_is_a_fresh_error_each_time() -> None:
    """Re-raising one stored instance accumulates a traceback that pins the page it names."""
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page)
    with pytest.raises(ManualVerificationRequiredError) as first:
        await session.visit("target", "https://www.target.com/c/a", settle_ms=1)
    with pytest.raises(ManualVerificationRequiredError) as second:
        await session.visit("target", "https://www.target.com/c/b", settle_ms=1)
    assert second.value is not first.value
    assert second.value.marker == first.value.marker


async def test_the_one_reload_this_class_performs_is_counted() -> None:
    """`page_loads` is what a run answers "how much did it navigate" with; a reload is a load."""
    page = RecordingPage("Robot or human?", "Press and hold")
    session = session_with(page)
    session._verification_timeout = 0.0
    with pytest.raises(ManualVerificationRequiredError):
        await session.visit("target", "https://www.target.com/c/a", settle_ms=1)
    session._awaiting["target"].marker = "blocked data request (https://redsky.target.com/x)"
    page.reload_hook = lambda: page.show("Fresh Bananas", "skip to main content")
    assert await session.wait_for_manual_verification("target") is True
    assert page.reloads == 1
    assert session.diagnostics()["page_loads"]["target"] == 2, "the goto and the recheck reload"


async def test_falling_back_from_an_attach_says_the_identity_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile is the session, so swapping profiles is not a detail to leave in silence.

    No browser starts here: `_ensure_context` imports `async_playwright` when it runs, so a stub
    module stands in for it and the attach and launch are both stubbed out.
    """
    import logging
    import sys
    import types

    class FakePlaywright:
        async def start(self) -> object:
            return object()

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = FakePlaywright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)

    session = BrowserSession(enabled=True)
    session._profile_dir = "/profiles/launched"
    session._cdp_profile_dir = "/profiles/attached"
    launched = object()

    async def no_chrome() -> None:
        return None

    async def launch() -> object:
        return launched

    session._attach_to_running_chrome = no_chrome  # type: ignore[method-assign]
    session._launch_persistent = launch  # type: ignore[method-assign]

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    log = logging.getLogger("storesplit.retailers.browser")
    log.addHandler(handler)
    try:
        assert await session._ensure_context() is launched
    finally:
        log.removeHandler(handler)

    assert session.mode == "persistent_system_chrome"
    assert session.profile_dir_in_use == "/profiles/launched"
    warned = [r for r in records if r.getMessage() == "browser_profile_identity_changed"]
    assert warned, "a run that changed profile must say so"
    assert warned[0].cdp_profile_dir == "/profiles/attached"
    assert warned[0].profile_dir == "/profiles/launched"


async def test_a_successful_attach_records_the_profile_it_attached_to() -> None:
    """Read from the process holding the port, because the browser will not say."""
    import types

    session = BrowserSession(enabled=True)
    context = object()
    browser = types.SimpleNamespace(contexts=[context])
    session._playwright = types.SimpleNamespace(
        chromium=types.SimpleNamespace(connect_over_cdp=lambda *_a, **_k: _ready(browser))
    )

    async def listening(_endpoint: str) -> bool:
        return True

    async def target(_endpoint: str) -> None:
        return None

    async def profile(_endpoint: str) -> str:
        return "/profiles/attached"

    session._cdp_is_listening = listening  # type: ignore[method-assign]
    session._ensure_cdp_target = target  # type: ignore[method-assign]
    session._attached_user_data_dir = profile  # type: ignore[method-assign]

    assert await session._attach_to_running_chrome() is context
    session._mode = "existing_chrome_cdp"
    assert session.profile_dir_in_use == "/profiles/attached"


async def test_the_profile_reader_is_best_effort_and_never_fails_an_attach() -> None:
    """No ps, no subprocess support, or a slow answer: unknown, not an exception."""
    import asyncio as aio

    session = BrowserSession(enabled=True)

    def refuse(*_args: object, **_kwargs: object):
        raise NotImplementedError("this event loop does not do subprocesses")

    original = aio.create_subprocess_exec
    aio.create_subprocess_exec = refuse  # type: ignore[assignment]
    try:
        assert await session._attached_user_data_dir("http://127.0.0.1:9222") is None
    finally:
        aio.create_subprocess_exec = original  # type: ignore[assignment]


def test_two_browsers_claiming_one_port_report_no_profile_rather_than_a_guess() -> None:
    """This is the value that must not be confidently wrong."""
    from app.retailers.browser import user_data_dir_in

    lines = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        "--remote-debugging-port=9222 --user-data-dir=/profiles/one",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        "--remote-debugging-port=9222 --user-data-dir=/profiles/two",
    ]
    assert user_data_dir_in(lines, 9222) is None
    assert user_data_dir_in(lines[:1], 9222) == "/profiles/one"


def test_the_cdp_profile_follows_the_launch_profile_unless_it_is_named() -> None:
    """Moving one profile must not strand the other as an empty directory: a new visitor."""
    moved = Settings(browser_profile_dir="/elsewhere/launch")
    assert moved.browser_cdp_profile_dir == "/elsewhere/chrome-cdp-profile"
    named = Settings(browser_profile_dir="/elsewhere/launch", browser_cdp_profile_dir="/named/cdp")
    assert named.browser_cdp_profile_dir == "/named/cdp"
