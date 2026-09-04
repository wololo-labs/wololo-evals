"""The 30kg carrier ceiling, pinned at the boundary and expressed against the
constant rather than a literal, so raising the ceiling does not silently
invalidate the test.
"""
from __future__ import annotations

import pytest

from quoting.pricing import (
    MAX_WEIGHT_GRAMS,
    QuoteRequest,
    QuoteRequestError,
    parse_quote_request,
    quote_pence,
)


def test_the_ceiling_is_inclusive():
    request = parse_quote_request(
        {"weight_grams": MAX_WEIGHT_GRAMS, "destination": "uk"}
    )
    assert request.weight_grams == MAX_WEIGHT_GRAMS
    assert quote_pence(request) == 395 + 120 * 30 + 850


@pytest.mark.parametrize("over", [1, 1000, 100_000])
def test_anything_over_the_ceiling_is_rejected(over):
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request(
            {"weight_grams": MAX_WEIGHT_GRAMS + over, "destination": "uk"}
        )
    assert excinfo.value.field == "weight_grams"
    assert str(MAX_WEIGHT_GRAMS) in excinfo.value.message


def test_the_ceiling_message_names_the_limit():
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request({"weight_grams": 999_999, "destination": "uk"})
    assert excinfo.value.message == (
        f"weight_grams must not exceed {MAX_WEIGHT_GRAMS}"
    )


def test_the_ceiling_is_checked_after_the_type(  ):
    """A float over the ceiling is a type error, not a range error: the order
    of the guards is part of the contract callers read."""
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request({"weight_grams": 99_999.5, "destination": "uk"})
    assert excinfo.value.message == "weight_grams must be an integer"
