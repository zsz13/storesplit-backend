"""One card per canonical product, paginated by product, with the best buyable offer on top.

The result of a search is a canonical product, never an offer. A product carried by four
stores is one card with four offers inside it, not four results -- and it occupies one page
slot, so a page boundary can never fall in the middle of a product's own offers.
"""

from decimal import Decimal

import pytest
from app.db.models import CanonicalProduct, Offer, RetailerProduct, Store
from app.normalize.availability import IN_STOCK, OUT_OF_STOCK, UNKNOWN
from app.services import scraper
from app.services.scraper import run_scrape
from app.services.search import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeAdapter, two_retailers

ZIP = "94105"


@pytest.fixture
async def scraped(sessionmaker, clients, monkeypatch: pytest.MonkeyPatch) -> None:
    adapters: dict[str, FakeAdapter] = {a.slug: a for a in two_retailers()}
    monkeypatch.setattr(scraper, "adapter_slugs", lambda: list(adapters))
    monkeypatch.setattr(scraper, "get_adapter", lambda slug, clients: adapters[slug])
    await run_scrape(sessionmaker, clients, ZIP, None, ["eggs", "chicken_breast", "milk"])


async def _search(client: AsyncClient, **params) -> dict:
    response = await client.get("/products/search", params={"q": "eggs", "zip_code": ZIP, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def _clone_offer_to_a_second_store(db: AsyncSession, price: str) -> CanonicalProduct:
    """Give one canonical product a second store's offer, the way two branches of one
    retailer produce two rows for the same carton."""
    offer = (
        await db.scalars(select(Offer).join(Offer.retailer_product).order_by(Offer.id).limit(1))
    ).one()
    origin = await db.get(Store, offer.store_id)
    assert origin is not None
    twin = Store(
        retailer_id=origin.retailer_id,
        external_id=f"{origin.external_id}-2",
        name=f"{origin.name} (second branch)",
        zip_code=origin.zip_code,
        served_zip_codes=[ZIP],
    )
    db.add(twin)
    await db.flush()
    db.add(
        Offer(
            retailer_product_id=offer.retailer_product_id,
            store_id=twin.id,
            price=Decimal(price),
            regular_price=Decimal(price),
            currency="USD",
            availability=IN_STOCK,
            unit_price=offer.unit_price,
            unit_price_unit=offer.unit_price_unit,
            scrape_source="test",
        )
    )
    await db.commit()
    rp = await db.get(RetailerProduct, offer.retailer_product_id)
    assert rp is not None and rp.canonical_product_id is not None
    product = await db.get(CanonicalProduct, rp.canonical_product_id)
    assert product is not None
    return product


class TestGrouping:
    async def test_a_product_appears_once_however_many_stores_carry_it(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        product = await _clone_offer_to_a_second_store(db, "5.49")

        body = await _search(client)

        matches = [p for p in body["products"] if p["id"] == product.id]
        assert len(matches) == 1, "one canonical product is one card, not one card per store"
        assert matches[0]["offer_count"] >= 2
        assert matches[0]["store_count"] >= 2

    async def test_every_product_in_a_page_is_distinct(self, client: AsyncClient, scraped) -> None:
        ids = [p["id"] for p in (await _search(client, availability="all"))["products"]]
        assert len(ids) == len(set(ids))

    async def test_offers_are_grouped_inside_their_product(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        product = await _clone_offer_to_a_second_store(db, "5.49")

        body = await _search(client)
        card = next(p for p in body["products"] if p["id"] == product.id)

        stores = {offer["store"]["id"] for offer in card["offers"]}
        assert len(stores) == card["store_count"]
        assert card["retailer_count"] >= 1

    async def test_different_package_sizes_stay_different_products(
        self, client: AsyncClient, scraped
    ) -> None:
        """The grouping must not swallow a genuine difference: 12 ct and 18 ct are not the
        same thing, and comparing them is exactly what the unit price is for."""
        body = await _search(client, availability="all")
        counts = {p["count"] for p in body["products"] if (p["brand"] or "").lower() == "farm co"}
        assert {12, 18} <= counts

    async def test_a_card_summarizes_its_offers_without_being_expanded(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        product = await _clone_offer_to_a_second_store(db, "5.49")

        card = next(p for p in (await _search(client))["products"] if p["id"] == product.id)

        assert card["offer_count"] == len(card["offers"])
        assert card["in_stock_offer_count"] >= 1
        assert card["price_low"] is not None and card["price_high"] is not None
        assert Decimal(card["price_low"]) <= Decimal(card["price_high"])


class TestBestOffer:
    async def test_the_collapsed_card_shows_the_best_in_stock_offer(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        product = await _clone_offer_to_a_second_store(db, "0.99")

        card = next(p for p in (await _search(client))["products"] if p["id"] == product.id)

        assert card["best_offer"] is not None
        assert card["best_offer"]["id"] == card["best_offer_id"]
        assert card["best_offer"]["availability"] == IN_STOCK

        def rank(offer: dict) -> Decimal:
            return Decimal(offer["unit_price"] or offer["price"])

        buyable = [o for o in card["offers"] if o["availability"] == IN_STOCK]
        assert rank(card["best_offer"]) == min(rank(o) for o in buyable), (
            "the leader is the cheapest per unit, not the smallest sticker price"
        )

    async def test_an_out_of_stock_offer_never_wins_even_when_cheapest(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        """A $1.99 carton nobody can buy is not a better answer than a $4.99 one on a shelf."""
        product = await _clone_offer_to_a_second_store(db, "0.01")
        clone = (
            await db.scalars(select(Offer).where(Offer.price == Decimal("0.01")).limit(1))
        ).one()
        clone.availability = OUT_OF_STOCK
        await db.commit()

        card = next(
            p
            for p in (await _search(client, availability="all"))["products"]
            if p["id"] == product.id
        )

        assert card["best_offer"] is not None
        assert card["best_offer"]["availability"] == IN_STOCK
        assert Decimal(card["best_offer"]["price"]) > Decimal("0.01")
        assert any(
            o["availability"] == OUT_OF_STOCK and Decimal(o["price"]) == Decimal("0.01")
            for o in card["offers"]
        ), "the cheaper unbuyable offer is still shown, just not as the answer"

    async def test_a_product_with_nothing_buyable_gets_no_best_offer(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        await db.execute(select(Offer))  # ensure the session is live before the bulk update below
        for offer in await db.scalars(select(Offer)):
            offer.availability = UNKNOWN
        await db.commit()

        body = await _search(client, availability="unknown")

        assert body["products"], "the products are still returned, just unbadged"
        for card in body["products"]:
            assert card["best_offer"] is None
            assert card["best_offer_id"] is None

    async def test_offers_inside_a_card_are_in_stock_first_then_by_unit_price(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        product = await _clone_offer_to_a_second_store(db, "0.01")
        clone = (
            await db.scalars(select(Offer).where(Offer.price == Decimal("0.01")).limit(1))
        ).one()
        clone.availability = OUT_OF_STOCK
        await db.commit()

        card = next(
            p
            for p in (await _search(client, availability="all"))["products"]
            if p["id"] == product.id
        )

        states = [o["availability"] for o in card["offers"]]
        assert states.index(IN_STOCK) < states.index(OUT_OF_STOCK)
        in_stock = [o for o in card["offers"] if o["availability"] == IN_STOCK]
        keys = [Decimal(o["unit_price"] or o["price"]) for o in in_stock]
        assert keys == sorted(keys)


class TestPagination:
    async def test_pages_are_counted_in_products_not_offers(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        await _clone_offer_to_a_second_store(db, "5.49")

        body = await _search(client, availability="all", page_size=1)

        assert len(body["products"]) == 1
        assert body["page"]["page_size"] == 1
        # The page holds one product and all of its offers, however many that is.
        assert body["page"]["total_products"] == len(
            {
                p["id"]
                for p in (await _search(client, availability="all", page_size=MAX_PAGE_SIZE))[
                    "products"
                ]
            }
        )

    async def test_walking_the_pages_visits_every_product_exactly_once(
        self, client: AsyncClient, scraped
    ) -> None:
        first = await _search(client, availability="all", page_size=2)
        total_pages = first["page"]["total_pages"]

        seen: list[int] = []
        for page in range(1, total_pages + 1):
            body = await _search(client, availability="all", page_size=2, page=page)
            seen.extend(p["id"] for p in body["products"])

        assert len(seen) == len(set(seen)), "a product must not appear on two pages"
        assert len(seen) == first["page"]["total_products"]

    async def test_page_metadata_is_consistent(self, client: AsyncClient, scraped) -> None:
        body = await _search(client, availability="all", page_size=1, page=1)
        page = body["page"]

        assert page["page"] == 1
        assert page["has_previous"] is False
        assert page["has_next"] is (page["total_pages"] > 1)
        assert page["total_pages"] == max(1, -(-page["total_products"] // page["page_size"]))

    async def test_the_last_page_reports_no_next(self, client: AsyncClient, scraped) -> None:
        first = await _search(client, availability="all", page_size=2)
        last = await _search(
            client, availability="all", page_size=2, page=first["page"]["total_pages"]
        )
        assert last["page"]["has_next"] is False

    async def test_a_page_past_the_end_is_empty_rather_than_an_error(
        self, client: AsyncClient, scraped
    ) -> None:
        body = await _search(client, page=999)
        assert body["products"] == []
        assert body["page"]["total_products"] > 0

    async def test_the_default_page_size_is_used_when_none_is_given(
        self, client: AsyncClient, scraped
    ) -> None:
        assert (await _search(client))["page"]["page_size"] == DEFAULT_PAGE_SIZE

    async def test_page_size_is_capped(self, client: AsyncClient, scraped) -> None:
        response = await client.get(
            "/products/search",
            params={"q": "eggs", "zip_code": ZIP, "page_size": MAX_PAGE_SIZE + 1},
        )
        assert response.status_code == 422

    async def test_the_unknown_section_appears_only_on_the_first_page(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        """A footnote that repeated under every page would stop being a footnote.

        One whole product is made unknown -- every offer of it, so it drops out of the
        confirmed results entirely and can only appear in the section under test. Flipping a
        single offer would not do it: the product would keep an in-stock offer, stay in the
        confirmed list, and then be excluded from the section as already shown.
        """
        # An eggs product specifically: the search under test is for eggs, so a milk product
        # would be filtered out by category and prove nothing.
        product_id = (
            await db.scalars(
                select(CanonicalProduct.id)
                .join(CanonicalProduct.retailer_products)
                .join(RetailerProduct.offers)
                .where(CanonicalProduct.category == "eggs")
                .order_by(CanonicalProduct.id.desc())
                .limit(1)
            )
        ).one()
        offers = list(
            await db.scalars(
                select(Offer)
                .join(Offer.retailer_product)
                .where(RetailerProduct.canonical_product_id == product_id)
            )
        )
        assert offers, "the chosen product must have offers to hide"
        for offer in offers:
            offer.availability = UNKNOWN
        await db.commit()

        first = await _search(client, page_size=1, page=1)
        second = await _search(client, page_size=1, page=2)

        # The original assertion here was `isinstance(..., list)`, which proved nothing: an
        # endpoint that always returned an empty list satisfied it.
        assert [p["id"] for p in first["unknown_products"]] == [product_id], (
            "page one carries the section, holding exactly the product nobody confirmed"
        )
        assert second["unknown_products"] == [], "later pages do not repeat it"
        assert product_id not in [p["id"] for p in first["products"]], (
            "and it is never mixed into the confirmed results"
        )


class TestCheapestBadge:
    async def test_the_cheapest_badge_is_global_not_page_local(
        self, client: AsyncClient, scraped
    ) -> None:
        """Otherwise page two would badge a dearer offer "Cheapest", truthfully and wrongly."""
        everything = await _search(client, page_size=MAX_PAGE_SIZE)
        expected = everything["cheapest_offer_id"]
        assert expected is not None

        total_pages = (await _search(client, page_size=1))["page"]["total_pages"]
        badged: list[int] = []
        for page in range(1, total_pages + 1):
            body = await _search(client, page_size=1, page=page)
            assert body["cheapest_offer_id"] == expected
            badged.extend(
                o["id"] for p in body["products"] for o in p["offers"] if o["is_cheapest_overall"]
            )

        assert badged == [expected], "exactly one offer carries the badge, on one page"


class TestQueryCost:
    async def test_a_page_costs_a_fixed_number_of_queries(
        self, client: AsyncClient, engine, scraped
    ) -> None:
        """The N+1 guard. Rendering more products must not mean more round trips.

        Search used to load every offer for the category at every nearby store; this pins
        that a page of one and a page of twenty cost the same number of statements.
        """
        counts: list[int] = []
        for page_size in (1, MAX_PAGE_SIZE):
            statements = 0

            def count(*_args, **_kwargs) -> None:
                nonlocal statements
                statements += 1

            event.listen(engine.sync_engine, "before_cursor_execute", count)
            try:
                await _search(client, availability="all", page_size=page_size)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count)
            counts.append(statements)

        assert counts[0] == counts[1], (
            f"a page of 1 cost {counts[0]} statements and a page of "
            f"{MAX_PAGE_SIZE} cost {counts[1]}; the count must not grow with the result"
        )


class TestUnknownSection:
    """ "Nobody publishes stock for this" is a fact about the product, not about the page.

    The section used to subtract only the products on the page being rendered, so a product
    ranked past the first page could appear in both lists at once -- captioned "cannot
    confirm these are on the shelf" here, and showing a confirmed in-stock price four pages
    later.
    """

    async def test_a_product_with_a_confirmed_offer_is_never_in_the_unknown_section(
        self, client: AsyncClient, db: AsyncSession, scraped
    ) -> None:
        # One offer of a product goes unknown while its siblings stay in stock: the product
        # is still confirmed buyable, so it belongs in the results, not in the footnote.
        offer = (
            await db.scalars(
                select(Offer)
                .join(Offer.retailer_product)
                .join(RetailerProduct.canonical_product)
                .where(CanonicalProduct.category == "eggs")
                .order_by(Offer.id)
                .limit(1)
            )
        ).one()
        rp = await db.get(RetailerProduct, offer.retailer_product_id)
        assert rp is not None
        product_id = rp.canonical_product_id
        offer.availability = UNKNOWN
        await db.commit()

        confirmed: set[int] = set()
        body = await _search(client, page_size=1, page=1)
        unknown_ids = {p["id"] for p in body["unknown_products"]}
        for page in range(1, (body["page"]["total_pages"] or 1) + 1):
            page_body = await _search(client, page_size=1, page=page)
            confirmed.update(p["id"] for p in page_body["products"])

        assert not (unknown_ids & confirmed), (
            f"products {unknown_ids & confirmed} are in both lists at once"
        )
        if product_id in confirmed:
            assert product_id not in unknown_ids
