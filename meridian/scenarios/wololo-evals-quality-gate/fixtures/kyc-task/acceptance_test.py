"""Acceptance test for the KYC silent-drop task (SPEC S14 rung 3, proof #1).

This goal AUTHORS the acceptance test; the Meridian fixture seeds the bug and
wololo-evals owns proving the fix. It runs INSIDE the trial sandbox against the
candidate's modified checkout of ``atlas-kyc-svc``: exit 0 = correct.

The seeded bug: the retry wrapper treats a provider 5xx as terminal and
silently drops the applicant. The fix must (1) retry transient 5xx and succeed
when the provider recovers, and (2) surface exhaustion as a raised error, never
a silent drop that removes the applicant from onboarding.

The repo under test is expected to expose ``kyc.submit_kyc(record, client)``
where ``client`` is a callable returning a provider response with a numeric
``status`` and a ``decision`` (or raising for transport errors). The test
drives it with a flaky client, so it is agnostic to how the fix is implemented
(a repaired wrapper, a reused backoff helper, whatever) — it asserts behaviour,
not internals.
"""
from __future__ import annotations

import pytest

kyc = pytest.importorskip("kyc")


class _Resp:
    def __init__(self, status, decision=None):
        self.status = status
        self.decision = decision


class _FlakyProvider:
    """Fails with a 5xx the first ``fail_times`` calls, then approves."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, record):
        self.calls += 1
        if self.calls <= self.fail_times:
            return _Resp(503)
        return _Resp(200, decision="approved")


class _AlwaysDown:
    def __init__(self):
        self.calls = 0

    def __call__(self, record):
        self.calls += 1
        return _Resp(503)


def test_transient_5xx_is_retried_not_dropped():
    provider = _FlakyProvider(fail_times=2)
    result = kyc.submit_kyc({"applicant": "a-1"}, provider)
    # the applicant is NOT silently dropped: a decision comes back after retries
    assert getattr(result, "decision", None) == "approved"
    assert provider.calls >= 3  # it actually retried the transient failures


def test_exhausted_retries_raise_not_silent_drop():
    provider = _AlwaysDown()
    with pytest.raises(Exception):
        kyc.submit_kyc({"applicant": "a-2"}, provider)
    # exhaustion surfaces as an error the caller can act on, never a silent
    # empty/None result that removes the applicant from onboarding.
    assert provider.calls >= 2
