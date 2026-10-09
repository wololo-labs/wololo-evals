"""Session tracking."""
from __future__ import annotations

from .frame import SEQ_SPACE, Frame


def ahead(new: int, old: int) -> bool:
    return 0 < (new - old) % SEQ_SPACE < SEQ_SPACE // 2


class Session:
    def __init__(self) -> None:
        self.node = None
        self.last = None
        self.open = False

    def accept(self, f: Frame) -> bool:
        if f.kind == "HELLO":
            self.node, self.open, self.last = f.node, True, None
            return True
        if not self.open or f.node != self.node:
            return False
        if f.kind == "RESET":
            self.last = None
            return True
        if f.kind == "BYE":
            self.open = False
            return True
        if self.last is not None and not ahead(f.seq, self.last):
            return False
        self.last = f.seq
        return True
