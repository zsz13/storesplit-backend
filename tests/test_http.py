"""request_with_retry: bounded retries on transient failures only. No real sleeping."""

import httpx
import pytest
from app.config import get_settings
from app.retailers import http as http_module
from app.retailers.http import build_client, request_with_retry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(*args: object) -> None:
        return None

    monkeypatch.setattr(http_module, "_sleep", instant)


def client_with(responses: list[int | Exception]) -> tuple[httpx.AsyncClient, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls) + 1)
        outcome = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"ok": outcome == 200})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_retries_429_then_succeeds() -> None:
    client, calls = client_with([429, 200])
    response = await request_with_retry(client, "GET", "https://x.test/", max_retries=3)
    assert response.status_code == 200 and calls == [1, 2]
    await client.aclose()


async def test_does_not_retry_404() -> None:
    client, calls = client_with([404, 200])
    response = await request_with_retry(client, "GET", "https://x.test/", max_retries=3)
    assert response.status_code == 404 and calls == [1]
    await client.aclose()


async def test_returns_last_5xx_after_exhausting_retries() -> None:
    client, calls = client_with([503])
    response = await request_with_retry(client, "GET", "https://x.test/", max_retries=2)
    assert response.status_code == 503 and calls == [1, 2, 3]
    await client.aclose()


async def test_transport_error_then_success() -> None:
    client, calls = client_with([httpx.ConnectTimeout("slow"), 200])
    response = await request_with_retry(client, "GET", "https://x.test/", max_retries=1)
    assert response.status_code == 200 and calls == [1, 2]
    await client.aclose()


async def test_transport_error_exhaustion_raises() -> None:
    client, calls = client_with([httpx.ConnectError("down")])
    with pytest.raises(httpx.ConnectError):
        await request_with_retry(client, "GET", "https://x.test/", max_retries=1)
    assert calls == [1, 2]
    await client.aclose()


async def test_build_client_sets_timeout_user_agent_and_pool_limits() -> None:
    settings = get_settings()
    client = build_client()
    assert client.timeout.read == settings.http_timeout_seconds
    assert client.timeout.connect == settings.http_connect_timeout_seconds
    assert "Mozilla" in client.headers["User-Agent"]
    # Keep-alive reuse is the point of a long-lived client, so the limits must be the
    # configured ones and not httpx's defaults.
    pool = client._transport._pool  # type: ignore[attr-defined]
    assert pool._max_connections == settings.http_max_connections
    assert pool._max_keepalive_connections == settings.http_max_keepalive_connections
    assert pool._keepalive_expiry == settings.http_keepalive_expiry_seconds
    await client.aclose()


async def test_explicit_cookie_header_wins_over_the_shared_jar() -> None:
    """Raley's selects a store with a per-request Cookie header, on the shared client.

    The site sets its own `FLDR.User`, so the jar fills up. Two stores fetched back to back
    must still each get their own cookie, or one store's prices are reported for the other.
    """
    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("Cookie"))
        return httpx.Response(200, headers={"set-cookie": "FLDR.User=storeId%3D01; Path=/"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    for store in ("415", "632"):
        await request_with_retry(
            client,
            "GET",
            "https://www.raleys.test/product/1/-",
            headers={"Cookie": f"FLDR.User=storeId%3D{store}%3B"},
        )
    assert sent == ["FLDR.User=storeId%3D415%3B", "FLDR.User=storeId%3D632%3B"]
    assert "FLDR.User" in client.cookies, "the site's own cookie really did land in the jar"
    await client.aclose()
