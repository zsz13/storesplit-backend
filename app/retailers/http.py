"""Shared async httpx clients: pooled connections, explicit timeouts, bounded retries.

Clients are long lived (see `app/retailers/clients.py`): they are opened once per process and
reused for every request, so keep-alive TCP/TLS connections are shared across retailers,
stores and categories. Nothing here ever builds a client per request.

Every request takes a slot from the ambient per-retailer request budget, so nested fan-outs
inside an adapter cannot exceed the configured number of requests in flight.
"""

import asyncio
import logging
import random
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.concurrency import request_slot
from app.config import get_settings

log = logging.getLogger("storesplit.http")

# A plain, honest desktop user agent; retailers that reject it are not scraped.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class RetryableHTTPError(RuntimeError):
    pass


def build_client(headers: Mapping[str, str] | None = None) -> httpx.AsyncClient:
    """One pooled AsyncClient. The caller owns it for the lifetime of the process."""
    settings = get_settings()
    base_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        base_headers.update(headers)
    return httpx.AsyncClient(
        headers=base_headers,
        timeout=httpx.Timeout(
            settings.http_timeout_seconds, connect=settings.http_connect_timeout_seconds
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive_connections,
            keepalive_expiry=settings.http_keepalive_expiry_seconds,
        ),
        follow_redirects=False,
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying transient failures with capped exponential backoff.

    Retries: connection errors, timeouts and HTTP 429/5xx. Never retries 4xx other than 429.
    """
    retries = get_settings().http_max_retries if max_retries is None else max_retries
    attempt = 0
    while True:
        attempt += 1
        started = time.monotonic()
        try:
            async with request_slot():
                response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt > retries:
                raise
            await _sleep(attempt, url, f"{type(exc).__name__}")
            continue
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "http_request",
            extra={"method": method, "url": url, "status": response.status_code, "ms": elapsed_ms},
        )
        if response.status_code in _RETRY_STATUSES and attempt <= retries:
            await _sleep(attempt, url, f"status {response.status_code}")
            continue
        return response


async def _sleep(attempt: int, url: str, reason: str) -> None:
    delay = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.25)
    log.warning(
        "http_retry",
        extra={"url": url, "attempt": attempt, "reason": reason, "delay_s": round(delay, 2)},
    )
    await asyncio.sleep(delay)
