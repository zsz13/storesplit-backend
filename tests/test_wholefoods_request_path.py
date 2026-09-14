"""The Whole Foods adapter as it actually runs: real request plumbing, no network.

The store-context proof and the availability rules are pure functions tested elsewhere. This
is the wiring that calls them -- which store gets asked about, how often, and what happens
when a call fails halfway. A scrape runs every category for a store concurrently, so that
wiring is where the interesting failures live.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from app.retailers.base import StoreLocation
from app.retailers.wholefoods.adapter import ASINS_PER_REQUEST, WholeFoodsAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "wholefoods"
SOMA_PAGE = (FIXTURES / "product_page_soma.html").read_text()
SOMA = StoreLocation("10151", "Whole Foods SoMa", zip_code="94107")
OCEAN = StoreLocation("10432", "Whole Foods Ocean", zip_code="94112")


def _search_payload(asins: list[str]) -> dict[str, Any]:
    return {
        "results": [
            {
                "name": f"Item {asin}, 12 oz",
                "slug": f"item-{asin.lower()}",
                "regularPrice": 1.99,
                "brand": "Brand",
            }
            for asin in asins
        ]
    }


def _wwos(asins: list[str], *, orderable: set[str] = frozenset()) -> list[dict[str, Any]]:
    return [
        {
            "asin": asin,
            "availability": "IN_STOCK" if asin in orderable else None,
            "offerDetails": {"offerListingId": "abc", "price": {"priceAmount": 1.99}}
            if asin in orderable
            else None,
        }
        for asin in asins
    ]


class Site:
    """A stand-in Whole Foods that records what was asked of it."""

    def __init__(
        self,
        asins: list[str],
        *,
        orderable: set[str] = frozenset(),
        page: str = SOMA_PAGE,
        wwos_fails_after: int | None = None,
        rescued_on_reread: set[str] = frozenset(),
    ) -> None:
        self.asins = asins
        self.orderable = orderable
        self.page = page
        # ASINs the real endpoint flaps on: no offer on the first read of them, a live one on
        # a later read. This is the behaviour the second pass exists for.
        self.rescued_on_reread = rescued_on_reread
        self.seen_asins: set[str] = set()
        # `request_with_retry` retries a 5xx, so a single failing response is not a failure.
        # This fails every wwos call after the given number, which is what an endpoint that
        # is actually down looks like.
        self.wwos_fails_after = wwos_fails_after
        self.wwos_calls = 0
        self.paths: list[str] = []
        self.wwos_batches: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/api/search":
            return httpx.Response(200, json=_search_payload(self.asins))
        if request.url.path == "/api/wwos/products":
            self.wwos_calls += 1
            asked = [asin for asin in (request.url.params.get("asins") or "").split(",") if asin]
            self.wwos_batches.append(asked)
            if self.wwos_fails_after is not None and self.wwos_calls > self.wwos_fails_after:
                return httpx.Response(503, text="down")
            flapped = {a for a in asked if a in self.rescued_on_reread and a in self.seen_asins}
            self.seen_asins.update(asked)
            return httpx.Response(200, json=_wwos(asked, orderable=self.orderable | flapped))
        return httpx.Response(200, text=self.page, headers={"content-type": "text/html"})


def _adapter(clients, site: Site) -> WholeFoodsAdapter:
    adapter = WholeFoodsAdapter(clients)
    adapter._client = httpx.AsyncClient(  # the seam the Kroger tests use too
        transport=httpx.MockTransport(site.handler), base_url="https://www.wholefoodsmarket.com"
    )
    return adapter


async def test_a_search_stamps_every_listing_with_the_store_it_proved(clients) -> None:
    site = Site(["B00000000A", "B00000000B"], orderable={"B00000000A"})

    listings = await _adapter(clients, site).search_products("eggs", SOMA)

    assert {item.store_context for item in listings} == {"10151"}
    by_sku = {item.retailer_sku: item.availability for item in listings}
    # B is the ASIN the endpoint answers for with nothing at all, which states nothing.
    assert by_sku == {"B00000000A": "in_stock", "B00000000B": "unknown"}


async def test_a_page_that_rendered_for_another_store_buys_nothing(clients) -> None:
    """The page proves store 10151; the adapter asked about 10432. Trust neither."""
    site = Site(["B00000000A"], orderable={"B00000000A"})

    listings = await _adapter(clients, site).search_products("eggs", OCEAN)

    assert [item.availability for item in listings] == ["unknown"]
    assert [item.store_context for item in listings] == [None]
    assert "/api/wwos/products" not in site.paths, "no availability was even asked for"


async def test_the_store_page_is_read_once_however_many_categories_run_at_once(clients) -> None:
    """A scrape runs all seven categories for a store concurrently.

    Checking the cache and filling it either side of an `await` let every one of them start
    its own fetch, so the store page was read seven times -- and whichever finished last
    decided the store's availability for the whole run.
    """
    site = Site(["B00000000A"], orderable={"B00000000A"})
    adapter = WholeFoodsAdapter(clients)

    async def slow(request: httpx.Request) -> httpx.Response:
        response = site.handler(request)
        if request.url.path.startswith("/grocery/product/"):
            await asyncio.sleep(0.02)  # a real store page is not instant
        return response

    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow), base_url="https://www.wholefoodsmarket.com"
    )

    await asyncio.gather(*(adapter.search_products(q, SOMA) for q in ("eggs", "milk", "bread")))

    assert site.paths.count("/grocery/product/item-b00000000a") == 1


async def test_a_re_read_that_fails_leaves_the_listing_unknown(clients) -> None:
    """The re-read can only rescue a positive, and a failed one rescues nothing."""
    site = Site(["B00000000A"], wwos_fails_after=1)  # the first read answers; the re-read does not

    listings = await _adapter(clients, site).search_products("eggs", SOMA)

    assert [item.availability for item in listings] == ["unknown"]
    assert [item.stock_status for item in listings] == ["availability=None no offer"]


async def test_every_asin_is_asked_about_when_there_are_more_than_one_batch(clients) -> None:
    asins = [f"B{index:09d}" for index in range(ASINS_PER_REQUEST + 7)]
    site = Site(asins, orderable=set(asins))

    listings = await _adapter(clients, site).search_products("eggs", SOMA)

    assert len(listings) == len(asins)
    assert all(item.availability == "in_stock" for item in listings)
    asked = [asin for batch in site.wwos_batches for asin in batch]
    assert sorted(asked) == sorted(asins), "no ASIN was dropped between batches"
    assert all(len(batch) <= ASINS_PER_REQUEST for batch in site.wwos_batches)


async def test_a_listing_with_no_asin_is_never_sent_to_the_availability_endpoint(
    clients,
) -> None:
    """A slug with no `-b0…` suffix becomes the SKU. Sending it can fail the whole batch,
    which would leave 50 real items unknown for the sake of one malformed one."""
    site = Site(["B00000000A"], orderable={"B00000000A"})
    site.asins = ["B00000000A"]
    adapter = _adapter(clients, site)
    original = _search_payload(["B00000000A"])
    original["results"].append(
        {"name": "Odd One, 12 oz", "slug": "odd-one-no-asin", "regularPrice": 2.99}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            site.paths.append(request.url.path)
            return httpx.Response(200, json=original)
        return site.handler(request)

    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.wholefoodsmarket.com"
    )
    listings = await adapter.search_products("eggs", SOMA)

    asked = [asin for batch in site.wwos_batches for asin in batch]
    assert "odd-one-no-asin" not in asked
    odd = next(item for item in listings if item.retailer_sku == "odd-one-no-asin")
    assert odd.availability == "unknown"


async def test_a_failing_search_is_not_swallowed(clients) -> None:
    """A search that fails must raise, so the scrape records it rather than expiring offers."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    adapter = WholeFoodsAdapter(clients)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.wholefoodsmarket.com"
    )
    try:
        await adapter.search_products("eggs", SOMA)
    except httpx.HTTPStatusError:
        return
    raise AssertionError("a 503 search should raise")


async def test_store_details_refuse_a_page_that_is_a_different_store(clients) -> None:
    """`/stores/<folder>` names its own `storeCode`; a stale folder must buy nothing."""
    elsewhere = json.dumps({"location": {"storeCode": "99999", "locationName": "Elsewhere"}})
    island = (
        '<script type="a-state" data-a-state="{&quot;key&quot;:&quot;detail-page-state&quot;}">'
    )
    site = Site([], page=island + elsewhere + "</script>")
    adapter = _adapter(clients, site)

    assert await adapter.fetch_store_details(SOMA) is None


async def test_a_reread_batch_that_fails_does_not_discard_the_ones_that_answered(clients) -> None:
    """The second pass can only add a positive, so a failed batch must cost only its own ASINs.

    Over `ASINS_PER_REQUEST` offer-less ASINs, the re-read is itself more than one call. When a
    later batch fails, the offers the earlier batch proved are real answers about a shelf --
    discarding them would trade a confirmed `in_stock` for an `unknown`, which is exactly the
    kind of invented verdict this endpoint's silence already caused once.
    """
    asins = [f"B{index:09d}" for index in range(ASINS_PER_REQUEST + 7)]
    rescued = set(asins[:ASINS_PER_REQUEST])
    # First read: two batches. Re-read: two more, and the last one is down.
    site = Site(asins, rescued_on_reread=rescued, wwos_fails_after=3)

    listings = await _adapter(clients, site).search_products("eggs", SOMA)

    by_sku = {item.retailer_sku: item.availability for item in listings}
    assert {by_sku[sku] for sku in asins[:ASINS_PER_REQUEST]} == {"in_stock"}
    assert {by_sku[sku] for sku in asins[ASINS_PER_REQUEST:]} == {"unknown"}
