from app.normalize.gtin import normalize_gtin


def test_gtin_padding_unifies_retailer_formats() -> None:
    assert normalize_gtin("0001111060903") == "00001111060903"  # Kroger 13-digit
    assert normalize_gtin("00715141514643") == "00715141514643"  # GTIN-14
    assert normalize_gtin("715141514643") == "00715141514643"  # UPC-A
    assert normalize_gtin("0-71514-15146-43") == "00715141514643"


def test_gtin_rejects_plu_and_garbage() -> None:
    assert normalize_gtin("00000000004011") is None  # banana PLU
    assert normalize_gtin("062124") is None  # Trader Joe's internal SKU
    assert normalize_gtin("") is None and normalize_gtin(None) is None
