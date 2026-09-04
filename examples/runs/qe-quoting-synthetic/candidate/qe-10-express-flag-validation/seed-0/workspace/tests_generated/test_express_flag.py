"""The express flag must be a real boolean. Accepting a truthy string would
silently double a customer's quote.
"""
from __future__ import annotations

import json

import pytest

from quoting.pricing import QuoteRequestError, parse_quote_request
from quoting.service import handle_quote


def _quote(**payload):
    return handle_quote(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("express", ["yes", "true", 1, 0, None, [], {}])
def test_a_non_boolean_express_flag_is_rejected(express):
    with pytest.raises(QuoteRequestError) as excinfo:
        parse_quote_request(
            {"weight_grams": 500, "destination": "uk", "express": express}
        )
    assert excinfo.value.field == "express"
    assert excinfo.value.message == "express must be a boolean"


@pytest.mark.parametrize("express", [True, False])
def test_both_boolean_values_are_accepted(express):
    request = parse_quote_request(
        {"weight_grams": 500, "destination": "uk", "express": express}
    )
    assert request.express is express


def test_the_http_layer_reports_the_offending_field():
    status, body = _quote(weight_grams=500, destination="uk", express="yes")
    assert status == 400
    assert body["field"] == "express"
