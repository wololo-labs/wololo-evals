"""Weight boundary: the smallest and largest parcel the service will price.

The shipped suite prices a 500g parcel and rejects a missing weight, which
leaves both ends of the accepted range untested. These pin them.
"""
from __future__ import annotations

import json

import pytest

from quoting.pricing import MAX_WEIGHT_GRAMS, QuoteRequestError, parse_quote_request
from quoting.service import handle_quote


def _quote(**payload):
    return handle_quote(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("weight", [-30_000, -1000, -1, 0])
def test_non_positive_weight_is_rejected(weight):
    status, body = _quote(weight_grams=weight, destination="uk")
    assert status == 400
    assert body["field"] == "weight_grams"
    assert body["error"] == "weight_grams must be positive"


def test_one_gram_is_the_smallest_priced_parcel():
    status, body = _quote(weight_grams=1, destination="uk")
    assert status == 200
    assert body["quote_pence"] == 515
    assert body["weight_grams"] == 1


def test_the_ceiling_itself_is_accepted():
    status, body = _quote(weight_grams=MAX_WEIGHT_GRAMS, destination="uk")
    assert status == 200
    assert body["quote_pence"] == 395 + 120 * 30 + 850


def test_one_gram_over_the_ceiling_is_rejected():
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request(
            {"weight_grams": MAX_WEIGHT_GRAMS + 1, "destination": "uk"}
        )
    assert excinfo.value.field == "weight_grams"
