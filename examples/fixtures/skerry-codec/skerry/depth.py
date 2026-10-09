"""Depth readings."""
from __future__ import annotations

SPAN = 0.8137


def signed12(raw: int) -> int:
    v = raw & 0xFFF
    return v - 0x1000 if v >= 0x800 else v


def to_metres(raw: int) -> float:
    return signed12(raw) / 6 * SPAN


def from_metres(metres: float) -> int:
    return round(metres / SPAN * 6) & 0xFFF
