"""Image URL unwrapping and validation.

`retailer_products.image_url` used to be the one URL field with no validation anywhere:
three adapters called `str()` on an unvalidated payload value, which turns a nested image
*object* into the string `"{'url': None}"` -- a non-empty value that a String column accepts
and a browser then requests against StoreSplit's own origin.
"""

import pytest
from app.retailers.images import image_url_from
from app.retailers.urls import IMAGE_MAX_LENGTH, clean_image_url


class TestImageUrlFrom:
    def test_plain_string(self) -> None:
        assert image_url_from(" https://cdn.test/a.jpg ") == "https://cdn.test/a.jpg"

    def test_empty_string_is_nothing(self) -> None:
        assert image_url_from("   ") is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"url": "https://cdn.test/a.jpg"},
            {"path": "https://cdn.test/a.jpg"},
            {"imageUrl": "https://cdn.test/a.jpg"},
            {"src": "https://cdn.test/a.jpg"},
            {"template": "https://cdn.test/a.jpg"},
            {"primary_image": {"url": "https://cdn.test/a.jpg"}},
            {"sizes": [{"size": "medium", "url": "https://cdn.test/a.jpg"}]},
            [{"url": "https://cdn.test/a.jpg"}],
        ],
        ids=["url", "path", "imageUrl", "src", "template", "nested", "sizes", "list"],
    )
    def test_unwraps_the_shapes_retailers_really_send(self, payload: object) -> None:
        assert image_url_from(payload) == "https://cdn.test/a.jpg"

    def test_prefers_the_first_named_key_over_traversal_order(self) -> None:
        # `url` wins over an alphabetically earlier key that is not an image field.
        assert image_url_from({"altText": "eggs", "url": "https://cdn.test/a.jpg"}) == (
            "https://cdn.test/a.jpg"
        )

    def test_object_with_no_url_anywhere_is_nothing(self) -> None:
        assert image_url_from({"id": 7, "canonicalUrl": None}) is None

    def test_never_stringifies_an_object(self) -> None:
        # The failure this module exists to prevent.
        assert image_url_from({"id": 7}) is None
        assert image_url_from([1, 2, 3]) is None
        assert image_url_from(object()) is None

    def test_bounded_recursion(self) -> None:
        deep: object = "https://cdn.test/a.jpg"
        for _ in range(50):
            deep = {"url": deep}
        assert image_url_from(deep) is None


class TestCleanImageUrl:
    def test_https_passes(self) -> None:
        assert clean_image_url("https://cdn.test/a.jpg") == "https://cdn.test/a.jpg"

    def test_protocol_relative_is_upgraded(self) -> None:
        # Raley's ships `//contenthandler-raleys.fieldera.com/...`.
        assert clean_image_url("//cdn.test/a.jpg") == "https://cdn.test/a.jpg"

    def test_http_is_upgraded_to_https(self) -> None:
        # An image is a subresource on an https page; http would be blocked as mixed content.
        assert clean_image_url("http://cdn.test/a.jpg") == "https://cdn.test/a.jpg"

    def test_a_different_host_is_allowed(self) -> None:
        # Unlike a product URL: image CDNs are legitimately not the retailer's own host.
        assert clean_image_url("https://target.scene7.com/is/image/Target/GUEST_x") is not None

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            123,
            {"url": "https://cdn.test/a.jpg"},
            ["https://cdn.test/a.jpg"],
            "{'id': 7, 'canonicalUrl': None}",
            "[object Object]",
            "null",
            "undefined",
            "",
            "   ",
            "/relative/a.jpg",
            "a.jpg",
            "ftp://cdn.test/a.jpg",
            "javascript:alert(1)",
            "data:image/png;base64,iVBOR",
            "https://user:pw@cdn.test/a.jpg",
            "https://evil.test\\@cdn.test/a.jpg",
            "https://cdn.test",
            "https://cdn.test/",
            "https://",
        ],
        ids=[
            "none",
            "int",
            "dict",
            "list",
            "repr",
            "object-object",
            "null",
            "undefined",
            "empty",
            "blank",
            "relative",
            "bare",
            "ftp",
            "javascript",
            "data",
            "credentials",
            "backslash",
            "no-path",
            "root-path",
            "hostless",
        ],
    )
    def test_rejected(self, raw: object) -> None:
        assert clean_image_url(raw) is None

    def test_too_long_for_the_column_is_rejected(self) -> None:
        long_url = "https://cdn.test/" + ("a" * IMAGE_MAX_LENGTH)
        assert len(long_url) > IMAGE_MAX_LENGTH
        assert clean_image_url(long_url) is None

    def test_query_string_is_kept(self) -> None:
        # Retailer CDNs size images with a query (`?w=200`); dropping it changes the image.
        assert clean_image_url("https://cdn.test/a.jpg?w=200&h=200") == (
            "https://cdn.test/a.jpg?w=200&h=200"
        )

    def test_unsubstituted_template_placeholder_is_rejected(self) -> None:
        # Smart & Final's `template` and the Instacart storefronts' `templateUrl` carry
        # placeholders. An unsubstituted one is not an image, it is a 404.
        assert clean_image_url("https://cdn.test/{size}/a.jpg") is None
        assert clean_image_url("https://cdn.test/a.jpg?w={width=}x{height=}") is None


class TestInternalHosts:
    """A retailer's images come off a CDN with a real name.

    A bare address is a request the shopper's browser would make into their own network on
    behalf of whatever a retailer payload said. Nothing legitimate looks like this.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "https://127.0.0.1/a.jpg",
            "https://127.0.0.1:8000/a.jpg",
            "https://localhost/a.jpg",
            "https://192.168.1.1/admin.jpg",
            "https://10.0.0.5/a.jpg",
            "https://172.16.0.1/a.jpg",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/a.jpg",
            "https://0.0.0.0/a.jpg",
        ],
        ids=[
            "loopback",
            "loopback-port",
            "localhost",
            "rfc1918-192",
            "rfc1918-10",
            "rfc1918-172",
            "link-local",
            "ipv6-loopback",
            "unspecified",
        ],
    )
    def test_rejected(self, raw: str) -> None:
        assert clean_image_url(raw) is None

    def test_a_real_cdn_still_passes(self) -> None:
        assert clean_image_url("https://images.cdn.smartandfinal.com/cell/a.jpeg") is not None
