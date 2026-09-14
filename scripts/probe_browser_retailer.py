#!/usr/bin/env python
"""Check what Target and Walmart will actually give a persistent, headed browser today.

This is the operator's end of the manual-verification checkpoint. Both retailers sit behind
PerimeterX, and whether they answer changes from week to week and from profile to profile,
so the honest way to know is to look:

    uv run --extra browser python scripts/probe_browser_retailer.py
    uv run --extra browser python scripts/probe_browser_retailer.py --retailer walmart --wait

For each page it reports whether the retailer served data, or asked for a human. It never
tries to get past a challenge. With `--wait` it leaves the window open on the challenge and
waits for you to finish it by hand; the moment you do, it carries on in the *same* browser
context, so the session you just proved out is the one it uses -- and, because the profile is
persistent, the one later runs reuse until it expires.

Findings when this was written, from a persistent profile in the host's own Chrome:

* Walmart product pages answer in full: `__NEXT_DATA__` carries the price, the UPC, the
  store the page resolved to, its own `canonicalUrl`, and a real per-store
  `availabilityStatus` (`{"display": "Out of stock", "value": "OUT_OF_STOCK"}`).
* Walmart category pages answer "Robot or human?".
* Target renders its whole page shell and blocks every data call underneath it: each
  `redsky.target.com` response is a PerimeterX block naming a CAPTCHA script. A page like
  that looks healthy and contains nothing, which is why the browser layer inspects the
  responses and not only the rendered text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.logging import configure_logging
from app.retailers.browser import (
    BrowserSession,
    BrowserUnavailableError,
    ManualVerificationRequiredError,
)

log = logging.getLogger("storesplit.scripts.probe_browser")

# Only pages the retailer's own robots.txt leaves open. Target disallows `/s?`, `/shop/`
# and `/pl/`; Walmart disallows `/search` and `/api/`. Category and product pages are not
# disallowed by either, and Walmart explicitly allows `/store/finder`.
PAGES: dict[str, list[tuple[str, str]]] = {
    "walmart": [
        ("store finder", "https://www.walmart.com/store/finder?location=94105"),
        (
            "product page",
            "https://www.walmart.com/ip/Great-Value-Large-White-Eggs-12-Count/145051970",
        ),
        ("category page", "https://www.walmart.com/browse/food/eggs/976759_9969031_1001320"),
    ],
    "target": [
        ("category page", "https://www.target.com/c/fresh-bananas-fruit-produce-grocery/-/N-mo05s"),
        ("product page", "https://www.target.com/p/-/A-14775063"),
    ],
}


async def probe(session: BrowserSession, retailer: str, *, wait: bool) -> list[str]:
    """Walk one retailer's pages, reporting what it served and what it asked for.

    Returns the lines to print. They are collected rather than printed as they happen so
    that two retailers running at once do not interleave into nonsense.
    """
    lines = [f"{retailer}:"]
    for label, url in PAGES[retailer]:
        try:
            page = await session.visit(retailer, url, settle_ms=6000)
        except ManualVerificationRequiredError as challenge:
            lines.append(f"  {label:<14} CHALLENGED  {challenge.marker[:70]}")
            lines.append(f"    -> {challenge}")
            if not wait:
                lines.append("    (re-run with --wait to complete it by hand and continue)")
                return lines
            window = get_settings().browser_manual_verification_timeout_seconds
            opened = datetime.now()
            expires = opened + timedelta(seconds=window)
            # Printed immediately, not buffered: this is the one line somebody is waiting on.
            print(
                f"\n  [{retailer}] NEEDS YOU: {challenge.marker[:60]}\n"
                f"    Complete the challenge in the open Chrome window.\n"
                f"    Window open {opened:%H:%M:%S} -> {expires:%H:%M:%S} "
                f"({window / 60:.0f} minutes). The page will NOT be reloaded while you work.",
                flush=True,
            )
            lines.append(f"    waited from {opened:%H:%M:%S}, window {window / 60:.0f} min")
            if not await session.wait_for_manual_verification(retailer):
                lines.append("    window expired; the tab is left open, so finishing it later")
                lines.append("    still counts -- the profile is persistent.")
                return lines
            print(f"  [{retailer}] verified -- continuing in the same session", flush=True)
            lines.append("    verified -- continued in the same browser session")
            try:
                page = await session.visit(retailer, url, settle_ms=6000)
            except ManualVerificationRequiredError as again:
                lines.append(f"    still challenged afterwards: {again.marker[:60]}")
                return lines
        payload = await page.evaluate(
            "() => {const e = document.querySelector('#__NEXT_DATA__');"
            " return e ? e.textContent.length : 0}"
        )
        title = (await page.title())[:60]
        lines.append(f"  {label:<14} OK          {title!r} embedded_json_bytes={payload}")
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retailer", choices=sorted(PAGES), action="append")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="leave the window open on a challenge and wait for you to complete it",
    )
    args = parser.parse_args()
    configure_logging("INFO")

    # Running this script *is* the explicit request for a browser that the setting
    # otherwise waits for, so it turns the fallback on for itself.
    session = BrowserSession(enabled=True)
    if not session.is_configured():
        print("the browser extra is not installed: uv sync --extra browser")
        return 2
    try:
        # Concurrently, and each retailer on its own page: a challenge on one must not hold
        # the other up, which is the same rule the scrape itself follows.
        retailers = args.retailer or sorted(PAGES)
        reports = await asyncio.gather(
            *(probe(session, retailer, wait=args.wait) for retailer in retailers)
        )
        for lines in reports:
            print()
            for line in lines:
                print(line)
        diagnostics = session.diagnostics()
        print(f"\nmode: {diagnostics['mode']}")
        print(f"  cdp endpoint : {diagnostics['cdp_endpoint']}")
        # The profile in use is the session. It is printed first, and the two configured
        # directories beside it, because "which profile was this?" is the question a run that
        # came back challenged has to answer before anything else is worth reading.
        print(f"  profile IN USE : {diagnostics['profile_dir'] or 'unknown'}")
        print(f"    configured   : {diagnostics['configured_profile_dir']}")
        print(f"    cdp profile  : {diagnostics['cdp_profile_dir']}")
        # A load is the expensive request here; the lines under it are the ones that did
        # not happen, and the first-party payloads they were for.
        print(f"  page loads   : {diagnostics['page_loads'] or '{}'}")
        print(f"  loads reused : {diagnostics['pages_reused_from_capture'] or '{}'}")
        print(f"  pages read on: {diagnostics['pages_continued'] or '{}'}")
        print(f"  data responses: {diagnostics['data_responses'] or '{}'}")
        print(f"  challenges   : {diagnostics['challenges'] or '{}'}")
        # What the session is carrying, counted. Run this before and after restarting the
        # browser and the two numbers answer "did the session survive" without guesswork:
        # `session_scoped` is the part that does not, and for Target that includes the store it
        # had selected. Counts and hosts only -- no cookie value is read or printed.
        census = await session.storage_census()
        if census.get("connected"):
            print(f"  cookies      : {census['cookies_total']} total")
            for host, counted in census["cookies_by_host"].items():
                if "target" in host or "walmart" in host:
                    print(
                        f"    {host:<28} persistent={counted['persistent']} "
                        f"session_scoped={counted['session_scoped']} (lost on restart)"
                    )
            for slug, storage in census["pages"].items():
                print(f"    {slug}: {storage}")
        for slug, state in sorted(diagnostics["retailers"].items()):
            print(f"  {slug:<10}: {state}")
        pending = session.awaiting_verification()
        if pending:
            print("\nstill waiting on a human:")
            for slug, message in pending.items():
                print(f"  {slug}: {message}")
        print(f"\n{json.dumps({'mode': diagnostics['mode'], **diagnostics['retailers']})}")
    except BrowserUnavailableError as exc:
        print(f"browser unavailable: {exc}")
        return 2
    finally:
        await session.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
