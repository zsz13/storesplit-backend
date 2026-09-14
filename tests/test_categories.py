import pytest
from app.normalize.categories import CATEGORIES, category_for_query, title_matches_category


@pytest.mark.parametrize(
    ("query", "key"),
    [
        ("eggs", "eggs"),
        ("Egg", "eggs"),
        ("milk", "milk"),
        ("chicken breast", "chicken_breast"),
        ("Chicken Breasts", "chicken_breast"),
        ("rice", "rice"),
        ("bread", "bread"),
        ("butter", "butter"),
        ("bananas", "bananas"),
        ("banana", "bananas"),
        ("caviar", None),
    ],
)
def test_category_for_query(query: str, key: str | None) -> None:
    category = category_for_query(query)
    assert (category.key if category else None) == key


@pytest.mark.parametrize(
    ("key", "title", "expected"),
    [
        ("eggs", "Organic Large Grade A Eggs, 12 CT", True),
        ("eggs", "Kimchi, 16 OZ", False),
        ("eggs", "Kale and Spinach Egg Bites with Egg White, 4.6 OZ", False),
        ("eggs", "Deviled Eggs", False),
        ("eggs", "Liquid Whole Eggs, 16 OZ", False),
        ("milk", "Whole Milk, 64 FZ", True),
        ("milk", "Unsweetened Cashew Milk, 32 FZ", False),
        ("milk", "CaCow Chocolate Milk, 8 FZ", False),
        ("chicken_breast", "Boneless Skinless Chicken Breast", True),
        ("chicken_breast", "Garlic & Herb Boneless Chicken Breast", False),
        ("chicken_breast", "Chicken Breast Tenderloin", False),
        ("rice", "White Jasmine Rice, 32 OZ", True),
        ("rice", "Caramel Rice Cakes, 6.56 OZ", False),
        ("bread", "Organic 21 Whole Grains Bread, 27 OZ", True),
        ("bread", "Banana Bread Pound Cake, 13.5 OZ", False),
        ("butter", "Salted Butter Sticks, 8 OZ", True),
        ("butter", "Smooth Peanut Butter, 16 OZ", False),
        ("butter", "Baby Butter, 1 EA", False),
        ("bananas", "Organic Banana", True),
        ("bananas", "Banana Nut Granola, 8 OZ", False),
        ("bananas", "Incense Holder Banana, 1 EA", False),
        ("bananas", "Banana", True),
        ("bananas", "Bananas", True),
        ("bananas", "Organic Baby Bananas", True),
        ("bananas", "Banana Ketchup, 12 OZ", False),
        ("bananas", "Bananas for Bacon Dog Treats, 6 OZ", False),
        ("bananas", "Wyman's Banana Berry, 48 OZ", False),
        ("eggs", "One Egg Pan, 1 EA", False),
        ("chicken_breast", "Thai Coconut Chicken Breast, 16 OZ", False),
        ("chicken_breast", "Organic Bone In Split Chicken Breast", True),
        ("butter", "Cinnamon Butter Streusel Loaf, 16 OZ", False),
        ("butter", "Fix and Fogg Everything Butter, 10 OZ", False),
        ("butter", "Unsalted Butter, 16 OZ", True),
        ("butter", "Salted Sweet Cream Butter, 16 OZ", True),
        ("butter", "Vanilla Ice Cream Butter Pecan, 16 OZ", False),
        ("milk", "Whole Milk Half Gallon", True),
        ("milk", "Cream On Top Whole Milk, 64 FZ", True),
        ("milk", "Half & Half, 32 FZ", False),
        ("milk", "Heavy Cream, 16 FZ", False),
    ],
)
def test_title_matches_category(key: str, title: str, expected: bool) -> None:
    assert title_matches_category(title, CATEGORIES[key]) is expected
