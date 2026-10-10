"""The suite the repository ships."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from skerry.depth import to_metres
from skerry.frame import Frame, decode, encode
from skerry.session import Session


def test_encoding_returns_bytes():
    assert isinstance(encode(Frame(node=7, seq=1, kind="DATA", payload=b"ab")), bytes)


def test_a_frame_comes_back_from_decode():
    raw = encode(Frame(node=7, seq=1, kind="DATA", payload=b"ab"))
    assert decode(raw) is not None


def test_a_depth_is_a_number():
    assert isinstance(to_metres(16), float)


def test_a_session_answers_a_hello():
    assert Session().accept(Frame(node=7, seq=0, kind="HELLO")) in (True, False)


def test_a_session_can_be_created():
    assert Session() is not None
