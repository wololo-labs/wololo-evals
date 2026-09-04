"""The quoting service's shipped test suite.

This is what the repository had before anyone pointed a QE skill at it: happy
paths for each destination zone, the express multiplier, and the two obvious
rejections. It is the no-agent baseline arm of the QE benchmark, and it is
deliberately the kind of suite that reads fine in review while leaving the
input-validation boundary untested.
"""
from __future__ import annotations

import json

from quoting.pricing import QuoteRequest, quote_pence
from quoting.service import handle_quote


def _quote(**payload):
    return handle_quote(json.dumps(payload).encode("utf-8"))


def test_uk_parcel_is_priced():
    status, body = _quote(weight_grams=500, destination="uk")
    assert status == 200
    assert body["quote_pence"] == 515


def test_eu_parcel_is_priced():
    status, body = _quote(weight_grams=1500, destination="eu")
    assert status == 200
    assert body["quote_pence"] == 1269


def test_rest_of_world_parcel_is_priced():
    status, body = _quote(weight_grams=2500, destination="row")
    assert status == 200
    assert body["quote_pence"] == 2425


def test_express_doubles_the_quote():
    standard = quote_pence(QuoteRequest(weight_grams=500, destination="uk"))
    express = quote_pence(QuoteRequest(weight_grams=500, destination="uk", express=True))
    assert express == standard * 2


def test_unknown_destination_is_rejected():
    status, body = _quote(weight_grams=500, destination="mars")
    assert status == 400
    assert body["field"] == "destination"


def test_missing_weight_is_rejected():
    status, body = _quote(destination="uk")
    assert status == 400
    assert body["field"] == "weight_grams"


def test_health_endpoint_shape():
    from quoting.service import QuoteHandler

    assert QuoteHandler.server_version.startswith("quoting-fixture")
