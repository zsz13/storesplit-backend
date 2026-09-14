"""Deterministic product matching.

Given a normalized retailer product and the existing canonical products of the same
category, decide whether it is the same comparable product as one of them.

Decision ladder (first that applies):
  1. exact GTIN/UPC match                          -> "exact",  confidence 1.0, merge
  2. same package (quantity, unit, count) AND no conflicting title attribute
     (size, grade, fat level, salted, organic, ...) AND
     same brand AND title similarity >= AUTO       -> "auto",   merge
  3. same package AND similarity >= UNRESOLVED     -> "unresolved", no merge, candidates kept
  4. otherwise                                     -> "new",    no merge

Different package sizes are never merged: they are distinct comparable products that share a
category and are compared on unit price instead. **Nor are a weighed product and a packaged
one**, however alike their sizes look: a tray sold at $7.99 a pound and a sealed 16 oz pack at
$7.99 both measure one pound, but one is a rate and the other is a price, and merging them
would put a number a shopper hands over next to one they do not.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from app.normalize.naming import (
    attributes_conflict,
    extract_attributes,
    normalize_brand,
    normalize_title,
)

AUTO_THRESHOLD = 0.85
UNRESOLVED_THRESHOLD = 0.60
_SIZE_TOLERANCE = Decimal("0.01")  # 1 % relative tolerance on comparison quantity


@dataclass(frozen=True)
class ProductFeatures:
    """Everything the matcher is allowed to look at."""

    category: str
    title: str
    brand: str | None
    comparison_quantity: Decimal | None  # package size in the category's comparison unit
    count: int | None
    gtin: str | None
    canonical_id: int | None = None  # set for existing canonical products
    # Whether this product is weighed at the till. Since a per-unit price is normalized
    # against one pound, every weighed product in a category now shares that size, and size
    # alone no longer tells a tray apart from a 16 oz pack of the same brand. This does.
    sold_by: str = "unit"


@dataclass(frozen=True)
class MatchResult:
    status: str  # exact | auto | unresolved | new
    confidence: float
    canonical_id: int | None
    candidates: list[dict[str, float | int]] = field(default_factory=list)


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    tokens_a, tokens_b = set(a.split()), set(b.split())
    # Word order carries little meaning in retailer titles: compare sorted token strings.
    ratio = SequenceMatcher(None, " ".join(sorted(tokens_a)), " ".join(sorted(tokens_b))).ratio()
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if tokens_a | tokens_b else 0.0
    return round((ratio + jaccard) / 2, 4)


def same_package(a: ProductFeatures, b: ProductFeatures) -> bool:
    if a.sold_by != b.sold_by:
        # One is weighed and one is packaged. They are not the same thing to buy, and their
        # comparison quantities agreeing is a coincidence of the unit, not evidence.
        return False
    if a.count is not None and b.count is not None and a.count != b.count:
        return False
    if a.comparison_quantity is None or b.comparison_quantity is None:
        return a.comparison_quantity == b.comparison_quantity
    larger = max(a.comparison_quantity, b.comparison_quantity)
    return abs(a.comparison_quantity - b.comparison_quantity) <= larger * _SIZE_TOLERANCE


def match_product(candidate: ProductFeatures, existing: list[ProductFeatures]) -> MatchResult:
    same_category = [e for e in existing if e.category == candidate.category]

    if candidate.gtin:
        for other in same_category:
            if other.gtin and other.gtin == candidate.gtin:
                return MatchResult("exact", 1.0, other.canonical_id)

    brand = normalize_brand(candidate.brand)
    title = normalize_title(candidate.title, candidate.brand)
    attributes = extract_attributes(candidate.title)
    scored: list[tuple[float, ProductFeatures]] = []
    for other in same_category:
        if not same_package(candidate, other):
            continue
        if attributes_conflict(attributes, extract_attributes(other.title)):
            continue
        other_brand = normalize_brand(other.brand)
        similarity = title_similarity(title, normalize_title(other.title, other.brand))
        if brand and other_brand:
            if brand != other_brand:
                continue
            score = similarity
        else:
            # Missing brand on either side caps confidence below the automatic threshold.
            score = min(similarity, AUTO_THRESHOLD - 0.01)
        scored.append((score, other))

    scored.sort(key=lambda pair: (-pair[0], pair[1].canonical_id or 0))
    if not scored:
        return MatchResult("new", 0.0, None)
    best_score, best = scored[0]
    candidates = [
        {"canonical_id": int(o.canonical_id or 0), "score": s}
        for s, o in scored[:5]
        if s >= UNRESOLVED_THRESHOLD
    ]
    if best_score >= AUTO_THRESHOLD:
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if runner_up >= AUTO_THRESHOLD and best_score - runner_up < 0.05:
            return MatchResult("unresolved", best_score, None, candidates)
        return MatchResult("auto", best_score, best.canonical_id, candidates)
    if best_score >= UNRESOLVED_THRESHOLD:
        return MatchResult("unresolved", best_score, None, candidates)
    return MatchResult("new", best_score, None)
