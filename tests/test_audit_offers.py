"""The post-scrape audit, tested on its own.

`scripts/audit_offers.py` is the thing that is supposed to notice when a scrape writes
something wrong. Left untested, it can quietly stop noticing -- a comparator that never
fires still prints PASS, which is the most expensive kind of green there is. These tests
feed it rows it should reject and rows it should accept, without a database or a browser.
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from app.db.models import Offer, Retailer, RetailerProduct
from app.normalize.availability import IN_STOCK, OUT_OF_STOCK

SCRIPT = Path(__file__).parent.parent / "scripts" / "audit_offers.py"
_spec = importlib.util.spec_from_file_location("audit_offers", SCRIPT)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def row(
    *,
    slug: str = "lucky",
    sku: str = "1",
    url: str | None = "https://shop.luckysupermarkets.com/store/lucky-supermarkets/products/1",
    availability: str = IN_STOCK,
    price: str = "3.99",
    unit_price: str | None = "0.33",
    canonical_id: int | None = 1,
    offer_id: int = 1,
) -> tuple[Offer, RetailerProduct, Retailer]:
    retailer = Retailer(id=1, slug=slug, name=slug.title())
    product = RetailerProduct(
        id=offer_id,
        retailer_id=1,
        canonical_product_id=canonical_id,
        retailer_sku=sku,
        title="thing",
        product_url=url,
    )
    product.retailer = retailer
    offer = Offer(
        id=offer_id,
        retailer_product_id=product.id,
        store_id=1,
        price=Decimal(price),
        regular_price=Decimal(price),
        availability=availability,
        unit_price=None if unit_price is None else Decimal(unit_price),
    )
    offer.retailer_product = product
    return offer, product, retailer


class TestUrlAndVocabulary:
    def test_a_clean_scrape_produces_no_failures(self) -> None:
        findings = audit.Findings()
        audit.audit_rows([row()], findings)
        assert findings.failures == []

    def test_a_link_back_to_storesplit_itself_is_caught(self) -> None:
        """The bug that put `http://localhost:3000/store/...` in front of a shopper."""
        findings = audit.Findings()
        audit.audit_rows([row(url="http://localhost:3000/store/lucky/products/1")], findings)
        assert findings.failures
        assert any(
            "localhost" in failure or "not a valid page" in failure for failure in findings.failures
        )

    def test_a_link_on_someone_elses_host_is_caught(self) -> None:
        findings = audit.Findings()
        audit.audit_rows([row(url="https://evil.test/store/lucky/products/1")], findings)
        assert findings.failures

    def test_a_state_outside_the_vocabulary_is_caught(self) -> None:
        findings = audit.Findings()
        audit.audit_rows([row(availability="probably?")], findings)
        assert any("not a known state" in failure for failure in findings.failures)

    def test_an_offer_with_no_url_is_fine(self) -> None:
        """`None` is a valid answer; a wrong link is not."""
        findings = audit.Findings()
        audit.audit_rows([row(url=None)], findings)
        assert findings.failures == []


class TestRanking:
    def test_the_shipped_ranking_puts_the_buyable_offer_first(self) -> None:
        """A cheaper out-of-stock offer must not lead a dearer one that is on the shelf."""
        findings = audit.Findings()
        cheap_but_gone = row(offer_id=1, availability=OUT_OF_STOCK, unit_price="0.10")
        dearer_and_real = row(offer_id=2, availability=IN_STOCK, unit_price="0.50")
        audit.audit_ranking([cheap_but_gone, dearer_and_real], findings)
        assert findings.failures == []

    def test_the_audit_fires_if_the_ranking_ever_regresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the detector detects. Swap in a naive cheapest-first order and it must fail.

        Without this, `audit_ranking` is only ever shown inputs it accepts, and a check that
        has never been seen to fail is not yet a check.
        """
        monkeypatch.setattr(
            audit, "offer_sort_key", lambda offer: (offer.unit_price or offer.price, offer.id)
        )
        findings = audit.Findings()
        cheap_but_gone = row(offer_id=1, availability=OUT_OF_STOCK, unit_price="0.10")
        dearer_and_real = row(offer_id=2, availability=IN_STOCK, unit_price="0.50")
        audit.audit_ranking([cheap_but_gone, dearer_and_real], findings)
        assert findings.failures
        assert "out of stock" in findings.failures[0]
        assert "leads 1 buyable offer(s)" in findings.failures[0]

    def test_a_product_that_is_out_of_stock_everywhere_is_not_a_failure(self) -> None:
        findings = audit.Findings()
        rows = [
            row(offer_id=1, availability=OUT_OF_STOCK, unit_price="0.10"),
            row(offer_id=2, availability=OUT_OF_STOCK, unit_price="0.50"),
        ]
        audit.audit_ranking(rows, findings)
        assert findings.failures == []

    def test_the_check_uses_the_shipped_comparator(self) -> None:
        """Imported, not restated, so it cannot validate a ranking that no longer ships."""
        from app.services.search import offer_sort_key

        assert audit.offer_sort_key is offer_sort_key

    def test_offers_with_no_canonical_product_are_skipped_not_crashed_on(self) -> None:
        findings = audit.Findings()
        audit.audit_ranking([row(canonical_id=None)], findings)
        assert findings.failures == []


class TestBrokenPageDetection:
    @pytest.mark.parametrize(
        "page_text",
        [
            "Oops! We can't seem to find the page you're looking for",  # Trader Joe's
            "404 Not Found | Trader Joe's",
            "Page not found",
            "We couldn't find that product",
            "This item is no longer available",
        ],
    )
    def test_a_retailer_error_page_is_recognised(self, page_text: str) -> None:
        assert any(marker in page_text.lower() for marker in audit.NOT_A_PRODUCT_PAGE)

    @pytest.mark.parametrize(
        "page_text",
        [
            "Butter Chicken with Basmati Rice | Trader Joe's",
            "Great Value Large White Eggs, 12 Count - Walmart.com",
            "Organic Pasture Raised Large Brown Eggs, 12 Count",
            "Boneless Skinless Chicken Breast",
        ],
    )
    def test_a_real_product_page_is_not_mistaken_for_an_error(self, page_text: str) -> None:
        assert not any(marker in page_text.lower() for marker in audit.NOT_A_PRODUCT_PAGE)


class TestReporting:
    def test_findings_report_non_zero_when_something_failed(self) -> None:
        findings = audit.Findings()
        findings.fail("something")
        assert findings.report() == 1

    def test_findings_report_zero_when_clean(self) -> None:
        assert audit.Findings().report() == 0

    def test_unchecked_links_are_a_failure_not_a_pass(self) -> None:
        """ "We could not look" must never render as "we looked and it was fine"."""
        findings = audit.Findings()
        findings.fail("walmart: links could not be checked -- needs a human")
        assert findings.report() == 1
