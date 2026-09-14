"""Ownership of the process's long-lived HTTP clients.

One `RetailerClients` is opened at application startup and closed at shutdown. Adapters never
create or close a client: they ask this pool for one and keep using it for the life of the
process, so connections stay keep-alive across scrape runs.

Most retailers share a single application client. A retailer gets its own persistent client
only when it needs session state that must not be shared -- in practice a guest session
cookie jar (Sprouts, Lucky, Save Mart). Cookies are domain-scoped, so sharing one jar between
retailers on different hosts is safe, and a retailer that selects a store with an explicit
per-request `Cookie` header keeps it: `http.cookiejar` does not inject a stored cookie over a
header the caller already set.

A retailer that no HTTP client can reach at all asks for `browser()` instead: the same
ownership rule, one process-lifetime resource closed here with the rest. It is built only if
something asks, so a scrape of HTTP-only retailers never launches a browser -- see
`app/retailers/browser.py` for what that layer will and will not do.
"""

from __future__ import annotations

from types import TracebackType

import httpx

from app.retailers.browser import BrowserSession
from app.retailers.http import build_client


class RetailerClients:
    """Long-lived `httpx.AsyncClient`s, opened together and closed together."""

    def __init__(self) -> None:
        # Built eagerly so startup, not the first scrape, is where bad HTTP settings surface.
        self._shared: httpx.AsyncClient | None = build_client()
        self._own: dict[str, httpx.AsyncClient] = {}
        # Not built here: constructing it is free, but nothing should launch a browser
        # because an adapter that never needs one happens to share this pool.
        self._browser: BrowserSession | None = None
        self._closed = False

    def shared(self) -> httpx.AsyncClient:
        """The application-wide client, for retailers that need no session state."""
        self._check_open()
        assert self._shared is not None
        return self._shared

    def own(self, key: str) -> httpx.AsyncClient:
        """The persistent client belonging to one retailer that keeps a session of its own.

        Opened on first use, because a retailer nobody scrapes should not hold a client.
        """
        self._check_open()
        client = self._own.get(key)
        if client is None:
            client = build_client()
            self._own[key] = client
        return client

    def browser(self) -> BrowserSession:
        """The process's one persistent browser, for retailers nothing else reaches.

        Built on first ask and closed with this pool, so the profile -- and any verification
        a human completed in it -- is shared by every retailer that needs one rather than
        rebuilt per adapter.
        """
        self._check_open()
        if self._browser is None:
            self._browser = BrowserSession()
        return self._browser

    async def aclose(self) -> None:
        self._closed = True
        browser, self._browser = self._browser, None
        clients = [c for c in (self._shared, *self._own.values()) if c is not None]
        self._shared = None
        self._own.clear()
        for client in clients:
            await client.aclose()
        if browser is not None:
            await browser.aclose()

    async def __aenter__(self) -> RetailerClients:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("RetailerClients has been closed")
