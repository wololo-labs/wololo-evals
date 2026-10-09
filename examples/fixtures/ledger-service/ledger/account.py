"""Account state: postings, reversals and the running balance.

The balance is maintained incrementally rather than recomputed, which is where
the sequence-dependent defects live. A test that posts one entry and checks the
balance cannot see any of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .money import DEFAULT_CURRENCY, convert

__all__ = ["Posting", "Account", "LedgerError"]


class LedgerError(RuntimeError):
    """A posting was refused."""


@dataclass(frozen=True)
class Posting:
    entry_id: str
    minor: int
    currency: str = DEFAULT_CURRENCY
    reverses: Optional[str] = None


@dataclass
class Account:
    """One account's postings and its running balance in minor units.

    ``balance`` is kept in the account's own currency. Idempotency is by
    ``entry_id``: re-posting the same id is a no-op rather than an error, so a
    retrying caller cannot double-count, and a reversal of an already reversed
    entry is refused rather than applied twice.
    """

    currency: str = DEFAULT_CURRENCY
    balance: int = 0
    postings: Dict[str, Posting] = field(default_factory=dict)
    reversed_ids: List[str] = field(default_factory=list)

    def post(self, posting: Posting) -> int:
        """Apply one posting, returning the balance after it."""
        if posting.entry_id in self.postings:
            # Idempotent retry: the caller may send the same entry twice.
            return self.balance

        if posting.reverses is not None:
            original = self.postings.get(posting.reverses)
            if original is None:
                raise LedgerError(f"cannot reverse unknown entry {posting.reverses!r}")
            if posting.reverses in self.reversed_ids:
                raise LedgerError(f"entry {posting.reverses!r} is already reversed")
            self.reversed_ids.append(posting.reverses)
            self.postings[posting.entry_id] = posting
            self.balance -= self._in_account_currency(original)
            return self.balance

        self.postings[posting.entry_id] = posting
        self.balance += self._in_account_currency(posting)
        return self.balance

    def _in_account_currency(self, posting: Posting) -> int:
        if posting.currency == self.currency:
            return posting.minor
        return convert(posting.minor, posting.currency, self.currency)

    def recompute(self) -> int:
        """Rebuild the balance from the postings, ignoring the running total.

        The two must agree. Where they do not, the incremental path has a bug
        and this is what proves it.
        """
        total = 0
        for entry_id, posting in self.postings.items():
            if posting.reverses is not None:
                original = self.postings.get(posting.reverses)
                if original is not None:
                    total -= self._in_account_currency(original)
                continue
            if entry_id in self.reversed_ids:
                continue
            total += self._in_account_currency(posting)
        return total
