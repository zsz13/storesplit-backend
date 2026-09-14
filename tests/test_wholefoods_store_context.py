"""Proving which Whole Foods store a page was rendered for, before believing anything on it.

Whole Foods will happily render a product page for a store other than the one asked for, and
`locationCookie` reports "Lamar", Austin TX on every page including San Francisco ones. So
the store is read from the fields that actually vary -- `overrideStoreId`, `isDefaultLocation`
and `almAttributes.storeId` -- and a page that cannot prove its store buys nothing.
"""

import json
from pathlib import Path

from app.retailers.wholefoods.adapter import parse_store_context

FIXTURES = Path(__file__).parent / "fixtures" / "wholefoods"


def _soma() -> str:
    return (FIXTURES / "product_page_soma.html").read_text()


def test_a_store_scoped_page_yields_its_store_and_discriminator() -> None:
    context = parse_store_context(_soma())

    assert context is not None
    assert context.store_external_id == "10151"
    assert context.discriminator == "A0D4"
    assert context.is_default_location is False


def test_the_location_cookie_naming_austin_is_ignored() -> None:
    """`locationCookie.name` is "Lamar" on this San Francisco page. It is not the store."""
    assert '"name": "Lamar"' in _soma() or '"name":"Lamar"' in _soma()

    context = parse_store_context(_soma())

    assert context is not None and context.store_external_id == "10151"


def test_a_page_rendered_for_the_default_location_proves_nothing() -> None:
    html = _soma().replace('"isDefaultLocation": false', '"isDefaultLocation": true')
    assert html != _soma()

    assert parse_store_context(html) is None


def test_a_page_whose_store_fields_disagree_proves_nothing() -> None:
    """`overrideStoreId` says one store and the ALM attributes another: believe neither."""
    html = _soma().replace('"overrideStoreId": "10151"', '"overrideStoreId": "10152"')
    assert html != _soma()

    assert parse_store_context(html) is None


def test_a_page_with_no_store_state_yields_nothing() -> None:
    assert parse_store_context("<html><body>nothing here</body></html>") is None
    assert parse_store_context(json.dumps({"props": {"pageProps": {}}})) is None
