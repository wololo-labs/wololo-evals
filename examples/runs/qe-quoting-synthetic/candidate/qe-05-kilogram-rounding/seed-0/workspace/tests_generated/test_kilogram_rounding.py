"""Part kilograms round up. The shipped suite only ever weighs parcels that
land on or just past a kilogram boundary, so truncation would go unnoticed.
"""
from __future__ import annotations

import pytest

from quoting.pricing import DESTINATIONS, QuoteRequest, quote_pence


@pytest.mark.parametrize(
    "weight,expected_kg",
    [(1, 1), (999, 1), (1000, 1), (1001, 2), (1999, 2), (2000, 2), (2001, 3)],
)
def test_part_kilograms_round_up(weight, expected_kg):
    base, per_kg = DESTINATIONS["uk"]
    assert quote_pence(
        QuoteRequest(weight_grams=weight, destination="uk")
    ) == base + per_kg * expected_kg


def test_a_sub_kilogram_parcel_is_never_priced_at_the_bare_base_rate():
    base, per_kg = DESTINATIONS["uk"]
    assert quote_pence(QuoteRequest(weight_grams=50, destination="uk")) > base


def test_rounding_is_applied_per_zone_rate():
    for zone, (base, per_kg) in DESTINATIONS.items():
        assert quote_pence(
            QuoteRequest(weight_grams=1500, destination=zone)
        ) == base + per_kg * 2
