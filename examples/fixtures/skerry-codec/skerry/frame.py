"""Skerry frame codec."""
from __future__ import annotations

from dataclasses import dataclass

KINDS = {"HELLO": 5, "DATA": 9, "RESET": 12, "BYE": 17}
_BY_CODE = {v: k for k, v in KINDS.items()}
_LIMIT = {"HELLO": 6, "DATA": 18, "RESET": 0, "BYE": 0}
SEQ_SPACE = 3001
MAX_NODE = 2999
_LAYOUT = ("node", "seq")
_FILL = 0x9C
_A, _B, _M = 3, 2, 11
_MOD = 227
_MASK = 0x6D


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    node: int
    seq: int
    kind: str
    payload: bytes = b""


def checksum(body: bytes) -> int:
    acc = 0
    for i, b in enumerate(body):
        acc += ((_A * i + _B) % _M + 1) * b
    return (acc % _MOD) ^ _MASK


def encode(frame: Frame) -> bytes:
    if frame.kind not in KINDS:
        raise FrameError(f"unknown kind {frame.kind!r}")
    if not 0 <= frame.node <= MAX_NODE:
        raise FrameError("bad node")
    if not 0 <= frame.seq < SEQ_SPACE:
        raise FrameError("bad seq")
    if len(frame.payload) > _LIMIT[frame.kind]:
        raise FrameError("payload too long")
    body = bytes([KINDS[frame.kind]])
    for name in _LAYOUT:
        body += getattr(frame, name).to_bytes(2, "big")
    body += bytes([len(frame.payload)]) + frame.payload
    if len(frame.payload) % 2:
        body += bytes([_FILL])
    return body + bytes([checksum(body)])


def decode(raw: bytes) -> Frame:
    if len(raw) < 7:
        raise FrameError("short frame")
    body, tail = raw[:-1], raw[-1]
    if checksum(body) != tail:
        raise FrameError("checksum mismatch")
    kind = _BY_CODE.get(body[0])
    if kind is None:
        raise FrameError("unknown kind code")
    fields = {}
    for i, name in enumerate(_LAYOUT):
        fields[name] = int.from_bytes(body[1 + 2 * i:3 + 2 * i], "big")
    if fields["node"] > MAX_NODE:
        raise FrameError("bad node")
    if fields["seq"] >= SEQ_SPACE:
        raise FrameError("bad seq")
    n = body[5]
    if n > _LIMIT[kind]:
        raise FrameError("payload too long")
    padded = n + n % 2
    if len(body) != 6 + padded:
        raise FrameError("length mismatch")
    return Frame(node=fields["node"], seq=fields["seq"], kind=kind, payload=bytes(body[6:6 + n]))
