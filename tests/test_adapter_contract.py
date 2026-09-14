"""The rules every adapter obeys, checked against every adapter's own fixtures.

The Lucky bug -- a payload object stringified into `product_url` -- was one adapter's
mistake, but nothing in the codebase stopped any other adapter making it. These tests are
the audit: for each registered retailer, every listing its parser produces must carry a URL
that is a real page on that retailer's own host (or no URL at all), and an availability that
is one of the three states.
"""

import inspect
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from app.normalize.availability import AVAILABILITY_STATES
from app.retailers import adapter_slugs, get_adapter, product_hosts, stock_reporting
from app.retailers.base import ProductListing, StoreLocation
from app.retailers.kroger.adapter import parse_products as kroger_products
from app.retailers.raleys.adapter import parse_product_page as raleys_page
from app.retailers.raleys.adapter import store_directory as raleys_directory
from app.retailers.ranch99.adapter import parse_search_results as ranch99_search
from app.retailers.safeway.adapter import parse_product_page as safeway_page
from app.retailers.savemartco.storefront import (
    LUCKY_BANNER,
    SAVEMART_BANNER,
)
from app.retailers.savemartco.storefront import (
    parse_items as savemartco_items,
)
from app.retailers.smartandfinal.adapter import parse_search_results as snf_search
from app.retailers.sprouts.adapter import parse_items as sprouts_items
from app.retailers.target.adapter import parse_category as target_category
from app.retailers.traderjoes.adapter import parse_products as tj_products
from app.retailers.urls import valid_image_url, valid_product_url
from app.retailers.wholefoods.adapter import parse_search_results as wfm_search
from app.retailers.wholefoods.stores import load_stores as wholefoods_directory
from app.retailers.zipmatch import rank_stores_by_zip

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def read(name: str) -> str:
    return (FIXTURES / name).read_text()


def _one(listing: ProductListing | None) -> list[ProductListing]:
    return [listing] if listing is not None else []


# One parser per retailer, fed its own captured payload.
PARSERS: dict[str, Callable[[], list[ProductListing]]] = {
    "wholefoods": lambda: wfm_search(load("wholefoods/search_eggs.json"), "10151"),
    "kroger": lambda: kroger_products(load("kroger/products_eggs.json"), "01400376"),
    "ranch99": lambda: ranch99_search(load("ranch99/search_eggs.json"), "1769"),
    "smartandfinal": lambda: snf_search(load("smartandfinal/search_eggs.json"), "320"),
    "sprouts": lambda: sprouts_items(load("sprouts/items_eggs.json"), "26417"),
    "traderjoes": lambda: tj_products(load("traderjoes/search_eggs.json"), "100", "tj:test"),
    # Target is read from the payloads its own page fetched, so its "fixture" is a pair.
    "target": lambda: target_category(
        [
            ("…/plp_search_v2", load("target/plp_search_v2.json")),
            (
                "…/product_summary_with_fulfillment_v1",
                load("target/product_summary_with_fulfillment_v1.json"),
            ),
        ],
        "3264",
        "target:test",
    ),
    "safeway": lambda: _one(safeway_page(read("safeway/product_page.html"), "3132")),
    "lucky": lambda: savemartco_items(
        load("lucky/items_eggs_94105.json"), "23130", "lucky:test", banner=LUCKY_BANNER
    ),
    "savemart": lambda: savemartco_items(
        load("lucky/items_eggs_94105.json"), "23130", "savemart:test", banner=SAVEMART_BANNER
    ),
    "raleys": lambda: _one(raleys_page(read("raleys/product_page.html"), "415")),
}


def test_every_registered_retailer_is_audited() -> None:
    """A new adapter cannot be added without bringing it under these rules."""
    assert set(PARSERS) == set(adapter_slugs())


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_every_product_url_is_a_real_page_on_the_right_retailer(slug: str) -> None:
    listings = PARSERS[slug]()
    assert listings, f"{slug} fixture should yield listings"
    hosts = product_hosts(slug)
    assert hosts, f"{slug} must declare a site_url"
    for item in listings:
        if item.product_url is None:
            continue  # a retailer with no reliable product page is allowed to say so
        assert valid_product_url(item.product_url, hosts=hosts), (
            f"{slug} produced {item.product_url!r}, which is not a page on {sorted(hosts)}"
        )


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_no_adapter_ever_stringifies_a_payload_into_a_url(slug: str) -> None:
    for item in PARSERS[slug]():
        url = item.product_url
        assert url is None or isinstance(url, str)
        if url is None:
            continue
        for poison in ("{", "}", "[object", "None", "null", "undefined", "__typename"):
            assert poison not in url, f"{slug}: {poison!r} in {url!r}"


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_every_image_url_would_be_rendered_as_an_image(slug: str) -> None:
    """`image_url` is a URL the browser fetches, so it lives under the same rules.

    It used to be the one URL field with no gate anywhere: `raleys`, `target` and `walmart`
    called `str()` on an unvalidated payload value, and `wholefoods` and `safeway` passed a
    dict straight through.
    """
    for item in PARSERS[slug]():
        if item.image_url is None:
            continue  # a listing without a picture is allowed to say so
        assert valid_image_url(item.image_url), (
            f"{slug} produced image_url={item.image_url!r}, which would not render"
        )


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_no_adapter_ever_stringifies_a_payload_into_an_image_url(slug: str) -> None:
    for item in PARSERS[slug]():
        url = item.image_url
        assert url is None or isinstance(url, str)
        if url is None:
            continue
        for poison in ("{", "}", "[object", "None", "null", "undefined", "__typename"):
            assert poison not in url, f"{slug}: {poison!r} in {url!r}"


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_at_least_one_listing_per_retailer_carries_an_image(slug: str) -> None:
    """A silent regression to "no images at all" is the failure mode this catches.

    Smart & Final's fallback read the container rather than the value, so items whose
    `image` dict had no `template` key lost their picture without anything failing.
    """
    listings = PARSERS[slug]()
    assert any(item.image_url for item in listings), (
        f"{slug} produced no image_url for any of its {len(listings)} fixture listings"
    )


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_every_listing_reports_one_of_the_three_availability_states(slug: str) -> None:
    for item in PARSERS[slug]():
        assert item.availability in AVAILABILITY_STATES, f"{slug}: {item.availability!r}"


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_availability_is_never_asserted_without_a_retailer_signal(slug: str) -> None:
    """`in_stock` must trace back to something the payload said, not to a default."""
    for item in PARSERS[slug]():
        if item.availability == "in_stock":
            assert item.stock_status, (
                f"{slug} called {item.retailer_sku} in stock but kept no retailer wording"
            )


def test_wholefoods_starts_unknown_because_its_search_says_nothing() -> None:
    """Whole Foods' search carries no availability; `/api/wwos/products` supplies it later."""
    listings = PARSERS["wholefoods"]()
    assert all(item.availability == "unknown" for item in listings)


def test_raleys_untracked_inventory_is_unknown_not_in_stock() -> None:
    """Raley's product pages report `inventoryMode: "None"`: no stock is tracked."""
    listing = PARSERS["raleys"]()[0]
    assert listing.availability == "unknown"
    assert listing.stock_status == "inventoryMode=None"


def test_an_optional_store_details_capability_has_the_shape_the_scrape_calls(clients) -> None:
    """`fetch_store_details` is found by name, not declared on the adapter Protocol.

    Nothing type-checks the adapters that grow one, and the scrape calls it as
    `await capability(store)`. A second retailer implementing it as, say, `(store_id: str)`
    would pass lint, types and every other test, and quietly lose its own hours forever.
    """
    for slug in adapter_slugs():
        capability = getattr(get_adapter(slug, clients), "fetch_store_details", None)
        if capability is None:
            continue
        assert inspect.iscoroutinefunction(capability), f"{slug}: must be awaitable"
        parameters = list(inspect.signature(capability).parameters.values())
        assert len(parameters) == 1, f"{slug}: takes the store, and nothing else"
        assert parameters[0].annotation in (StoreLocation, "StoreLocation"), (
            f"{slug}: takes a StoreLocation"
        )


def test_which_retailers_can_read_their_own_store_details_is_pinned_by_name(clients) -> None:
    """A capability is found with `getattr`, so losing one is silent: the adapter keeps
    working, the scrape keeps passing, and that retailer simply says "Hours not published"
    for ever. Naming them here turns that into a failing test.

    Raley's is the deliberate absence. It serves store details from the robots-disallowed
    `/api`, and its store page carries interface strings and no store record, so it has no
    first-party surface to implement this against -- only a verified Google place.
    """
    can_read = {
        slug
        for slug in adapter_slugs()
        if getattr(get_adapter(slug, clients), "fetch_store_details", None) is not None
    }

    assert can_read == {
        "wholefoods",
        "target",
        "safeway",
        "smartandfinal",
        "sprouts",
        "lucky",
        "savemart",
        "ranch99",
        "traderjoes",
    }
    assert "raleys" not in can_read, "no surface robots.txt allows; Google is its only source"


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_every_retailer_states_whether_it_publishes_stock_at_all(slug: str) -> None:
    """A new adapter has to answer the question, because a default would answer it wrongly.

    `live` on a retailer that publishes nothing words every offer "stock not confirmed", as
    though a lookup had failed; `not_published` on one that does excuses a real unknown as
    "the retailer does not say". Both are lies to a shopper, so the Protocol requires it.
    """
    assert stock_reporting(slug) in ("live", "not_published")


@pytest.mark.parametrize("slug", sorted(PARSERS))
def test_a_retailer_that_publishes_no_stock_never_reports_an_in_stock_listing(
    slug: str,
) -> None:
    """The declaration and the parser have to agree, or one of them is wrong.

    `not_published` is what lets a client say "availability not published" instead of
    "stock unknown". If such an adapter ever produced an `in_stock` listing, the wording
    would be a flat contradiction of a badge the same offer could win.
    """
    if stock_reporting(slug) != "not_published":
        return
    for item in PARSERS[slug]():
        assert item.availability != "in_stock", (
            f"{slug} declares it publishes no stock but called {item.retailer_sku} in stock"
        )


def test_traderjoes_and_raleys_are_the_retailers_that_publish_no_stock() -> None:
    """Pinned by name, because this is a fact about those two retailers rather than a rule.

    Trader Joe's `availability` is `"1"` for its entire catalogue (carriage, not stock) and
    Raley's commercetools catalogue runs `inventoryMode: "None"`. Anything else gaining the
    flag is a claim about a retailer that somebody has to have checked.
    """
    silent = {slug for slug in adapter_slugs() if stock_reporting(slug) == "not_published"}
    assert silent == {"traderjoes", "raleys"}


def test_an_unregistered_slug_is_treated_as_a_retailer_that_does_publish_stock() -> None:
    """A row whose adapter has been removed keeps the cautious wording.

    "Availability not published" is an excuse; it must never be earned by a missing adapter.
    """
    assert stock_reporting("a-retailer-that-was-deleted") == "live"


def test_a_vendored_directory_resolves_two_distant_zips_to_different_stores(clients) -> None:
    """Whichever way an adapter finds its stores, the ZIP has to be what decides them.

    Only the vendored-directory retailers can be asked this without a network, which is both
    of the ones this change was about.
    """
    for slug, directory in (
        ("wholefoods", wholefoods_directory()),
        ("raleys", list(raleys_directory())),
    ):
        downtown = [s.external_id for s in rank_stores_by_zip(directory, "94105")]
        inland = [s.external_id for s in rank_stores_by_zip(directory, "95814")]
        assert downtown and inland, slug
        assert set(downtown).isdisjoint(inland), f"{slug} serves the same store 90 miles apart"
