"""Retailer adapters. Each retailer lives in its own package; nothing outside it knows the
retailer's URLs, payload shapes or quirks."""

from collections.abc import Callable
from urllib.parse import urlsplit

from app.normalize.availability import LIVE_STOCK, StockReporting
from app.retailers.base import RetailerAdapter
from app.retailers.clients import RetailerClients
from app.retailers.kroger.adapter import KrogerAdapter
from app.retailers.raleys.adapter import RaleysAdapter
from app.retailers.ranch99.adapter import Ranch99Adapter
from app.retailers.safeway.adapter import SafewayAdapter
from app.retailers.savemartco.adapter import LuckyAdapter, SaveMartAdapter
from app.retailers.smartandfinal.adapter import SmartAndFinalAdapter
from app.retailers.sprouts.adapter import SproutsAdapter
from app.retailers.target.adapter import TargetAdapter
from app.retailers.traderjoes.adapter import TraderJoesAdapter
from app.retailers.wholefoods.adapter import WholeFoodsAdapter

# The registry's single source: each class supplies its own slug and its own site.
#
# `target` is here and `walmart` is not, and the difference is measured rather than felt.
# Both adapters are written and tested against real captured payloads
# (`app/retailers/target/`, `app/retailers/walmart/`) and both were driven live.
#
# Target, on a signed-in Chrome session that persists between runs: 12 category loads, 9 of
# them clean, and the three that were not were the first three -- a cold session being asked
# to prove itself, which it then did for nine loads in a row without anyone touching it.
# What it returns is right and repeatable: a price and its own canonical URL for every row,
# and store stock that varies the way real stock does (milk 26 in stock / 3 out / 1 unknown).
# A challenge now costs only the category it interrupted: the browser layer isolates Target
# on its own page, the scrape records it as needing a person rather than as a fault, and the
# categories that did come back are ingested.
#
# Walmart stays out. On the same session it failed to identify its own store on one run and
# was challenged on the next, and its browse pages cannot express out-of-stock at all -- they
# omit what they lack rather than marking it.
#
# Target costs a normal scrape nothing: it needs the optional browser layer, so
# `is_configured()` is False unless `BROWSER_FALLBACK_ENABLED` is set, and the run skips it
# the way it skips Kroger without credentials.
_ADAPTER_CLASSES = (
    WholeFoodsAdapter,
    KrogerAdapter,
    Ranch99Adapter,
    SmartAndFinalAdapter,
    SproutsAdapter,
    TraderJoesAdapter,
    SafewayAdapter,
    LuckyAdapter,
    SaveMartAdapter,
    RaleysAdapter,
    TargetAdapter,
)

# Every adapter is constructed from the process's client pool and nothing else.
_ADAPTERS: dict[str, Callable[[RetailerClients], RetailerAdapter]] = {
    adapter.slug: adapter for adapter in _ADAPTER_CLASSES
}
# Read off the classes, so a retailer's product hosts are known without building it.
_SITE_URLS: dict[str, str] = {adapter.slug: adapter.site_url for adapter in _ADAPTER_CLASSES}
# Likewise for whether a retailer states stock at all: it is a property of the adapter, and
# reading it off the class means a search can answer without an HTTP client pool to build one.
_STOCK_REPORTING: dict[str, StockReporting] = {
    adapter.slug: adapter.stock_reporting for adapter in _ADAPTER_CLASSES
}


def adapter_slugs() -> list[str]:
    return list(_ADAPTERS)


def product_hosts(slug: str) -> frozenset[str]:
    """The hosts this retailer's product URLs are allowed to live on.

    Taken from the adapter's declared `site_url`, so there is one answer whether an adapter
    is building a URL or the scrape service is checking one it was handed.
    """
    host = urlsplit(_SITE_URLS.get(slug, "")).hostname
    return frozenset({host}) if host else frozenset()


def stock_reporting(slug: str) -> StockReporting:
    """Whether this retailer publishes per-store stock at all.

    It decides how an `unknown` offer is worded, and nothing else -- see
    `normalize/availability.py`. A slug with no adapter (a row left by a retailer that has
    since been removed) is reported as `live`, so its offers keep the cautious wording
    rather than being excused as "not published" on the strength of a missing adapter.
    """
    return _STOCK_REPORTING.get(slug, LIVE_STOCK)


def get_adapter(slug: str, clients: RetailerClients) -> RetailerAdapter:
    """Build an adapter bound to the process's long-lived HTTP clients.

    Adapter instances are cheap and hold no resource of their own: the client they use
    outlives them and is closed once, with `clients`.
    """
    try:
        adapter_class = _ADAPTERS[slug]
    except KeyError as exc:
        raise KeyError(f"unknown retailer '{slug}'; known: {', '.join(_ADAPTERS)}") from exc
    return adapter_class(clients)
