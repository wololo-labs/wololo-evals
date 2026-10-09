"""The suite the repository ships.

It is green, it is thin, and it is the control's starting point: every
assertion here checks that something came back rather than what came back.
"""
from __future__ import annotations

import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ledger.account import Account, Posting, LedgerError
from ledger.money import from_minor_units, to_minor_units, convert, RATES, DEFAULT_CURRENCY
from ledger.service import handle_posting


# Thin control tests (original)

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


# Comprehensive money module tests

class TestToMinorUnits:
    def test_basic_conversion_two_exponent(self):
        assert to_minor_units("12.34", "GBP") == 1234
        assert to_minor_units("1.00", "USD") == 100
        assert to_minor_units("0.01", "EUR") == 1

    def test_negative_amounts(self):
        assert to_minor_units("-12.34", "GBP") == -1234
        assert to_minor_units("-0.01", "USD") == -1

    def test_no_decimal_point(self):
        assert to_minor_units("100", "GBP") == 10000
        assert to_minor_units("100", "JPY") == 100

    def test_zero_exponent_currency(self):
        assert to_minor_units("1000", "JPY") == 1000
        assert to_minor_units("1000.5", "JPY") == 1001

    def test_three_exponent_currency(self):
        assert to_minor_units("1.234", "KWD") == 1234
        assert to_minor_units("0.001", "KWD") == 1

    def test_half_up_rounding_positive(self):
        assert to_minor_units("1.005", "GBP") == 101
        assert to_minor_units("1.004", "GBP") == 100
        assert to_minor_units("0.125", "GBP") == 13

    def test_half_up_rounding_negative(self):
        assert to_minor_units("-1.005", "GBP") == -101
        assert to_minor_units("-1.004", "GBP") == -100

    def test_leading_zeros(self):
        assert to_minor_units("001.23", "GBP") == 123
        assert to_minor_units("0.01", "GBP") == 1

    def test_trailing_zeros(self):
        assert to_minor_units("12.3", "GBP") == 1230
        assert to_minor_units("12.30", "GBP") == 1230

    def test_explicit_plus_sign(self):
        assert to_minor_units("+12.34", "GBP") == 1234

    def test_whitespace_stripped(self):
        assert to_minor_units("  12.34  ", "GBP") == 1234

    def test_empty_amount_raises(self):
        with pytest.raises(ValueError, match="amount is empty"):
            to_minor_units("", "GBP")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="amount is empty"):
            to_minor_units("   ", "GBP")

    def test_invalid_decimal_format_raises(self):
        with pytest.raises(ValueError, match="not a decimal number"):
            to_minor_units("12.34.56", "GBP")

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="not a decimal number"):
            to_minor_units("abc", "GBP")

    def test_non_digit_in_fractional_part_raises(self):
        with pytest.raises(ValueError, match="not a decimal number"):
            to_minor_units("12.3a", "GBP")

    def test_unknown_currency_raises(self):
        with pytest.raises(ValueError, match="unknown currency"):
            to_minor_units("12.34", "XXX")

    def test_all_supported_currencies(self):
        assert to_minor_units("1.00", "GBP") == 100
        assert to_minor_units("1.00", "EUR") == 100
        assert to_minor_units("1.00", "USD") == 100
        assert to_minor_units("1.00", "JPY") == 1
        assert to_minor_units("1.000", "KWD") == 1000


class TestFromMinorUnits:
    def test_basic_conversion_two_exponent(self):
        assert from_minor_units(1234, "GBP") == "12.34"
        assert from_minor_units(100, "USD") == "1.00"
        assert from_minor_units(1, "EUR") == "0.01"

    def test_negative_amounts(self):
        assert from_minor_units(-1234, "GBP") == "-12.34"
        assert from_minor_units(-1, "USD") == "-0.01"

    def test_zero_exponent_currency(self):
        assert from_minor_units(1000, "JPY") == "1000"

    def test_three_exponent_currency(self):
        assert from_minor_units(1234, "KWD") == "1.234"
        assert from_minor_units(1, "KWD") == "0.001"

    def test_zero_amount(self):
        assert from_minor_units(0, "GBP") == "0.00"
        assert from_minor_units(0, "JPY") == "0"

    def test_unknown_currency_raises(self):
        with pytest.raises(ValueError, match="unknown currency"):
            from_minor_units(1234, "XXX")

    def test_roundtrip_conversion(self):
        for currency in ["GBP", "EUR", "USD", "JPY", "KWD"]:
            original = "123.45" if currency != "JPY" else "123"
            minor = to_minor_units(original, currency)
            rendered = from_minor_units(minor, currency)
            re_parsed = to_minor_units(rendered, currency)
            assert re_parsed == minor


class TestConvert:
    def test_same_currency_no_conversion(self):
        assert convert(1000, "GBP", "GBP") == 1000
        assert convert(100, "USD", "USD") == 100

    def test_gbp_to_usd(self):
        gbp_100 = 10000
        usd = convert(gbp_100, "GBP", "USD")
        assert usd > gbp_100

    def test_gbp_to_eur(self):
        gbp_100 = 10000
        eur = convert(gbp_100, "GBP", "EUR")
        assert eur > gbp_100

    def test_jpy_conversion(self):
        gbp_100 = 10000
        jpy = convert(gbp_100, "GBP", "JPY")
        assert jpy == 19000

    def test_kwd_conversion(self):
        gbp_100 = 10000
        kwd = convert(gbp_100, "GBP", "KWD")
        assert kwd == 39000

    def test_negative_amount_conversion(self):
        result = convert(-1000, "GBP", "USD")
        assert result < 0

    def test_unknown_source_currency_raises(self):
        with pytest.raises(ValueError, match="unknown currency"):
            convert(1000, "XXX", "USD")

    def test_unknown_target_currency_raises(self):
        with pytest.raises(ValueError, match="unknown currency"):
            convert(1000, "GBP", "XXX")

    def test_roundtrip_through_default_currency(self):
        original = 5000
        eur = convert(original, "GBP", "EUR")
        back = convert(eur, "EUR", "GBP")
        assert back == original

    def test_half_away_from_zero_rounding(self):
        usd_minor = 1
        result = convert(usd_minor, "USD", "GBP")
        assert isinstance(result, int)

    def test_conversion_with_zero(self):
        assert convert(0, "GBP", "USD") == 0


# Comprehensive account tests

class TestPosting:
    def test_posting_immutability(self):
        posting = Posting(entry_id="a", minor=100)
        with pytest.raises(AttributeError):
            posting.entry_id = "b"

    def test_posting_with_reverses(self):
        posting = Posting(entry_id="rev", minor=100, reverses="original")
        assert posting.reverses == "original"

    def test_posting_default_currency(self):
        posting = Posting(entry_id="a", minor=100)
        assert posting.currency == DEFAULT_CURRENCY

    def test_posting_custom_currency(self):
        posting = Posting(entry_id="a", minor=100, currency="USD")
        assert posting.currency == "USD"


class TestAccount:
    def test_account_default_state(self):
        account = Account()
        assert account.balance == 0
        assert account.currency == DEFAULT_CURRENCY
        assert len(account.postings) == 0

    def test_post_single_entry(self):
        account = Account()
        posting = Posting(entry_id="a", minor=1000)
        balance = account.post(posting)
        assert balance == 1000
        assert account.balance == 1000

    def test_post_multiple_entries(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        balance = account.post(Posting(entry_id="b", minor=500))
        assert balance == 1500
        assert account.balance == 1500

    def test_negative_posting(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        balance = account.post(Posting(entry_id="b", minor=-300))
        assert balance == 700
        assert account.balance == 700

    def test_idempotent_repost_same_entry_id(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        balance = account.post(Posting(entry_id="a", minor=1000))
        assert balance == 1000
        assert account.balance == 1000

    def test_reversal_basic(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        balance = account.post(Posting(entry_id="rev", minor=0, reverses="a"))
        assert balance == 0
        assert account.balance == 0

    def test_reversal_with_different_amounts_ignores_reversal_amount(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        balance = account.post(Posting(entry_id="rev", minor=9999, reverses="a"))
        assert balance == 0

    def test_reversal_unknown_entry_raises(self):
        account = Account()
        with pytest.raises(LedgerError, match="cannot reverse unknown entry"):
            account.post(Posting(entry_id="rev", minor=0, reverses="nonexistent"))

    def test_double_reversal_raises(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        account.post(Posting(entry_id="rev", minor=0, reverses="a"))
        with pytest.raises(LedgerError, match="is already reversed"):
            account.post(Posting(entry_id="rev2", minor=0, reverses="a"))

    def test_reversal_records_reversed_id(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        account.post(Posting(entry_id="rev", minor=0, reverses="a"))
        assert "a" in account.reversed_ids

    def test_posting_recorded_in_postings_dict(self):
        account = Account()
        posting = Posting(entry_id="a", minor=1000)
        account.post(posting)
        assert "a" in account.postings

    def test_recompute_matches_incremental_simple(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        account.post(Posting(entry_id="b", minor=500))
        recomputed = account.recompute()
        assert recomputed == account.balance

    def test_recompute_matches_incremental_with_reversals(self):
        account = Account()
        account.post(Posting(entry_id="a", minor=1000))
        account.post(Posting(entry_id="b", minor=500))
        account.post(Posting(entry_id="rev_a", minor=0, reverses="a"))
        recomputed = account.recompute()
        assert recomputed == account.balance
        assert recomputed == 500

    def test_recompute_matches_incremental_complex_sequence(self):
        account = Account()
        entries = [
            Posting(entry_id="a", minor=1000),
            Posting(entry_id="b", minor=500),
            Posting(entry_id="c", minor=-200),
            Posting(entry_id="rev_b", minor=0, reverses="b"),
            Posting(entry_id="d", minor=300),
        ]
        for entry in entries:
            account.post(entry)
        recomputed = account.recompute()
        assert recomputed == account.balance
        assert recomputed == 1100

    def test_account_with_custom_currency(self):
        account = Account(currency="USD")
        posting = Posting(entry_id="a", minor=100, currency="USD")
        balance = account.post(posting)
        assert balance == 100
        assert account.currency == "USD"

    def test_cross_currency_posting_converted(self):
        account = Account(currency="GBP")
        posting = Posting(entry_id="a", minor=100, currency="USD")
        balance = account.post(posting)
        assert balance != 100

    def test_reversal_of_cross_currency_post(self):
        account = Account(currency="GBP")
        account.post(Posting(entry_id="a", minor=10000, currency="USD"))
        original_balance = account.balance
        account.post(Posting(entry_id="rev", minor=0, reverses="a"))
        assert account.balance == 0
        assert "a" in account.reversed_ids


# Comprehensive service tests

class TestHandlePosting:
    def test_valid_posting(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        assert result["status"] == "applied"
        assert result["balance"] == 1000

    def test_returns_all_required_fields(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        assert "status" in result
        assert "balance" in result
        assert "currency" in result

    def test_missing_entry_id(self):
        account = Account()
        result = handle_posting(account, {"amount": "10.00"})
        assert result["status"] == "rejected"
        assert "reason" in result

    def test_empty_entry_id(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "", "amount": "10.00"})
        assert result["status"] == "rejected"

    def test_entry_id_not_string(self):
        account = Account()
        result = handle_posting(account, {"entry_id": 123, "amount": "10.00"})
        assert result["status"] == "rejected"

    def test_missing_amount(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a"})
        assert result["status"] == "rejected"
        assert "amount" in result["reason"]

    def test_amount_not_string(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": 10})
        assert result["status"] == "rejected"

    def test_invalid_amount_format(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "invalid"})
        assert result["status"] == "rejected"

    def test_empty_amount_string(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": ""})
        assert result["status"] == "rejected"

    def test_missing_currency_uses_default(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        assert result["status"] == "applied"
        assert result["currency"] == DEFAULT_CURRENCY

    def test_explicit_currency(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00", "currency": "USD"})
        assert result["status"] == "applied"
        assert result["currency"] == "GBP"

    def test_currency_not_string(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00", "currency": 123})
        assert result["status"] == "rejected"

    def test_unpriced_currency_fallback_to_default(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00", "currency": "UNKNOWN"})
        assert result["status"] == "applied"
        assert result["priced"] is False

    def test_priced_currency_flag(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00", "currency": "USD"})
        assert result["priced"] is True

    def test_reversal_without_reverses_field(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        assert result["status"] == "applied"

    def test_reversal_with_string_reverses(self):
        account = Account()
        handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        result = handle_posting(account, {"entry_id": "rev", "amount": "10.00", "reverses": "a"})
        assert result["status"] == "applied"
        assert result["balance"] == 0

    def test_reversal_non_string_reverses(self):
        account = Account()
        handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        result = handle_posting(account, {"entry_id": "rev", "amount": "10.00", "reverses": 123})
        assert result["status"] == "rejected"

    def test_reversal_unknown_entry_refused(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "rev", "amount": "10.00", "reverses": "nonexistent"})
        assert result["status"] == "refused"

    def test_double_reversal_refused(self):
        account = Account()
        handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        handle_posting(account, {"entry_id": "rev", "amount": "10.00", "reverses": "a"})
        result = handle_posting(account, {"entry_id": "rev2", "amount": "10.00", "reverses": "a"})
        assert result["status"] == "refused"

    def test_refused_posting_balance_unchanged(self):
        account = Account()
        handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        initial_balance = account.balance
        handle_posting(account, {"entry_id": "rev", "amount": "10.00", "reverses": "nonexistent"})
        assert account.balance == initial_balance

    def test_rejected_posting_balance_unchanged(self):
        account = Account()
        handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        initial_balance = account.balance
        handle_posting(account, {"entry_id": "b", "amount": "invalid"})
        assert account.balance == initial_balance

    def test_idempotent_posting_returns_same_balance(self):
        account = Account()
        result1 = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        result2 = handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        assert result1["balance"] == result2["balance"]

    def test_sequence_dependent_balance(self):
        account = Account()
        handle_posting(account, {"entry_id": "a", "amount": "10.00"})
        handle_posting(account, {"entry_id": "b", "amount": "20.00"})
        result = handle_posting(account, {"entry_id": "c", "amount": "30.00"})
        assert result["balance"] == 6000

    def test_negative_amount(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "-15.50"})
        assert result["status"] == "applied"
        assert result["balance"] == -1550

    def test_zero_amount(self):
        account = Account()
        result = handle_posting(account, {"entry_id": "a", "amount": "0.00"})
        assert result["status"] == "applied"
        assert result["balance"] == 0
