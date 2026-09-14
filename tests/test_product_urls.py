"""A product URL is a real page on the retailer's own host, or it is None."""

import pytest
from app.retailers.urls import MAX_LENGTH, clean_product_url, valid_product_url

WFM = "https://www.wholefoodsmarket.com"


def test_absolute_url_on_the_retailer_host_is_kept() -> None:
    url = f"{WFM}/grocery/product/produce-organic-banana-b0014gpskq"
    assert clean_product_url(url, base_url=WFM) == url


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "dce2530a", "canonicalUrl": None, "__typename": "LandingProductCanonicalUrl"},
        [{"canonicalUrl": "/p/1"}],
        {"canonicalUrl": "/p/1"},
        12345,
        object(),
        None,
        True,
    ],
)
def test_non_string_payloads_never_become_urls(raw: object) -> None:
    """The Lucky bug: an object stringified into `product_url`."""
    assert clean_product_url(raw, base_url="https://shop.luckysupermarkets.com") is None


@pytest.mark.parametrize(
    "raw",
    [
        "{'id': 'dce2530a', 'canonicalUrl': None, '__typename': 'LandingProductCanonicalUrl'}",
        '{"canonicalUrl": null}',
        "[object Object]",
        "[]",
        "None",
        "null",
        "undefined",
        "nan",
        "N/A",
        "-",
        "",
        "   ",
    ],
)
def test_stringified_objects_and_placeholders_are_rejected(raw: str) -> None:
    assert clean_product_url(raw, base_url=WFM) is None


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "mailto:someone@example.com",
        "file:///etc/passwd",
        "ftp://www.wholefoodsmarket.com/p/1",
    ],
)
def test_only_http_schemes_survive(raw: str) -> None:
    assert clean_product_url(raw, base_url=WFM) is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example.com/product/1",
        "https://wholefoodsmarket.com.evil.example/product/1",
        "https://www.safeway.com/shop/pd/-/960273951",
        "//evil.example.com/product/1",
    ],
)
def test_another_retailers_host_is_rejected(raw: str) -> None:
    assert clean_product_url(raw, base_url=WFM) is None


def test_relative_paths_are_resolved_against_the_retailer_host() -> None:
    assert (
        clean_product_url("/grocery/product/x-b01", base_url=WFM) == f"{WFM}/grocery/product/x-b01"
    )
    # Whole Foods' own `detailUri` has no leading slash.
    assert clean_product_url("product/x-b01", base_url=WFM) == f"{WFM}/product/x-b01"


def test_a_bare_token_is_not_a_path() -> None:
    """`localhost-relative garbage`: a value with no path separator is not a URL."""
    assert clean_product_url("OrganicBanana", base_url=WFM) is None
    assert clean_product_url("b0014gpskq", base_url=WFM) is None


def test_the_host_root_is_not_a_product_page() -> None:
    assert clean_product_url(WFM, base_url=WFM) is None
    assert clean_product_url(f"{WFM}/", base_url=WFM) is None
    assert clean_product_url("/", base_url=WFM) is None


def test_whitespace_and_control_characters_are_rejected() -> None:
    assert clean_product_url(f"{WFM}/product/a b", base_url=WFM) is None
    assert clean_product_url(f"{WFM}/product/a\nb", base_url=WFM) is None


def test_urls_longer_than_the_column_are_rejected() -> None:
    assert clean_product_url(f"{WFM}/product/{'a' * MAX_LENGTH}", base_url=WFM) is None


def test_scheme_and_host_are_normalized_but_the_path_is_not() -> None:
    assert (
        clean_product_url("HTTPS://WWW.WholeFoodsMarket.com/Grocery/Product/A-B01", base_url=WFM)
        == f"{WFM}/Grocery/Product/A-B01"
    )


def test_extra_hosts_are_allowed_when_a_retailer_declares_them() -> None:
    url = "https://shop.savemart.com/store/savemart/products/1"
    assert clean_product_url(url, base_url="https://www.savemart.com") is None
    assert (
        clean_product_url(
            url, base_url="https://www.savemart.com", extra_hosts=("shop.savemart.com",)
        )
        == url
    )


def test_query_strings_survive() -> None:
    url = "https://www.smartandfinal.com/sm/pickup/rsid/320/product/1?store=320"
    assert clean_product_url(url, base_url="https://www.smartandfinal.com") == url


class TestValidProductUrl:
    """The ingest-boundary check: hosts are known, the base URL is not."""

    def test_accepts_a_url_on_an_allowed_host(self) -> None:
        assert valid_product_url(f"{WFM}/grocery/product/a-b01", hosts={"www.wholefoodsmarket.com"})

    def test_rejects_the_wrong_retailer(self) -> None:
        assert not valid_product_url(f"{WFM}/grocery/product/a", hosts={"www.safeway.com"})

    @pytest.mark.parametrize("raw", ["{'canonicalUrl': None}", "None", "/relative/only", None, {}])
    def test_rejects_everything_the_cleaner_rejects(self, raw: object) -> None:
        assert not valid_product_url(raw, hosts={"www.wholefoodsmarket.com"})


class TestHostileInput:
    """Values a hostile or compromised retailer payload could contain."""

    @pytest.mark.parametrize(
        "raw",
        [
            # `urlsplit` ends the authority only at / ? #, and reads the host after the LAST
            # `@`, so it sees the retailer. Every browser treats `\\` as an authority
            # terminator for http(s) and resolves the attacker's host instead.
            "https://evil.example\\@www.wholefoodsmarket.com/product/1",
            "https://www.wholefoodsmarket.com\\.evil.example/product/1",
            "https://evil.example\\www.wholefoodsmarket.com/product/1",
        ],
    )
    def test_a_backslash_in_the_authority_cannot_smuggle_another_host(self, raw: str) -> None:
        assert clean_product_url(raw, base_url=WFM) is None
        assert not valid_product_url(raw, hosts={"www.wholefoodsmarket.com"})

    @pytest.mark.parametrize(
        "raw",
        [
            "https://user:password@www.wholefoodsmarket.com/product/1",
            "https://user@www.wholefoodsmarket.com/product/1",
            "https://@www.wholefoodsmarket.com/product/1",
        ],
    )
    def test_credentials_are_never_carried_into_a_link(self, raw: str) -> None:
        """Userinfo makes the hover target misleading and submits credentials on click."""
        assert clean_product_url(raw, base_url=WFM) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "https://[abc/product/1",  # urlsplit raises "Invalid IPv6 URL"
            "https://\u2100www.wholefoodsmarket.com/product/1",  # NFKC-normalizes into a host
            "https://[::1]:notaport/product/1",
        ],
    )
    def test_input_that_makes_the_parser_raise_is_rejected_not_propagated(self, raw: str) -> None:
        """A parser error must not escape: it would abort the retailer's whole ingest."""
        assert clean_product_url(raw, base_url=WFM) is None
