"""Parcel quoting rules for the fixture service.

Pure functions over plain values so the rules are testable without starting the
HTTP server. Prices are in pence throughout: a float pound value would make
every equality assertion in a generated test suite subtly wrong.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MAX_WEIGHT_GRAMS",
    "DESTINATIONS",
    "QuoteRequestError",
    "QuoteRequest",
    "quote_pence",
    "parse_quote_request",
]

#: Carrier ceiling. Anything heavier is a freight booking, not a parcel.
MAX_WEIGHT_GRAMS = 30_000

#: Destination zone -> (base pence, pence per whole kilogram).
DESTINATIONS = {
    "uk": (395, 120),
    "eu": (749, 260),
    "row": (1195, 410),
}

#: Multiplier applied to the whole quote when express delivery is requested.
EXPRESS_MULTIPLIER = 2

#: Parcels over this weight pay a handling surcharge on top of the zone rate.
HEAVY_THRESHOLD_GRAMS = 20_000
HEAVY_SURCHARGE_PENCE = 850


class QuoteRequestError(ValueError):
    """The request cannot be priced. Carries the offending field so the HTTP
    layer can answer 400 with something a caller can act on."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class QuoteRequest:
    weight_grams: int
    destination: str
    express: bool = False


def parse_quote_request(payload: object) -> QuoteRequest:
    """Validate an incoming JSON body into a :class:`QuoteRequest`."""
    if not isinstance(payload, dict):
        raise QuoteRequestError("body", "request body must be a JSON object")

    raw_weight = payload.get("weight_grams")
    if raw_weight is None:
        raise QuoteRequestError("weight_grams", "weight_grams is required")
    if isinstance(raw_weight, bool) or not isinstance(raw_weight, int):
        raise QuoteRequestError("weight_grams", "weight_grams must be an integer")
    if raw_weight <= 0:
        raise QuoteRequestError("weight_grams", "weight_grams must be positive")
    if raw_weight > MAX_WEIGHT_GRAMS:
        raise QuoteRequestError(
            "weight_grams",
            f"weight_grams must not exceed {MAX_WEIGHT_GRAMS}",
        )

    destination = payload.get("destination")
    if destination not in DESTINATIONS:
        raise QuoteRequestError(
            "destination",
            f"destination must be one of {sorted(DESTINATIONS)}",
        )

    express = payload.get("express", False)
    if not isinstance(express, bool):
        raise QuoteRequestError("express", "express must be a boolean")

    return QuoteRequest(
        weight_grams=raw_weight, destination=destination, express=express
    )


def quote_pence(request: QuoteRequest) -> int:
    """Price one parcel in pence."""
    base, per_kg = DESTINATIONS[request.destination]
    whole_kg = (request.weight_grams + 999) // 1000
    total = base + per_kg * whole_kg
    if request.weight_grams >= HEAVY_THRESHOLD_GRAMS:
        total += HEAVY_SURCHARGE_PENCE
    if request.express:
        total *= EXPRESS_MULTIPLIER
    return total
