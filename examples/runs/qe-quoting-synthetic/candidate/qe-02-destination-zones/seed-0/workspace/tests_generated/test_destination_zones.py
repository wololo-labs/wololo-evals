"""Every destination zone, priced from the table rather than from a constant
copied into the test, so a rate change fails one assertion and not six.
"""
from __future__ import annotations

import json

import pytest

from quoting.pricing import DESTINATIONS, QuoteRequest, quote_pence
from quoting.service import handle_quote


def _quote(**payload):
    return handle_quote(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("zone", sorted(DESTINATIONS))
def test_every_zone_prices_a_one_kilogram_parcel(zone):
    base, per_kg = DESTINATIONS[zone]
    status, body = _quote(weight_grams=1000, destination=zone)
    assert status == 200
    assert body["quote_pence"] == base + per_kg
    assert body["destination"] == zone


def test_zones_are_ordered_uk_then_eu_then_row():
    uk = quote_pence(QuoteRequest(weight_grams=1000, destination="uk"))
    eu = quote_pence(QuoteRequest(weight_grams=1000, destination="eu"))
    row = quote_pence(QuoteRequest(weight_grams=1000, destination="row"))
    assert uk < eu < row


@pytest.mark.parametrize("bad", ["mars", "UK", "", "uk "])
def test_an_unknown_zone_is_rejected(bad):
    status, body = _quote(weight_grams=1000, destination=bad)
    assert status == 400
    assert body["field"] == "destination"


def test_a_missing_destination_is_rejected():
    status, body = _quote(weight_grams=1000)
    assert status == 400
    assert body["field"] == "destination"
