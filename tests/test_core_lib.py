"""M1 core-extraction invariants: the eval core is a pure, DB-free library.

Proves the facade promise from ``docs/plans/wave-contracts.md`` and the
expansion spec's M1 milestone:

- ``wololo_evals.core`` imports and its judge / stats / scoring / versioning /
  suites surface is callable with **zero** ``WOLOLO_EVALS_*`` env vars set and
  **no** Postgres fixture (none of the ``pg``/``app_engine`` fixtures are
  touched here — this whole module is pure).
- A judge-only configuration (``JudgeSettings``) needs only judge env vars,
  while the aggregate ``Settings`` stays fail-closed on the mandatory
  DB/master-key/JWKS trio (so ``api.py``/``asgi.py``/``jobs.worker`` are
  unchanged).
- The N>=5 trial floor (SPEC S6.2 / S7, DOCTRINE 15.6) is enforced *in the
  core facade* as an explicit ``ValueError``, while the raw ``stats.py``
  surface the service imports keeps computing degenerate verdicts unchanged.
"""
from __future__ import annotations

import pytest

from wololo_evals import core
from wololo_evals import stats as raw_stats
from wololo_evals.settings import (
    JudgeSettings,
    Settings,
    reset_settings_for_tests,
)

# Mandatory, no-default env vars that the aggregate Settings demands.
_MANDATORY_ENV = (
    "WOLOLO_EVALS_DATABASE_URL",
    "WOLOLO_EVALS_MASTER_KEY",
    "WOLOLO_EVALS_JWKS",
)


@pytest.fixture()
def no_db_env(monkeypatch):
    """Strip every WOLOLO_EVALS_* var so 'works with zero DB env' is real."""
    import os

    for key in list(os.environ):
        if key.startswith("WOLOLO_EVALS_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_for_tests()
    yield
    reset_settings_for_tests()


# ---------------------------------------------------------------------------
# 1. The core library is importable + callable with no DB, no env, no network.
# ---------------------------------------------------------------------------
def test_scoring_roundtrip_no_db_env(no_db_env):
    h = "a" * 64
    score = core.build_eval_score("tenant-1", "run-1", "activation", 0.5, h, h)
    assert score["dimension"] == "activation"
    assert score["value"] == 0.5
    assert score["suite_version"] == h

    run = core.build_eval_run(
        "tenant-1", "run-1", "cand-1", "skill-review", "passed", h, h, 5
    )
    assert run["eval_suite"] == "skill-review"
    assert run["n_trials"] == 5


def test_stats_callable_no_db_env(no_db_env):
    diffs = core.paired_uplift_pcts([0.5, 0.6, 0.5, 0.4, 0.5], [0.6, 0.66, 0.6, 0.54, 0.48])
    stats = core.verdict_stats(diffs)
    assert stats["n"] == 5
    assert stats["wins"] == 4


def test_judge_roundtrip_with_injected_call_model(no_db_env):
    """run_judge is callable with a fake, network-free call_model."""
    import json

    def fake_call_model(system: str, user: str) -> str:
        assert "SECURITY RULES" in system  # hardened prompt built by the core
        return json.dumps(
            {
                "dimension_scores": {"clarity": 0.8, "safety": 0.9},
                "rationale": "ok",
                "injection_suspected": False,
            }
        )

    result = core.run_judge(
        fake_call_model, "rubric text", "candidate output", ["clarity", "safety"], k=3
    )
    assert result.dimension_medians == {"clarity": 0.8, "safety": 0.9}
    assert result.injection_suspected is False
    assert len(result.samples) == 3


def test_versioning_and_suites_callable_no_db_env(no_db_env):
    # suite_dir/rubric_path are pure path helpers; rubric_version hashes bytes.
    rpath = core.rubric_path("skill-review")
    assert rpath.name == "rubric.yaml"
    version = core.rubric_version(rpath)
    assert len(version) == 64


# ---------------------------------------------------------------------------
# 2. Judge-only settings need only judge env vars; aggregate stays fail-closed.
# ---------------------------------------------------------------------------
def test_judge_settings_needs_no_db_env(no_db_env):
    js = JudgeSettings()  # constructs with the mandatory DB trio unset
    assert js.judge_backend == "http"
    call_model = core.http_call_model(
        js.judge_model, base_url=js.judge_base_url, api_key=js.judge_api_key
    )
    assert callable(call_model)


def test_aggregate_settings_fails_closed_without_db_env(no_db_env):
    with pytest.raises(RuntimeError) as exc:
        Settings()
    assert exc.value.args[0].startswith("required environment variable")


def test_aggregate_settings_flat_surface_preserved(monkeypatch):
    """api/asgi/worker read flat attributes; the split keeps them working."""
    for key in _MANDATORY_ENV:
        monkeypatch.setenv(key, "x" * 64 if "MASTER" in key else "value")
    reset_settings_for_tests()
    try:
        cfg = Settings()
        # every flat accessor the service uses still resolves
        assert cfg.database_url == "value"
        assert cfg.master_key_hex == "x" * 64
        assert cfg.jwks_path == "value"
        assert cfg.judge_model  # defaulted
        assert cfg.blob_backend == "fs"
        assert cfg.deploy_base_url
        # and the composed sub-settings are reachable
        assert cfg.judge.judge_model == cfg.judge_model
    finally:
        reset_settings_for_tests()


# ---------------------------------------------------------------------------
# 3. N>=5 trial floor enforced in-library (ValueError), raw stats unchanged.
# ---------------------------------------------------------------------------
def test_floor_raises_below_five():
    with pytest.raises(core.FloorViolation):
        core.verdict_stats_floored([1.0, 2.0, 3.0])
    # FloorViolation is a ValueError so existing handlers keep catching it.
    assert issubclass(core.FloorViolation, ValueError)


def test_floor_passes_at_and_above_five():
    stats = core.verdict_stats_floored([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["n"] == 5
    # identical to the raw engine once the floor is cleared
    assert stats == raw_stats.verdict_stats([1.0, 2.0, 3.0, 4.0, 5.0])


def test_floor_smoke_bypass_is_non_verdict():
    stats = core.verdict_stats_floored([0.9], smoke=True)
    assert stats["n"] == 1
    assert stats["low_confidence"] is True


def test_single_arm_floor_enforced():
    with pytest.raises(core.FloorViolation):
        core.single_arm_stats_floored([0.7, 0.8], pass_bar=0.5)
    ok = core.single_arm_stats_floored([0.7, 0.8, 0.9, 0.6, 0.75], pass_bar=0.5)
    assert ok["n"] == 5


def test_check_trial_floor_direct():
    core.check_trial_floor(5)  # no raise
    core.check_trial_floor(1, smoke=True)  # no raise
    with pytest.raises(core.FloorViolation):
        core.check_trial_floor(4)


def test_raw_stats_floor_agnostic_unchanged():
    """worker.py/api.py import stats directly; that path must be untouched:
    the raw engine still computes a degenerate verdict below the floor."""
    degenerate = raw_stats.verdict_stats([1.0, 2.0])
    assert degenerate["n"] == 2
    assert degenerate["low_confidence"] is True
