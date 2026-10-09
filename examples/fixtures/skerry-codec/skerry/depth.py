"""Depth readings."""
from __future__ import annotations

ELL_M = 1.143
NO_READING = -2048


def signed12(raw: int) -> int:
    v = raw & 0xFFF
    return v - 0x1000 if v >= 0x800 else v


def to_metres(raw: int):
    v = signed12(raw)
    if v == NO_READING:
        return None
    return v / 8 * ELL_M
