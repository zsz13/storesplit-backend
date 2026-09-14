"""The browser's view of the API: which page origins may call it.

A CORS origin is matched as an exact string, so `localhost` and `127.0.0.1` are two
different origins even though they are the same host. The dev stack publishes the
frontend on `0.0.0.0:3000`, so it answers to every loopback spelling; the allowlist has
to name them all or the app loads fine and then every API call fails with "No
'Access-Control-Allow-Origin' header", which reads like a backend outage rather than the
address bar.
"""

import pytest
from httpx import AsyncClient

# The dev frontend, however loopback is spelled in the address bar.
FRONTEND_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"]


@pytest.mark.parametrize("origin", FRONTEND_ORIGINS)
async def test_dev_frontend_origin_is_allowed(client: AsyncClient, origin: str) -> None:
    response = await client.get("/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize("origin", FRONTEND_ORIGINS)
async def test_dev_frontend_preflight_is_allowed(client: AsyncClient, origin: str) -> None:
    """POST /products/refresh sends JSON, so the browser preflights it before the real call."""
    response = await client.options(
        "/products/refresh",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert "POST" in response.headers.get("access-control-allow-methods", "")


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example.com",
        "http://localhost.evil.example.com",  # prefix match would wave this through
        "https://localhost:3000",  # scheme is part of the origin
        "http://localhost:3001",  # port is part of the origin
    ],
)
async def test_unrelated_origins_are_not_allowed(client: AsyncClient, origin: str) -> None:
    """The loopback fix must stay an allowlist, not become a wildcard."""
    response = await client.get("/health", headers={"Origin": origin})

    assert "access-control-allow-origin" not in response.headers
