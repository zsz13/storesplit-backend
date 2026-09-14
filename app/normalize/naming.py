"""Brand and title normalization used by product matching."""

import re

from app.normalize.units import strip_quantity_phrases

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")
_NOISE_WORDS = {"the", "and", "with", "of", "a", "an", "brand"}


# Attribute groups whose values make products materially different even when the rest of
# the title matches ("Large" vs "Extra Large" eggs, "Whole" vs "2%" milk, salted/unsalted).
# Patterns are matched against the lowercased title; the first matching value in a group wins.
ATTRIBUTE_GROUPS: dict[str, list[tuple[str, str]]] = {
    "size_grade": [
        (r"\b(?:extra[\s-]?large|x-?large|xl)\b", "extra_large"),
        (r"\bjumbo\b", "jumbo"),
        (r"\bmedium\b", "medium"),
        (r"\bsmall\b", "small"),
        (r"\blarge\b", "large"),
    ],
    "grade": [(r"\bgrade\s*aa\b", "aa"), (r"\bgrade\s*a\b", "a"), (r"\bgrade\s*b\b", "b")],
    "organic": [(r"\borganic\b", "organic")],
    "fat": [
        (r"\b(?:fat[\s-]?free|non[\s-]?fat|skim)\b", "fat_free"),
        (r"\b(?:1\s?%|low[\s-]?fat)\b", "low_fat"),
        (r"\b(?:2\s?%|reduced[\s-]?fat)\b", "reduced_fat"),
        (r"\bwhole\b", "whole"),
    ],
    "salt": [(r"\bunsalted\b", "unsalted"), (r"\bsalted\b", "salted")],
    "bone": [(r"\bboneless\b", "boneless"), (r"\bbone[\s-]?in\b", "bone_in")],
    "skin": [(r"\bskinless\b", "skinless"), (r"\bskin[\s-]?on\b", "skin_on")],
    "color": [(r"\bbrown\b", "brown"), (r"\bwhite\b", "white")],
    "lactose": [(r"\blactose[\s-]?free\b", "lactose_free")],
    "grain": [(r"\bwhole[\s-]?(?:grain|wheat)\b", "whole_grain")],
}


def extract_attributes(title: str) -> dict[str, str]:
    """Deterministic product attributes found in a title, keyed by attribute group."""
    text = title.lower()
    found: dict[str, str] = {}
    for group, patterns in ATTRIBUTE_GROUPS.items():
        for pattern, value in patterns:
            if re.search(pattern, text):
                found[group] = value
                break
    return found


def attributes_conflict(a: dict[str, str], b: dict[str, str]) -> bool:
    """True when both titles state a value for the same group and the values differ."""
    return any(group in b and b[group] != value for group, value in a.items())


def normalize_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    text = brand.lower().replace("&", " and ").replace("'", "")
    text = _SPACES.sub(" ", _NON_ALNUM.sub(" ", text)).strip()
    return text or None


def normalize_title(title: str, brand: str | None = None) -> str:
    """Lowercase title with quantity phrases, brand tokens and punctuation removed."""
    text = title.lower().replace("&", " and ").replace("'", "")
    text = strip_quantity_phrases(text)
    text = _NON_ALNUM.sub(" ", text)
    tokens = [t for t in text.split() if t not in _NOISE_WORDS]
    brand_tokens = set((normalize_brand(brand) or "").split())
    tokens = [t for t in tokens if t not in brand_tokens]
    return " ".join(tokens)
