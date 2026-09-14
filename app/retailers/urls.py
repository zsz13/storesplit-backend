"""Product URL validation, shared by every adapter.

`ProductListing.product_url` is either a real page on the retailer's own host or `None`.
The three ways it went wrong are all closed here:

* a payload's nested URL *object* stringified into the field (Lucky's
  `productCanonicalUrl` is `{id, canonicalUrl, __typename}`, and `canonicalUrl` is usually
  null) -- anything that is not a `str` is rejected outright, and so is a `str` that is the
  repr of an object;
* a relative path stored raw, which the browser then resolves against StoreSplit's own
  origin (`http://localhost:3000/...`) -- relative paths are resolved against the retailer
  here, at parse time, or dropped;
* a URL on some other host, or in a scheme a browser must not follow.

Adapters call `clean_product_url` with their own site as the base. The scrape service calls
`valid_product_url` again before writing, so no path into the database can persist a value
these rules reject.

`clean_image_url` applies the same rules to `image_url`, minus the host equality check:
a retailer's images are legitimately served from somewhere else (`target.scene7.com`,
`images.cdn.smartandfinal.com`, a CloudFront distribution), so the host cannot be pinned to
the product host. Everything that made a product URL dangerous still applies -- an image URL
is a subresource the browser fetches, and an object stringified into the field resolves
against StoreSplit's own origin exactly as a bad product URL did.
"""

from collections.abc import Collection
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit, urlunsplit

# `retailer_products.product_url` is String(500); a longer value is not a URL worth keeping.
MAX_LENGTH = 500

_SCHEMES = frozenset({"https", "http"})
# Values retailers and serializers use to mean "nothing", which must never become a link.
_PLACEHOLDERS = frozenset(
    {"", "-", "--", "n/a", "na", "nan", "nil", "none", "null", "undefined", "[object object]"}
)


def clean_product_url(
    raw: object, *, base_url: str, extra_hosts: Collection[str] = ()
) -> str | None:
    """The retailer's own absolute product page, or None when the payload has none.

    `base_url` is the retailer's public site; a relative path is resolved against it and an
    absolute URL must live on its host (or on one of `extra_hosts`, for banners whose shop
    runs on a different subdomain than their marketing site).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.lower() in _PLACEHOLDERS or text[:1] in {"{", "["}:
        return None
    if any(character.isspace() or ord(character) < 0x20 for character in text):
        return None
    # A backslash is where this check and a browser disagree, so nothing containing one is
    # accepted. `urlsplit` ends the authority only at "/", "?" or "#" and reads the host
    # after the *last* "@", so `https://evil.test\@retailer.com/p` looks like the retailer
    # to Python; the WHATWG parser every browser uses treats "\" as an authority terminator
    # and navigates to evil.test instead. No real product URL carries a literal backslash.
    if "\\" in text:
        return None

    base = urlsplit(base_url)
    if not base.scheme or not base.hostname:
        raise ValueError(f"base_url must be absolute, got {base_url!r}")

    if "//" in text[:8] or ":" in text.split("/", 1)[0]:
        candidate = text  # absolute, protocol-relative, or a scheme we are about to reject
    elif "/" not in text:
        return None  # a bare token is an identifier, not a path
    else:
        candidate = urljoin(f"{base.scheme}://{base.netloc}/", text)

    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname  # raises on a bad bracketed host, like the port below
        port = parts.port
    except ValueError:
        # Unbalanced brackets, a non-numeric port, or a netloc that NFKC-normalizes into
        # delimiters. Returning None keeps one hostile listing from aborting a whole ingest.
        return None
    if parts.scheme.lower() not in _SCHEMES or not hostname:
        return None
    if parts.username is not None or parts.password is not None:
        # Credentials make the hover target read as the retailer while the request carries
        # someone else's basic auth. A product page never needs them.
        return None
    allowed = {base.hostname.lower(), *(host.lower() for host in extra_hosts)}
    if hostname.lower() not in allowed:
        return None
    if parts.path in {"", "/"}:
        return None
    _ = port  # parsed only so an invalid one raises above

    url = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment)
    )
    return url if len(url) <= MAX_LENGTH else None


def valid_product_url(raw: object, *, hosts: Collection[str]) -> bool:
    """Would this value be shown as a link for a retailer serving `hosts`?

    The check the scrape service runs before writing, and the one a stored row must still
    pass. Unlike `clean_product_url` it resolves nothing: a relative path that reached this
    point was never made absolute, and is rejected.
    """
    if not hosts:
        return False
    first, *rest = sorted(hosts)
    cleaned = clean_product_url(raw, base_url=f"https://{first}", extra_hosts=rest)
    return cleaned is not None and cleaned == raw


# `retailer_products.image_url` is String(500) too.
IMAGE_MAX_LENGTH = MAX_LENGTH
# Placeholders in a CDN template that an adapter was supposed to substitute. Left in, the
# URL is a 404 rather than an image: Smart & Final's `template` carries `{size}` and the
# Instacart storefronts' `templateUrl` carries `{width=}x{height=}`.
_TEMPLATE_MARKERS = ("{", "}")


def clean_image_url(raw: object) -> str | None:
    """An absolute https image URL, or None when the payload has none.

    Unlike `clean_product_url` this pins no host: image CDNs are other hosts by design. It
    resolves nothing either -- a relative path has no base to be resolved against here, and
    guessing one is how a retailer path became a link to StoreSplit itself.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.lower() in _PLACEHOLDERS or text[:1] in {"{", "["}:
        return None
    if any(character.isspace() or ord(character) < 0x20 for character in text):
        return None
    if "\\" in text:
        return None
    if any(marker in text for marker in _TEMPLATE_MARKERS):
        return None

    # `//host/path` means "same scheme as the page"; the page is https, so this is https.
    candidate = f"https:{text}" if text.startswith("//") else text
    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
        _ = parts.port  # parsed so an invalid one raises here rather than in the browser
    except ValueError:
        return None
    if parts.scheme.lower() not in _SCHEMES or not hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path in {"", "/"}:
        return None
    if _is_internal_host(hostname):
        # A retailer's images come off a CDN with a real name. A bare address -- and above
        # all a loopback or private one -- is not that: it is a request the shopper's browser
        # would make into their own network on behalf of whatever the payload said. No real
        # image URL looks like this, so refusing costs nothing.
        return None

    # Always https: an image over http on an https page is blocked as mixed content, so an
    # http URL is not a picture anybody would see.
    url = urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, parts.fragment))
    return url if len(url) <= IMAGE_MAX_LENGTH else None


def _is_internal_host(hostname: str) -> bool:
    """A raw address, or one that names somewhere only this machine can reach."""
    try:
        address = ip_address(hostname.strip("[]"))
    except ValueError:
        return hostname.lower() in {"localhost", "localhost."}
    return not (address.is_global and not address.is_private)


def valid_image_url(raw: object) -> bool:
    """Would this stored value be rendered as an image? The scrape service's write gate."""
    return clean_image_url(raw) == raw
