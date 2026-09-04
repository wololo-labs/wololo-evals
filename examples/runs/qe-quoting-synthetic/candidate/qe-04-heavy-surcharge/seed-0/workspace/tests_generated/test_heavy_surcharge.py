"""The heavy-parcel surcharge, pinned at its threshold rather than either side
of it: an off-by-one in the comparison is invisible to a test that only weighs
5kg and 25kg.
"""
from __future__ import annotations

import pytest

from quoting.pricing import (
    HEAVY_SURCHARGE_PENCE,
    HEAVY_THRESHOLD_GRAMS,
    QuoteRequest,
    quote_pence,
)


def _uk(weight: int) -> int:
    return quote_pence(QuoteRequest(weight_grams=weight, destination="uk"))


def test_the_threshold_itself_pays_the_surcharge():
    just_under = _uk(HEAVY_THRESHOLD_GRAMS - 1000)
    at_threshold = _uk(HEAVY_THRESHOLD_GRAMS)
    # One extra kilogram of zone rate, plus the surcharge appearing.
    assert at_threshold - just_under == 120 + HEAVY_SURCHARGE_PENCE


def test_just_under_the_threshold_pays_no_surcharge():
    assert _uk(HEAVY_THRESHOLD_GRAMS - 1) == 395 + 120 * 20


@pytest.mark.parametrize("weight", [1, 1000, 10_000, HEAVY_THRESHOLD_GRAMS - 1])
def test_light_parcels_never_pay_the_surcharge(weight):
    whole_kg = (weight + 999) // 1000
    assert _uk(weight) == 395 + 120 * whole_kg


def test_the_surcharge_is_flat_not_proportional():
    a = _uk(HEAVY_THRESHOLD_GRAMS)
    b = _uk(HEAVY_THRESHOLD_GRAMS + 5000)
    assert b - a == 120 * 5
