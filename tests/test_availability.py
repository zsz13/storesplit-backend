"""Retailer availability collapses to exactly three states, and never guesses `in_stock`."""

import pytest
from app.normalize.availability import (
    IN_STOCK,
    OUT_OF_STOCK,
    UNKNOWN,
    availability_from_flag,
    normalize_availability,
)


@pytest.mark.parametrize(
    "raw",
    [
        "inStock",
        "in_stock",
        "IN STOCK",
        "available",
        "AVAILABLE",
        "in-stock",
        "  InStock  ",
    ],
)
def test_stocked_tokens(raw: str) -> None:
    assert normalize_availability(raw) == IN_STOCK


@pytest.mark.parametrize(
    "raw",
    ["lowStock", "low_stock", "limited", "limitedStock", "low availability", "temporarily low"],
)
def test_a_hedge_is_not_a_promise(raw: str) -> None:
    """These read as `in_stock` until Lucky 19830961 showed what one of them really means.

    The Instacart storefront prints `lowStock` to the shopper as "Likely out of stock", so
    "few left" is not portably "buyable". Without a retailer saying which it means, the
    honest answer is `unknown` -- and a retailer that does know maps it in its own adapter.
    """
    assert normalize_availability(raw) == UNKNOWN


@pytest.mark.parametrize(
    "raw",
    [
        "outOfStock",
        "out_of_stock",
        "OUT OF STOCK",
        "out",
        "oos",
        "unavailable",
        "sold_out",
        "soldOut",
        "discontinued",
        "not_available",
        "TEMPORARILY_OUT_OF_STOCK",
        "out-of-stock",
    ],
)
def test_unstocked_tokens(raw: str) -> None:
    assert normalize_availability(raw) == OUT_OF_STOCK


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "who knows", "backorder", "preorder", "coming soon", 3, {}, [], object()],
)
def test_anything_unrecognised_is_unknown_not_in_stock(raw: object) -> None:
    """The rule that matters: missing availability is never `in_stock`."""
    assert normalize_availability(raw) == UNKNOWN


def test_the_three_states_are_the_only_states() -> None:
    assert {IN_STOCK, OUT_OF_STOCK, UNKNOWN} == {"in_stock", "out_of_stock", "unknown"}


class TestAvailabilityFromFlag:
    def test_a_true_flag_is_in_stock(self) -> None:
        assert availability_from_flag(True) == IN_STOCK

    def test_a_false_flag_is_out_of_stock(self) -> None:
        assert availability_from_flag(False) == OUT_OF_STOCK

    def test_a_missing_flag_is_unknown(self) -> None:
        assert availability_from_flag(None) == UNKNOWN

    def test_a_stock_level_refines_a_true_flag(self) -> None:
        assert availability_from_flag(True, "outOfStock") == OUT_OF_STOCK
        assert availability_from_flag(True, "lowStock") == IN_STOCK

    def test_a_false_flag_wins_over_an_optimistic_level(self) -> None:
        assert availability_from_flag(False, "inStock") == OUT_OF_STOCK

    def test_a_level_alone_decides_when_there_is_no_flag(self) -> None:
        assert availability_from_flag(None, "inStock") == IN_STOCK
        assert availability_from_flag(None, "outOfStock") == OUT_OF_STOCK
        assert availability_from_flag(None, "mystery") == UNKNOWN

    @pytest.mark.parametrize("flag", [1, "true", "yes", "Y"])
    def test_truthy_spellings_of_a_flag(self, flag: object) -> None:
        assert availability_from_flag(flag) == IN_STOCK

    @pytest.mark.parametrize("flag", [0, "false", "no", "N", "0"])
    def test_falsy_spellings_of_a_flag(self, flag: object) -> None:
        assert availability_from_flag(flag) == OUT_OF_STOCK

    def test_an_uninterpretable_flag_is_unknown(self) -> None:
        assert availability_from_flag("maybe") == UNKNOWN
        assert availability_from_flag(object()) == UNKNOWN
