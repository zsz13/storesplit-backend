"""A published telephone number as one canonical string, or nothing.

Reading a phone number is normalization, not retailer knowledge -- the same argument that
put `parse_clock_12h` in `hours.py` rather than in Sprouts and Smart & Final separately.
Three adapters had written this out before it lived here.
"""

import re

_NON_DIGITS = re.compile(r"\D+")


def e164_us(raw: object) -> str | None:
    """ "(510) 769-8899" / "415-863-1292" / "+19257548824" -> "+15107698899", else None.

    Ten digits is a US number and eleven beginning with 1 is the same number written with
    its country code. Anything else is refused rather than padded or truncated: a number
    that dials the wrong shop is worse than no number, and the column is nullable.
    """
    digits = _NON_DIGITS.sub("", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else None
