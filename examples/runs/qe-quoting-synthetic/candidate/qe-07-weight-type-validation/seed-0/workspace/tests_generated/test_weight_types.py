"""weight_grams must be an integer. Booleans are the interesting case: in
Python `True` is an int, so a naive isinstance check prices a parcel weighing
`True` grams.
"""
from __future__ import annotations

import pytest

from quoting.pricing import QuoteRequestError, parse_quote_request


@pytest.mark.parametrize("weight", [True, False, 1.5, "500", None, [500], {"g": 500}])
def test_a_non_integer_weight_is_rejected(weight):
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request({"weight_grams": weight, "destination": "uk"})
    assert excinfo.value.field == "weight_grams"


def test_a_bool_is_not_accepted_as_an_integer_weight():
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request({"weight_grams": True, "destination": "uk"})
    assert excinfo.value.message == "weight_grams must be an integer"


def test_a_plain_integer_is_accepted():
    request = parse_quote_request({"weight_grams": 500, "destination": "uk"})
    assert request.weight_grams == 500
