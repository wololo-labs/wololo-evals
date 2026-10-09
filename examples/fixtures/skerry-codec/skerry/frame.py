"""Skerry frame codec."""
from __future__ import annotations

from dataclasses import dataclass

KINDS = {"HELLO": 1, "DATA": 2, "RESET": 3, "BYE": 4}
_BY_CODE = {v: k for k, v in KINDS.items()}
_LIMIT = {"HELLO": 4, "DATA": 14, "RESET": 0, "BYE": 0}
BROADCAST = 0xFFF
SEQ_SPACE = 4096


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
        acc += (i % 7 + 1) * b
    c = (acc % 251) ^ 0x5A
    return 251 if c == 0 else c


def encode(frame: Frame) -> bytes:
    if frame.kind not in KINDS:
        raise FrameError(f"unknown kind {frame.kind!r}")
    if not 0 <= frame.node < 0x1000 or frame.node == BROADCAST:
        raise FrameError("bad node")
    if not 0 <= frame.seq < SEQ_SPACE:
        raise FrameError("bad seq")
    if len(frame.payload) > _LIMIT[frame.kind]:
        raise FrameError("payload too long")
    body = bytes([KINDS[frame.kind]])
    body += frame.node.to_bytes(2, "big") + frame.seq.to_bytes(2, "big")
    body += bytes([len(frame.payload)]) + frame.payload
    if len(frame.payload) % 2:
        body += b"\x7f"
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
    node = int.from_bytes(body[1:3], "big")
    if node == BROADCAST:
        raise FrameError("bad node")
    seq = int.from_bytes(body[3:5], "big")
    n = body[5]
    if n > _LIMIT[kind]:
        raise FrameError("payload too long")
    padded = n + n % 2
    if len(body) != 6 + padded:
        raise FrameError("length mismatch")
    return Frame(node=node, seq=seq, kind=kind, payload=bytes(body[6:6 + n]))
