"""Express pricing, including the interaction the shipped suite never exercises:
a parcel that is both heavy and express.
"""
from __future__ import annotations

import json

import pytest

from quoting.pricing import (
    EXPRESS_MULTIPLIER,
    HEAVY_SURCHARGE_PENCE,
    HEAVY_THRESHOLD_GRAMS,
    QuoteRequest,
    quote_pence,
)
from quoting.service import handle_quote


def _quote(**payload):
    return handle_quote(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("zone", ["uk", "eu", "row"])
def test_express_multiplies_the_whole_quote(zone):
    standard = quote_pence(QuoteRequest(weight_grams=1500, destination=zone))
    express = quote_pence(
        QuoteRequest(weight_grams=1500, destination=zone, express=True)
    )
    assert express == standard * EXPRESS_MULTIPLIER


def test_express_applies_after_the_heavy_surcharge():
    """Ordering matters: the surcharge is part of what express multiplies."""
    heavy = QuoteRequest(weight_grams=HEAVY_THRESHOLD_GRAMS, destination="uk")
    heavy_express = QuoteRequest(
        weight_grams=HEAVY_THRESHOLD_GRAMS, destination="uk", express=True
    )
    assert quote_pence(heavy_express) == quote_pence(heavy) * EXPRESS_MULTIPLIER
    assert quote_pence(heavy_express) - quote_pence(heavy) > HEAVY_SURCHARGE_PENCE


def test_the_response_echoes_the_express_flag():
    status, body = _quote(weight_grams=500, destination="uk", express=True)
    assert status == 200
    assert body["express"] is True


def test_omitting_express_defaults_to_standard():
    status, body = _quote(weight_grams=500, destination="uk")
    assert status == 200
    assert body["express"] is False
