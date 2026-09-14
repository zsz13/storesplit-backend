"""Kroger adapter request path: credential gating and bearer token handling, no network."""

import asyncio

import httpx
import pytest
from app.config import get_settings
from app.retailers.base import AdapterUnavailableError, StoreLocation
from app.retailers.kroger.adapter import KrogerAdapter

STORE = StoreLocation("70300175", "Ralphs", zip_code="90012")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KROGER_CLIENT_ID", "id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_unconfigured_adapter_refuses_requests(
    clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    monkeypatch.delenv("KROGER_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    adapter = KrogerAdapter(clients)
    assert adapter.is_configured() is False
    with pytest.raises(AdapterUnavailableError):
        await adapter.search_products("eggs", STORE)
    get_settings.cache_clear()


async def test_token_is_fetched_once_and_sent_as_bearer(clients, configured) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/connect/oauth2/token"):
            assert request.headers["Authorization"].startswith("Basic ")
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "tok123", "expires_in": 1800})
        assert request.headers["Authorization"] == "Bearer tok123"
        if request.url.path.endswith("/locations"):
            assert request.url.params["filter.zipCode.near"] == "90012"
            return httpx.Response(200, json={"data": []})
        assert request.url.params["filter.term"] == "eggs"
        assert request.url.params["filter.locationId"] == "70300175"
        return httpx.Response(200, json={"data": []})

    adapter = KrogerAdapter(clients)
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert adapter.is_configured()
    assert await adapter.find_stores("90012-1234") == []
    assert await adapter.search_products("eggs", STORE) == []
    token_calls = [r for r in seen if r.url.path.endswith("/token")]
    assert len(token_calls) == 1 and len(seen) == 3
    await adapter._client.aclose()


async def test_concurrent_requests_share_one_token(clients, configured) -> None:
    """Two searches starting together must not each mint their own bearer token."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/connect/oauth2/token"):
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "tok123", "expires_in": 1800})
        return httpx.Response(200, json={"data": []})

    adapter = KrogerAdapter(clients)
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with asyncio.TaskGroup() as group:
        group.create_task(adapter.search_products("eggs", STORE))
        group.create_task(adapter.search_products("milk", STORE))
    assert len([path for path in seen if path.endswith("/token")]) == 1
    await adapter._client.aclose()
