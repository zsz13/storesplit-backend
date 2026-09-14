"""GTIN normalization so barcodes from different retailers compare as equal.

Retailers send UPC-A (12 digits), EAN-13, Kroger-style 13-digit UPCs with a leading zero, or
GTIN-14. All are the same number space once left-padded to 14 digits. Produce PLU codes
(4-5 digit numbers such as 4011 for bananas) are not GTINs and are dropped.
"""

import re

_NON_DIGITS = re.compile(r"\D")
_VALID_LENGTHS = {8, 12, 13, 14}
_PLU_MAX = 99_999


def normalize_gtin(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) not in _VALID_LENGTHS or int(digits) <= _PLU_MAX:
        return None
    return digits.zfill(14)
