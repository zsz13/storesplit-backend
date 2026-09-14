#!/usr/bin/env python
"""Start the system Chrome with its DevTools port open, so a scrape can attach to it.

The browser layer prefers attaching to a Chrome that is *already running*
(`existing_chrome_cdp`) over launching one of its own. Attaching is the honest version of
"use my browser": nothing is copied, nothing is synthesised, and the session the retailer
sees is whatever that browser already has. But Chrome only listens if it was started with
`--remote-debugging-port`, and since Chrome 136 it refuses that flag when the profile is the
*default* one -- a deliberate restriction, so that a page cannot talk a browser into exposing
its own cookies. That restriction is not something to work around; this script simply starts
Chrome the supported way, on a profile directory of its own that persists between runs.

    uv run python scripts/start_chrome_cdp.py          # start it, print the endpoint
    uv run python scripts/start_chrome_cdp.py --status  # just say whether it is listening

The profile it opens is yours to use normally: sign in, pick a store, browse. Everything it
accumulates stays in that directory and is there next run, which is the point -- a retailer
that has seen the same browser before is not meeting a stranger every scrape.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LINUX_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable")


def chrome_binary() -> str | None:
    if Path(CHROME).exists():
        return CHROME
    for candidate in LINUX_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


async def listening(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parts.hostname or "127.0.0.1", parts.port or 9222), timeout=3
        )
    except (TimeoutError, OSError):
        return False
    writer.close()
    return True


async def main() -> int:
    settings = get_settings()
    endpoint = settings.browser_cdp_endpoint
    port = urlsplit(endpoint).port or 9222
    # Named in settings, not derived here, so this script and the session that attaches to it
    # cannot disagree about which profile an `existing_chrome_cdp` run is really shopping in.
    profile = Path(settings.browser_cdp_profile_dir)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="only report whether it is up")
    args = parser.parse_args()

    if await listening(endpoint):
        print(f"already listening on {endpoint} -- the scrape will attach to it")
        return 0
    if args.status:
        print(f"nothing on {endpoint}; run this script without --status to start Chrome")
        return 1

    binary = chrome_binary()
    if binary is None:
        print("could not find Google Chrome; install it or set BROWSER_CHANNEL")
        return 2
    profile.mkdir(parents=True, exist_ok=True)
    # A separate profile directory, because Chrome will not open this port on the default
    # one. Everything else is Chrome's own default behaviour: no flags that change what a
    # page can observe about the browser.
    subprocess.Popen(
        [binary, f"--remote-debugging-port={port}", f"--user-data-dir={profile}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        await asyncio.sleep(0.5)
        if await listening(endpoint):
            print(f"Chrome is up and listening on {endpoint}")
            print(f"  profile: {profile}")
            print("  sign in and pick your stores in this window; it persists between runs.")
            return 0
    print(f"Chrome did not open {endpoint} within 10s")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
