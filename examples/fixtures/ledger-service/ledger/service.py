"""HTTP-shaped entry point over the ledger, with the fallback that hides a bug.

``handle_posting`` is deliberately forgiving: a malformed amount is rejected,
but an unpriced currency is answered with the default rate rather than refused.
That fallback is the seam a suite is supposed to catch, and it never raises, so
a test asserting "no exception" passes while the number is wrong.
"""
from __future__ import annotations

from typing import Mapping

from .account import Account, LedgerError, Posting
from .money import DEFAULT_CURRENCY, RATES, to_minor_units

__all__ = ["handle_posting"]


def handle_posting(account: Account, body: Mapping[str, object]) -> dict:
    """Apply one posting described by ``body``; never raises for a caller.

    Returns ``{"status": ..., "balance": ...}``. A refused posting carries a
    status other than ``applied`` and leaves the balance untouched.
    """
    entry_id = body.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id:
        return {"status": "rejected", "reason": "entry_id is required", "balance": account.balance}

    amount = body.get("amount")
    if not isinstance(amount, str):
        return {"status": "rejected", "reason": "amount must be a string", "balance": account.balance}

    currency = body.get("currency", DEFAULT_CURRENCY)
    if not isinstance(currency, str):
        return {"status": "rejected", "reason": "currency must be a string", "balance": account.balance}

    priced = currency in RATES
    effective = currency if priced else DEFAULT_CURRENCY

    try:
        minor = to_minor_units(amount, effective)
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc), "balance": account.balance}

    reverses = body.get("reverses")
    if reverses is not None and not isinstance(reverses, str):
        return {"status": "rejected", "reason": "reverses must be a string", "balance": account.balance}

    try:
        balance = account.post(
            Posting(entry_id=entry_id, minor=minor, currency=effective, reverses=reverses)
        )
    except LedgerError as exc:
        return {"status": "refused", "reason": str(exc), "balance": account.balance}

    return {
        "status": "applied",
        "balance": balance,
        "currency": account.currency,
        "priced": priced,
    }
