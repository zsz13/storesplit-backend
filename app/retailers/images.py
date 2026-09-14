"""Reading an image URL out of whatever shape a retailer put it in.

Retailers spell an image in every shape a JSON payload allows: a bare string
(Whole Foods `imageThumbnail`, Safeway `imageUrl`), an object with the URL under one of
several keys (Sprouts/Lucky/Save Mart `itemImage.url`, Target `primary_image.url`,
Raley's `images[].url`), and objects nested inside lists inside objects (Kroger's
`images[].sizes[].url`).

Adapters used to unwrap this by hand, and three of them called `str()` on the value first.
`str()` on a dict yields `"{'url': None}"` -- a non-empty string that a `String(500)` column
accepts happily and a browser then resolves against StoreSplit's own origin. This module is
the single owner of the unwrapping, so an adapter cannot reintroduce that: it returns a real
`str` or `None`, never a repr.
"""

from typing import Any

from app.retailers.urls import clean_image_url

# Keys retailers put an image URL under, in the order they should be preferred. `template`
# and `templateUrl` come last: they carry placeholders (`{size}`, `{width=}x{height=}`) and
# are only useful once an adapter has substituted them.
_URL_KEYS = ("url", "path", "imageUrl", "image_url", "src", "href", "template", "templateUrl")
# Keys whose value is a container of images rather than an image.
_CONTAINER_KEYS = ("sizes", "images", "image", "primary_image", "primaryImage", "itemImage")
# A payload cannot nest usefully deeper than this; a cycle-shaped or absurd structure stops
# here rather than exhausting the stack during an ingest.
_MAX_DEPTH = 8


def image_url_from(raw: Any, *, _depth: int = 0) -> str | None:
    """The image URL inside `raw`, or None when there is not one.

    Never returns the repr of an object: a value that is not a string, and contains no
    string under a known key, is nothing.
    """
    if _depth > _MAX_DEPTH:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        for key in _URL_KEYS:
            if key in raw:
                url = image_url_from(raw[key], _depth=_depth + 1)
                if url is not None:
                    return url
        for key in _CONTAINER_KEYS:
            if key in raw:
                url = image_url_from(raw[key], _depth=_depth + 1)
                if url is not None:
                    return url
        return None
    if isinstance(raw, list | tuple):
        for entry in raw:
            url = image_url_from(entry, _depth=_depth + 1)
            if url is not None:
                return url
    return None


def first_image_url(*candidates: Any) -> str | None:
    """The first candidate that yields a URL.

    Adapters read an image from more than one field (Smart & Final's `image` then
    `primaryImage`); falling back on the *container* rather than the value is what silently
    dropped images when the first container existed but held no URL.
    """
    for candidate in candidates:
        url = image_url_from(candidate)
        if url is not None:
            return url
    return None


def listing_image_url(*candidates: Any) -> str | None:
    """What an adapter puts on `ProductListing.image_url`: unwrapped, then validated.

    The image counterpart of `clean_product_url`. Adapters call this instead of reaching
    into a payload themselves, and `app/services/scraper.py` checks the result again with
    `valid_image_url` before writing, so no path can persist a value these rules reject.
    """
    return clean_image_url(first_image_url(*candidates))
