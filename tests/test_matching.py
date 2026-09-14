from dataclasses import replace
from decimal import Decimal

from app.matching.deterministic import (
    ProductFeatures,
    match_product,
    same_package,
    title_similarity,
)
from app.normalize.naming import attributes_conflict, extract_attributes


def features(
    title: str,
    brand: str | None,
    size: str,
    count: int | None = None,
    gtin: str | None = None,
    cid: int | None = None,
) -> ProductFeatures:
    return ProductFeatures("eggs", title, brand, Decimal(size), count, gtin, cid)


def test_exact_gtin_match_is_automatic() -> None:
    existing = [
        features("Some other title entirely", "Other", "12", 12, gtin="0001111060903", cid=7)
    ]
    result = match_product(
        features("Grade A Large Eggs", "Farm Co", "12", 12, gtin="0001111060903"), existing
    )
    assert result.status == "exact" and result.canonical_id == 7 and result.confidence == 1.0


def test_same_brand_same_package_similar_title_merges() -> None:
    existing = [features("large grade a eggs", "Farm Co", "12", 12, cid=1)]
    result = match_product(features("Grade A Large Eggs, 12 CT", "FARM CO", "12", 12), existing)
    assert result.status == "auto" and result.canonical_id == 1


def test_different_package_size_never_merges() -> None:
    existing = [features("large grade a eggs", "Farm Co", "12", 12, cid=1)]
    result = match_product(features("Large Grade A Eggs, 18 CT", "Farm Co", "18", 18), existing)
    assert result.status == "new" and result.canonical_id is None


def test_different_brand_never_merges() -> None:
    existing = [features("large grade a eggs", "Farm Co", "12", 12, cid=1)]
    result = match_product(features("Large Grade A Eggs, 12 CT", "Vital Farms", "12", 12), existing)
    assert result.status == "new"


def test_missing_brand_is_never_automatic() -> None:
    existing = [features("large grade a eggs", "Farm Co", "12", 12, cid=1)]
    result = match_product(features("Large Grade A Eggs, 12 CT", None, "12", 12), existing)
    assert result.status == "unresolved" and result.canonical_id is None
    assert result.candidates and result.candidates[0]["canonical_id"] == 1


def test_low_similarity_is_new() -> None:
    existing = [features("pasture raised brown eggs", "Farm Co", "12", 12, cid=1)]
    result = match_product(features("Omega-3 White Eggs, 12 CT", "Farm Co", "12", 12), existing)
    assert result.status in {"new", "unresolved"} and result.canonical_id is None


def test_ambiguous_near_tie_is_unresolved() -> None:
    existing = [
        features("large grade a eggs", "Farm Co", "12", 12, cid=1),
        features("large grade a eggs", "Farm Co", "12", 12, cid=2),
    ]
    result = match_product(features("Large Grade A Eggs", "Farm Co", "12", 12), existing)
    assert result.status == "unresolved" and result.canonical_id is None


def test_conflicting_attributes_never_merge() -> None:
    existing = [features("organic large grade aa eggs", "Judys", "12", 12, cid=1)]
    result = match_product(
        features("Organic Extra Large Grade AA Eggs, 12 CT", "Judys", "12", 12), existing
    )
    assert result.status == "new"
    milk = [ProductFeatures("milk", "whole milk", "Clover", Decimal(1), None, None, 2)]
    result = match_product(
        ProductFeatures("milk", "Reduced Fat Milk, 128 FZ", "Clover", Decimal(1), None, None), milk
    )
    assert result.status == "new"


def test_extract_attributes() -> None:
    assert extract_attributes("Organic Extra Large Grade AA Brown Eggs") == {
        "size_grade": "extra_large",
        "grade": "aa",
        "organic": "organic",
        "color": "brown",
    }
    assert extract_attributes("Lactose Free 2% Reduced Fat Milk")["fat"] == "reduced_fat"
    assert attributes_conflict({"fat": "whole"}, {"fat": "low_fat"})
    assert not attributes_conflict({"fat": "whole"}, {"organic": "organic"})


def test_similarity_is_symmetric_and_bounded() -> None:
    a, b = "large grade a eggs", "grade a large eggs"
    assert 0 < title_similarity(a, b) <= 1
    assert title_similarity(a, b) == title_similarity(b, a)


def test_a_weighed_product_never_merges_with_a_packaged_one() -> None:
    """Since a per-pound price is normalized against one pound, every weighed product in a
    category now measures one pound -- the same as a sealed 16 oz pack of the same brand at
    the same price. Size alone stopped telling them apart, so the basis has to."""
    packaged = ProductFeatures(
        category="chicken_breast",
        title="boneless skinless chicken breast",
        brand="Pine Manor",
        comparison_quantity=Decimal(1),
        count=None,
        gtin=None,
        canonical_id=1,
        sold_by="unit",
    )
    weighed = ProductFeatures(
        category="chicken_breast",
        title="boneless skinless chicken breast",
        brand="Pine Manor",
        comparison_quantity=Decimal(1),
        count=None,
        gtin=None,
        sold_by="weight",
    )

    assert not same_package(weighed, packaged)
    assert match_product(weighed, [packaged]).status == "new"
    assert match_product(weighed, [replace(packaged, sold_by="weight")]).status == "auto"


def test_a_shared_gtin_still_outranks_the_basis() -> None:
    """A barcode is the retailer telling you it is the same item. Two retailers disagreeing
    about how they ring it up does not make it two products."""
    packaged = ProductFeatures(
        category="chicken_breast",
        title="boneless skinless chicken breast",
        brand="Pine Manor",
        comparison_quantity=Decimal(1),
        count=None,
        gtin="00012345678905",
        canonical_id=1,
        sold_by="unit",
    )
    weighed = replace(packaged, canonical_id=None, sold_by="weight")

    assert match_product(weighed, [packaged]).status == "exact"
