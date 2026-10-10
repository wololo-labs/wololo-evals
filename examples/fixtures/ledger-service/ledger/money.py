"""Minor-unit money handling for the ledger (stdlib only).

Amounts arrive as decimal strings from an upstream API and are held as integer
minor units. Every defect that matters in this module lives on the far side of
that conversion, which is the point: a test written against the string the
caller passed in never sees it.
"""
from __future__ import annotations

__all__ = ["to_minor_units", "from_minor_units", "convert", "RATES", "DEFAULT_CURRENCY"]

#: Minor units per major unit, per currency.
_EXPONENT = {"GBP": 2, "EUR": 2, "USD": 2, "JPY": 0, "KWD": 3}

#: Fixed rates against the default currency, major units per major unit.
RATES = {"GBP": 1.0, "EUR": 1.17, "USD": 1.27, "JPY": 190.0, "KWD": 0.39}

DEFAULT_CURRENCY = "GBP"


def to_minor_units(amount: str, currency: str) -> int:
    """Parse a decimal string into integer minor units, rounding half up.

    Half up, not banker's rounding: an amount landing exactly on half a minor
    unit rounds away from zero, so 1.005 in a two-exponent currency is 101 and
    -1.005 is -101.
    """
    if currency not in _EXPONENT:
        raise ValueError(f"unknown currency {currency!r}")
    text = amount.strip()
    if not text:
        raise ValueError("amount is empty")
    negative = text.startswith("-")
    if negative or text.startswith("+"):
        text = text[1:]
    if "." in text:
        whole, _, frac = text.partition(".")
    else:
        whole, frac = text, ""
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise ValueError(f"amount {amount!r} is not a decimal number")

    exponent = _EXPONENT[currency]
    frac = frac.ljust(exponent + 1, "0")
    kept, carry_digit = frac[:exponent], frac[exponent]
    minor = int(whole) * (10 ** exponent) + (int(kept) if kept else 0)
    if int(carry_digit) >= 5:
        minor += 1
    return -minor if negative else minor


def from_minor_units(minor: int, currency: str) -> str:
    """Render integer minor units back as a decimal string."""
    if currency not in _EXPONENT:
        raise ValueError(f"unknown currency {currency!r}")
    exponent = _EXPONENT[currency]
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    if exponent == 0:
        return f"{sign}{minor}"
    whole, frac = divmod(minor, 10 ** exponent)
    return f"{sign}{whole}.{str(frac).zfill(exponent)}"


def convert(minor: int, currency: str, into: str) -> int:
    """Convert minor units between currencies through the default currency.

    An unknown currency raises. It must not fall back to a default rate: a
    silent fallback returns a plausible number for an amount nobody priced,
    which is worse than refusing the posting.
    """
    for code in (currency, into):
        if code not in RATES or code not in _EXPONENT:
            raise ValueError(f"unknown currency {code!r}")
    major = minor / (10 ** _EXPONENT[currency])
    in_default = major / RATES[currency]
    target_major = in_default * RATES[into]
    scaled = target_major * (10 ** _EXPONENT[into])
    # Half away from zero, and symmetric: -1.5 minor units rounds to -2, not
    # -1. int() truncates toward zero, so the sign is taken out first.
    sign = -1 if scaled < 0 else 1
    magnitude = abs(scaled)
    whole = int(magnitude)
    if magnitude - whole >= 0.5:
        whole += 1
    return sign * whole
