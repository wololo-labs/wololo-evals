"""A table-driven sweep across the whole pricing surface.

Every row states a complete request and the exact pence it must cost, derived
by hand from the published rate card rather than from the implementation, so
the table stays an independent oracle rather than a snapshot of whatever the
code currently returns.
"""
from __future__ import annotations

import json

import pytest

from quoting.pricing import QuoteRequest, quote_pence
from quoting.service import handle_quote

# (weight_grams, destination, express, expected pence)
RATE_CARD = [
    (1, "uk", False, 515),
    (999, "uk", False, 515),
    (1000, "uk", False, 515),
    (1001, "uk", False, 635),
    (5000, "uk", False, 995),
    (5000, "uk", True, 1990),
    (1000, "eu", False, 1009),
    (2500, "eu", False, 1529),
    (2500, "eu", True, 3058),
    (1000, "row", False, 1605),
    (7500, "row", False, 4475),
    (20_000, "uk", False, 3645),
    (20_000, "uk", True, 7290),
    (30_000, "row", False, 14_345),
    (19_999, "uk", False, 2795),
]


@pytest.mark.parametrize("weight,destination,express,expected", RATE_CARD)
def test_the_rate_card_holds(weight, destination, express, expected):
    request = QuoteRequest(
        weight_grams=weight, destination=destination, express=express
    )
    assert quote_pence(request) == expected


@pytest.mark.parametrize("weight,destination,express,expected", RATE_CARD)
def test_the_http_layer_returns_the_same_prices(weight, destination, express, expected):
    status, body = handle_quote(
        json.dumps(
            {"weight_grams": weight, "destination": destination, "express": express}
        ).encode("utf-8")
    )
    assert status == 200
    assert body["quote_pence"] == expected


def test_the_rate_card_covers_every_zone():
    assert {row[1] for row in RATE_CARD} == {"uk", "eu", "row"}
