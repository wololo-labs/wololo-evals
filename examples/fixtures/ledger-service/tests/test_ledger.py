"""The suite the repository ships.

It is green, it is thin, and it is the control's starting point: every
assertion here checks that something came back rather than what came back.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ledger.account import Account, Posting
from ledger.money import from_minor_units, to_minor_units
from ledger.service import handle_posting


def test_parsing_an_amount_returns_a_number():
    assert to_minor_units("12.34", "GBP") is not None


def test_rendering_an_amount_returns_a_string():
    assert isinstance(from_minor_units(1234, "GBP"), str)


def test_a_posting_moves_the_balance():
    account = Account()
    account.post(Posting(entry_id="a", minor=1000))
    assert account.balance != 0


def test_the_service_answers_a_posting():
    account = Account()
    result = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
    assert "status" in result


def test_the_service_rejects_a_missing_entry_id():
    account = Account()
    result = handle_posting(account, {"amount": "10.00"})
    assert result["status"] == "rejected"
