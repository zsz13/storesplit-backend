"""The migrations, run as migrations.

`conftest` builds the schema with `Base.metadata.create_all`, which never executes an Alembic
revision. That is fine for the ORM, but it means the part of a migration that repairs existing
rows -- the reason this one exists -- was covered by nothing. These tests run the real
`alembic upgrade` against a real database and assert on the data afterwards.

They are synchronous on purpose: `alembic/env.py` ends in `asyncio.run(...)`, so it cannot be
invoked from inside a running event loop. Everything here drives the project's own async
engine through `asyncio.run` instead of pulling in a second, synchronous driver.
"""

import asyncio
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.db.safety import require_disposable_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in TEST_URL,
    reason="TEST_DATABASE_URL must point at PostgreSQL; migrations are not run on SQLite",
)

BASELINE = "19cc31159d59"
AVAILABILITY = "774a717a7963"

POISON = "{'id': 'dce2530a', 'canonicalUrl': None, '__typename': 'LandingProductCanonicalUrl'}"
GOOD = "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/1"
RELATIVE = "/store/lucky-supermarkets/products/2"


async def _run(statements: list[str]) -> list[list]:
    engine = create_async_engine(TEST_URL)
    results: list[list] = []
    try:
        async with engine.begin() as connection:
            for statement in statements:
                cursor = await connection.execute(text(statement))
                results.append(list(cursor.scalars()) if cursor.returns_rows else [])
    finally:
        await engine.dispose()
    return results


def run_sql(*statements: str) -> list[list]:
    return asyncio.run(_run(list(statements)))


@pytest.fixture
def alembic_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """An empty database at the pre-availability baseline."""
    # `DROP SCHEMA public CASCADE` below is irreversible and takes every user relation with
    # it, so nothing here runs until `app.db.safety` has proved that
    # TEST_URL names a database that exists to be thrown away -- and this asks *before*
    # the monkeypatch below, so what it compares against is the real DATABASE_URL.
    asyncio.run(require_disposable_test_database(TEST_URL))
    # `alembic/env.py` reads the URL from application settings, not from alembic.ini.
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    run_sql("DROP SCHEMA public CASCADE", "CREATE SCHEMA public")
    command.upgrade(config, BASELINE)
    yield config
    run_sql("DROP SCHEMA public CASCADE", "CREATE SCHEMA public")
    get_settings.cache_clear()


def sql_literal(value: str | None) -> str:
    """A SQL string literal. `repr` is not one: it double-quotes anything with an apostrophe,
    which PostgreSQL reads as an identifier -- and the poison value is full of apostrophes."""
    if value is None:
        return "NULL"
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def seed(*product_urls: str | None) -> None:
    values = ", ".join(
        f"(1, 'sku-{index}', 'Eggs', {sql_literal(url)}, 'new', 'test', '[]', now())"
        for index, url in enumerate(product_urls)
    )
    run_sql(
        "INSERT INTO retailers (id, slug, name, created_at) VALUES (1, 'lucky', 'Lucky', now())",
        "INSERT INTO retailer_products (retailer_id, retailer_sku, title, product_url, "
        f"match_status, scrape_source, match_candidates, last_scraped_at) VALUES {values}",
    )


def test_the_upgrade_clears_urls_that_were_never_urls(alembic_config: Config) -> None:
    """The stringified `productCanonicalUrl` object, on rows written before the fix."""
    seed(POISON, GOOD, RELATIVE, None)

    command.upgrade(alembic_config, AVAILABILITY)

    urls = run_sql("SELECT product_url FROM retailer_products ORDER BY id")[0]
    assert GOOD in urls, "a real https URL must survive the cleanup"
    assert not any(url and "canonicalUrl" in url for url in urls), "the object repr must go"
    assert not any(url and url.startswith("/") for url in urls), "relative paths must go"
    assert all(url is None or url.startswith("https://") for url in urls)
    assert urls.count(None) == 3


def test_the_upgrade_leaves_existing_offers_unknown_not_in_stock(
    alembic_config: Config,
) -> None:
    """Nothing recorded what those scrapes saw, so nothing may claim they were stocked."""
    seed(GOOD)
    run_sql(
        "INSERT INTO stores (id, retailer_id, external_id, name, served_zip_codes, "
        "created_at) VALUES (1, 1, 'S1', 'Store', '[]', now())",
        "INSERT INTO offers (retailer_product_id, store_id, price, regular_price, currency, "
        "scrape_source, scraped_at) SELECT id, 1, 1.99, 1.99, 'USD', 'test', now() "
        "FROM retailer_products",
    )

    command.upgrade(alembic_config, AVAILABILITY)

    assert run_sql("SELECT availability FROM offers")[0] == ["unknown"]


def test_the_downgrade_removes_the_column(alembic_config: Config) -> None:
    command.upgrade(alembic_config, AVAILABILITY)
    command.downgrade(alembic_config, BASELINE)
    columns = run_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'offers'"
    )[0]
    assert "availability" not in columns


# ---------------------------------- the weight-priced size repair (c3a1d0e7f482)

PRICE_BASIS = "8fc71eae3f69"
WEIGHT_REPAIR = "c3a1d0e7f482"
STORE_DETAILS_REREAD = "d7b21f0c4e93"
SAVEMARTCO_STORE_KEYS = "f1c9d3a7e5b4"
TRADERJOES_REREAD = "a4e91b2c7d68"


def seed_weight_priced() -> None:
    """A canonical product as the pre-fix code wrote one: a tray of chicken sold at $2.59/lb,
    recorded as a 5.25 lb package because 5.25 was the biggest number in its title."""
    run_sql(
        "INSERT INTO retailers (id, slug, name, created_at) VALUES (1, 'target', 'Target', now())",
        "INSERT INTO stores (id, retailer_id, external_id, name, served_zip_codes, created_at) "
        "VALUES (1, 1, '3264', 'Target San Francisco Stonestown', '[]', now())",
        # The tray: weight-priced, and wrong.
        "INSERT INTO canonical_products (id, category, normalized_name, quantity, quantity_unit, "
        "count, comparison_unit, comparison_quantity, attributes, created_at) VALUES "
        "(1, 'chicken_breast', 'chicken breast value pack', 5.25, 'lb', NULL, 'lb', 5.25, "
        '\'{"sold_by": "weight"}\', now())',
        # A weighed product a category compares in ounces, to prove the conversion.
        "INSERT INTO canonical_products (id, category, normalized_name, quantity, quantity_unit, "
        "count, comparison_unit, comparison_quantity, attributes, created_at) VALUES "
        "(2, 'bread', 'sliced turkey', 3, 'lb', NULL, 'oz', 48, "
        '\'{"sold_by": "weight"}\', now())',
        # A fixed package, which must not be touched by any of this.
        "INSERT INTO canonical_products (id, category, normalized_name, quantity, quantity_unit, "
        "count, comparison_unit, comparison_quantity, attributes, created_at) VALUES "
        "(3, 'eggs', 'large grade a eggs', 12, 'count', 12, 'count', 12, "
        '\'{"sold_by": "unit"}\', now())',
        "INSERT INTO retailer_products (id, retailer_id, canonical_product_id, retailer_sku, "
        "title, match_status, scrape_source, match_candidates, last_scraped_at) VALUES "
        "(1, 1, 1, '86676070', 'Chicken Breast Value Pack - 2.5-5.25lbs - price per lb', "
        "'auto', 'test', '[]', now()), "
        "(2, 1, 2, 'turkey', 'Sliced Turkey Breast', 'auto', 'test', '[]', now()), "
        "(3, 1, 3, 'eggs', 'Large Grade A Eggs - 12ct', 'auto', 'test', '[]', now())",
        # $2.59 a pound, stored as if it were $2.59 for the tray and divided by 5.25 lb.
        "INSERT INTO offers (retailer_product_id, store_id, price, regular_price, currency, "
        "unit_price, unit_price_unit, scrape_source, scraped_at) VALUES "
        "(1, 1, 2.59, 2.59, 'USD', 0.4933, 'lb', 'test', now()), "
        "(2, 1, 9.99, 9.99, 'USD', 0.2081, 'oz', 'test', now()), "
        "(3, 1, 5.89, 5.89, 'USD', 0.4908, 'count', 'test', now())",
    )


def test_the_repair_restores_a_weighed_products_size_to_one_pound(
    alembic_config: Config,
) -> None:
    """A tray sold at a rate is a pound, not whichever weight its title happened to name.
    `comparison_quantity` is what a basket divides by, and it never heals on its own: the
    scrape keeps an existing SKU-to-canonical mapping deliberately."""
    command.upgrade(alembic_config, PRICE_BASIS)
    seed_weight_priced()

    command.upgrade(alembic_config, WEIGHT_REPAIR)

    sizes = run_sql(
        "SELECT quantity || ' ' || quantity_unit || ' -> ' || comparison_quantity "
        "FROM canonical_products ORDER BY id"
    )[0]
    assert sizes[0].startswith("1.0000 lb -> 1.0"), sizes[0]
    assert sizes[1].startswith("1.0000 lb -> 16.0"), "one pound is sixteen ounces"
    assert sizes[2].startswith("12.0000 count -> 12.0"), "a fixed package is left alone"
    assert run_sql("SELECT count FROM canonical_products ORDER BY id")[0] == [None, None, 12]


def test_the_repair_leaves_every_offer_alone(alembic_config: Config) -> None:
    """The repair cannot tell a double-divided rate from a correctly divided package total:
    both satisfy `unit_price == price / comparison_quantity`, because that is exactly how a
    package's unit price is computed. A canonical product is shared between retailer SKUs, so
    restating on that basis would relabel a genuine total as a per-pound rate -- the original
    bug aimed the other way. Offers carry their basis from the adapter and the next scrape of
    their store and category rewrites them, which is the path that knows the answer.
    """
    command.upgrade(alembic_config, PRICE_BASIS)
    seed_weight_priced()
    before = run_sql(
        "SELECT price_basis || ' ' || unit_price FROM offers ORDER BY retailer_product_id"
    )[0]

    command.upgrade(alembic_config, WEIGHT_REPAIR)

    after = run_sql(
        "SELECT price_basis || ' ' || unit_price FROM offers ORDER BY retailer_product_id"
    )[0]
    assert after == before == ["package 0.4933", "package 0.2081", "package 0.4908"]


def test_the_repair_leaves_price_history_alone(alembic_config: Config) -> None:
    """History is a record of what was collected, not of what is understood today."""
    command.upgrade(alembic_config, PRICE_BASIS)
    seed_weight_priced()
    run_sql(
        "INSERT INTO price_history (retailer_product_id, store_id, price, regular_price, "
        "unit_price, scrape_source, scraped_at) VALUES (1, 1, 2.59, 2.59, 0.4933, 'test', now())"
    )

    command.upgrade(alembic_config, WEIGHT_REPAIR)

    assert run_sql("SELECT unit_price FROM price_history")[0] == [Decimal("0.4933")]


def test_the_repair_downgrade_is_schema_only_and_does_not_raise(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, WEIGHT_REPAIR)
    command.downgrade(alembic_config, PRICE_BASIS)

    columns = run_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'offers'"
    )[0]
    assert "price_basis" in columns, "the downgrade steps back one revision, not two"


# -- d7b21f0c4e93: re-read store details for a retailer that has just gained the capability


def seed_stamped_stores() -> None:
    """Two retailers' stores, both stamped by a scrape that ran before Sprouts could read
    its own store pages, and one store nobody has ever stamped."""
    run_sql(
        "INSERT INTO retailers (id, slug, name, created_at) VALUES "
        "(1, 'sprouts', 'Sprouts Farmers Market', now()), (2, 'lucky', 'Lucky', now())",
        "INSERT INTO stores (id, retailer_id, external_id, name, timezone, hours, "
        "hours_source, hours_updated_at, latitude, longitude, served_zip_codes, "
        "created_at) VALUES "
        "(1, 1, '601', 'Sprouts Daly City', NULL, NULL, NULL, now(), 37.66, -122.46, "
        "'[]', now()), "
        "(2, 1, '219', 'Sprouts Oakland', NULL, NULL, NULL, now(), NULL, NULL, '[]', now()), "
        "(3, 2, '23130', 'Lucky 23130', NULL, NULL, NULL, now(), NULL, NULL, '[]', now()), "
        "(4, 1, '999', 'Sprouts Never Read', NULL, NULL, NULL, NULL, NULL, NULL, '[]', now())",
    )


def test_a_retailer_that_gained_store_details_is_unstamped_so_it_is_read_again(
    alembic_config: Config,
) -> None:
    """The stamp said "asked recently"; it meant "asked with code that could not answer"."""
    command.upgrade(alembic_config, WEIGHT_REPAIR)
    seed_stamped_stores()

    command.upgrade(alembic_config, STORE_DETAILS_REREAD)

    stamped = run_sql(
        "SELECT external_id FROM stores WHERE hours_updated_at IS NOT NULL ORDER BY id"
    )[0]
    assert stamped == ["23130"], "only Sprouts is unstamped; Lucky's gate is untouched"


def test_unstamping_throws_away_no_answer_it_is_still_waiting_for(
    alembic_config: Config,
) -> None:
    """Clearing a stamp asks a question. Everything the last answer left stays put until a
    successful read replaces it -- including the coordinates a store is ranked and pinned by.

    Each column is asserted with its own query on purpose: `run_sql` returns `scalars()`, so
    a single five-column SELECT would silently prove one column and ignore four.
    """
    command.upgrade(alembic_config, WEIGHT_REPAIR)
    seed_stamped_stores()
    run_sql(
        "UPDATE stores SET timezone = 'America/Los_Angeles', hours_source = 'old:source', "
        'hours = \'{"weekly": {"0": {"opens": "08:00", "closes": "20:00"}}}\' '
        "WHERE id = 1"
    )

    command.upgrade(alembic_config, STORE_DETAILS_REREAD)

    assert run_sql("SELECT timezone FROM stores WHERE id = 1")[0] == ["America/Los_Angeles"]
    assert run_sql("SELECT hours_source FROM stores WHERE id = 1")[0] == ["old:source"]
    assert run_sql("SELECT latitude::text FROM stores WHERE id = 1")[0] == ["37.66"]
    assert run_sql("SELECT longitude::text FROM stores WHERE id = 1")[0] == ["-122.46"]
    assert run_sql("SELECT hours::text FROM stores WHERE id = 1")[0] != [None]
    assert run_sql("SELECT hours_updated_at FROM stores WHERE id = 1")[0] == [None]


def test_the_downgrade_does_not_raise_and_changes_no_data(alembic_config: Config) -> None:
    command.upgrade(alembic_config, STORE_DETAILS_REREAD)
    seed_stamped_stores()

    command.downgrade(alembic_config, WEIGHT_REPAIR)

    assert run_sql("SELECT count(*) FROM stores WHERE hours_updated_at IS NOT NULL")[0] == [3]


# -- a4e91b2c7d68: re-read Trader Joe's, whose published week has just become readable


def seed_traderjoes_stores() -> None:
    """Trader Joe's stores as an existing database holds them: stamped by weekly reads that
    each found a complete week, no timezone to read it in, and so no hours to write.

    Store 225 carries hours bought from Google for a store whose own week was sitting unread
    in the locator all along -- the row that most wants the re-read, not the least.
    """
    run_sql(
        "INSERT INTO retailers (id, slug, name, created_at) VALUES "
        "(1, 'traderjoes', 'Trader Joe''s', now()), (2, 'raleys', 'Raley''s', now())",
        "INSERT INTO stores (id, retailer_id, external_id, name, timezone, hours, "
        "hours_source, hours_updated_at, latitude, longitude, served_zip_codes, "
        "created_at) VALUES "
        "(1, 1, '78', 'Trader Joe''s San Francisco - 9th St (78)', NULL, NULL, "
        "'traderjoes:locator', now(), 37.77111, -122.40738, '[]', now()), "
        "(2, 1, '225', 'Trader Joe''s San Francisco - Pacific Place (225)', "
        '\'America/Los_Angeles\', \'{"weekly": {"0": {"opens": "08:00", '
        '"closes": "22:00"}}}\', \'google:places/details\', now(), 37.78533, -122.40567, '
        "'[]', now()), "
        "(3, 2, '201', 'Raley''s Somewhere', NULL, NULL, 'raleys:sitemap', now(), NULL, NULL, "
        "'[]', now()), "
        "(4, 1, '999', 'Trader Joe''s Never Read (999)', NULL, NULL, NULL, NULL, NULL, NULL, "
        "'[]', now())",
    )


def test_trader_joes_is_unstamped_so_its_own_week_is_read_once_more(
    alembic_config: Config,
) -> None:
    """The stamp said "asked recently"; it meant "asked while the answer was unreadable"."""
    command.upgrade(alembic_config, SAVEMARTCO_STORE_KEYS)
    seed_traderjoes_stores()

    command.upgrade(alembic_config, TRADERJOES_REREAD)

    stamped = run_sql(
        "SELECT external_id FROM stores WHERE hours_updated_at IS NOT NULL ORDER BY id"
    )[0]
    assert stamped == ["201"], "only Trader Joe's is unstamped; Raley's gate is untouched"


def test_a_google_sourced_week_survives_until_the_retailers_own_replaces_it(
    alembic_config: Config,
) -> None:
    """Clearing a stamp asks a question, and the store keeps answering with what it has while
    it waits. Nothing is blanked here -- the next successful read is what overwrites it."""
    command.upgrade(alembic_config, SAVEMARTCO_STORE_KEYS)
    seed_traderjoes_stores()

    command.upgrade(alembic_config, TRADERJOES_REREAD)

    assert run_sql("SELECT timezone FROM stores WHERE id = 2")[0] == ["America/Los_Angeles"]
    assert run_sql("SELECT hours_source FROM stores WHERE id = 2")[0] == ["google:places/details"]
    assert run_sql("SELECT hours::text FROM stores WHERE id = 2")[0] != [None]
    assert run_sql("SELECT latitude::text FROM stores WHERE id = 2")[0] == ["37.78533"]
    assert run_sql("SELECT hours_updated_at FROM stores WHERE id = 2")[0] == [None]


def test_the_trader_joes_downgrade_does_not_raise_and_changes_no_data(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, TRADERJOES_REREAD)
    seed_traderjoes_stores()

    command.downgrade(alembic_config, SAVEMARTCO_STORE_KEYS)

    assert run_sql("SELECT count(*) FROM stores WHERE hours_updated_at IS NOT NULL")[0] == [3]


# -- d2b8c1a5e307: a history row says what its number means


STOCK_STATUS = "b7d3f0a92c15"
HISTORY_BASIS = "d2b8c1a5e307"


def seed_history_without_a_basis() -> None:
    """History as it stood before this revision: a per-pound rate, a package total, and a row
    whose offer has since been expired -- five numbers each and nothing saying what they are.
    """
    run_sql(
        "INSERT INTO retailers (id, slug, name, created_at) VALUES (1, 'target', 'Target', now())",
        "INSERT INTO stores (id, retailer_id, external_id, name, served_zip_codes, created_at) "
        "VALUES (1, 1, '3264', 'Stonestown', '[]', now()), "
        "(2, 1, '1234', 'Serramonte', '[]', now())",
        "INSERT INTO canonical_products (id, category, normalized_name, quantity, quantity_unit, "
        "comparison_unit, comparison_quantity, attributes, created_at) VALUES "
        "(1, 'chicken_breast', 'chicken breast', 1, 'lb', 'lb', 1, '{}', now())",
        "INSERT INTO retailer_products (id, retailer_id, canonical_product_id, retailer_sku, "
        "title, match_status, scrape_source, match_candidates, last_scraped_at) VALUES "
        "(1, 1, 1, '86676070', 'Chicken Breast Value Pack', 'auto', 'test', '[]', now())",
        # The live offer: a rate per pound, and it knows it.
        "INSERT INTO offers (retailer_product_id, store_id, price, regular_price, currency, "
        "price_basis, unit_price, unit_price_unit, scrape_source, scraped_at) VALUES "
        "(1, 1, 2.59, 2.59, 'USD', 'lb', 2.5900, 'lb', 'test', now())",
        # Three history rows at that store, and one at a store whose offer is gone.
        "INSERT INTO price_history (retailer_product_id, store_id, price, regular_price, "
        "unit_price, scrape_source, scraped_at) VALUES "
        "(1, 1, 2.29, 2.29, 2.2900, 'test', '2026-08-01 10:00+00'), "
        "(1, 1, 2.49, 2.49, 2.4900, 'test', '2026-08-15 10:00+00'), "
        "(1, 1, 2.59, 2.59, 2.5900, 'test', '2026-09-01 10:00+00'), "
        "(1, 2, 3.19, 3.19, 3.1900, 'test', '2026-08-20 10:00+00')",
    )


def test_existing_history_survives_and_keeps_its_timestamps(alembic_config: Config) -> None:
    """The whole point of the table. Nothing is deleted, merged, or re-dated."""
    command.upgrade(alembic_config, STOCK_STATUS)
    seed_history_without_a_basis()
    before = run_sql("SELECT price || '@' || scraped_at FROM price_history ORDER BY id")[0]

    command.upgrade(alembic_config, HISTORY_BASIS)

    assert run_sql("SELECT price || '@' || scraped_at FROM price_history ORDER BY id")[0] == before
    assert run_sql("SELECT count(*) FROM price_history")[0] == [4]


def test_the_basis_is_backfilled_from_the_offer_that_wrote_the_row(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, STOCK_STATUS)
    seed_history_without_a_basis()

    command.upgrade(alembic_config, HISTORY_BASIS)

    assert run_sql(
        "SELECT price_basis || ' ' || coalesce(unit_price_unit, '-') FROM price_history "
        "WHERE store_id = 1 ORDER BY id"
    )[0] == ["lb lb", "lb lb", "lb lb"]


def test_a_row_whose_offer_is_gone_records_no_basis_at_all(alembic_config: Config) -> None:
    """There is no offer left to read the basis off. `package` would not be an absence -- a
    reader prints it as "for the pack" over what may be a per-pound rate -- so the row says
    nothing instead, and the API sends null."""
    command.upgrade(alembic_config, STOCK_STATUS)
    seed_history_without_a_basis()

    command.upgrade(alembic_config, HISTORY_BASIS)

    basis, unit = run_sql(
        "SELECT price_basis FROM price_history WHERE store_id = 2",
        "SELECT unit_price_unit FROM price_history WHERE store_id = 2",
    )
    assert basis == [None] and unit == [None]


def test_the_series_index_replaces_the_pair_index(alembic_config: Config) -> None:
    command.upgrade(alembic_config, HISTORY_BASIS)

    indexes = run_sql("SELECT indexname FROM pg_indexes WHERE tablename = 'price_history'")[0]
    assert "ix_price_history_series" in indexes
    assert "ix_price_history_product_store" not in indexes


def test_the_history_downgrade_restores_the_old_shape_and_keeps_the_rows(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, STOCK_STATUS)
    seed_history_without_a_basis()
    command.upgrade(alembic_config, HISTORY_BASIS)

    command.downgrade(alembic_config, STOCK_STATUS)

    assert run_sql("SELECT count(*) FROM price_history")[0] == [4]
    columns = run_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'price_history'"
    )[0]
    assert "price_basis" not in columns and "unit_price_unit" not in columns
    indexes = run_sql("SELECT indexname FROM pg_indexes WHERE tablename = 'price_history'")[0]
    assert "ix_price_history_product_store" in indexes
