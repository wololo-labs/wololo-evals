"""The harness-under-eval moat, exercised without a docker daemon (SPEC S6).

A fake :data:`SandboxExec` stands in for the Inspect sandbox so the whole
bridge runs end to end: fresh clone, profile-scoped skill install on the
treatment arm only, the harness's own headless loop, transcript-derived turn
counting, git-diff capture, the in-sandbox acceptance test, the hardened judge,
and the weighted composite. Three layers are covered:

1. ``run_harness_trial`` directly (the subprocess bridge over the exec seam);
2. ``inspect_adapter.run_task_uplift`` (the Inspect Task/Solver/Scorer wiring);
3. ``worker.execute_run`` on task-uplift for BOTH Claude Code and Hermes,
   producing real paired uplift statistics and a leaderboard row.

The fake makes both arms succeed; the treatment arm spends fewer harness turns
(a real skill effect), so the uplift is a genuine positive number computed from
paired trials, not an asserted constant.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text

from wololo_evals import versioning
from wololo_evals.crypto import EnvelopeCipher
from wololo_evals.db import tenant_txn
from wololo_evals.harness_solver import HarnessTrialConfig, run_harness_trial
from wololo_evals.harnesses import resolve_runner
from wololo_evals.inspect_adapter import run_task_uplift
from wololo_evals.jobs.worker import execute_run
from wololo_evals.suites import base as suites_base
from wololo_evals.suites.task_uplift.task import TaskFixture

TENANT = "meridian"
BASELINE_TURNS = 5
TREATMENT_TURNS = 3


class FakeSandbox:
    """Sequential, arm-aware fake executor. Arm is inferred from whether a
    skill was installed after the last clone (treatment installs; baseline does
    not) — safe because the injected-exec path runs samples sequentially."""

    def __init__(self):
        self.arm = "baseline"
        self.installed: list[str] = []
        self.harness_runs: list[str] = []

    @staticmethod
    def _events(n: int) -> str:
        return "\n".join(
            json.dumps({"stage": "post-llm-event", "i": i}) for i in range(n)
        )

    async def exec(self, cmd, env=None, timeout=0):
        c0 = cmd[0]
        if c0 in ("rm", "mkdir"):
            return (0, "", "")
        if c0 == "git":
            if "clone" in cmd:
                self.arm = "baseline"  # each trial starts fresh
                return (0, "", "")
            if "checkout" in cmd:
                return (0, "", "")
            if "diff" in cmd:
                return (0, f"diff --git a/kyc.py b/kyc.py\n+    verified ({self.arm})\n", "")
            if "status" in cmd:
                return (0, "", "")
        if c0 == "cat":
            n = TREATMENT_TURNS if self.arm == "treatment" else BASELINE_TURNS
            return (0, self._events(n), "")
        if c0 == "sh":
            script, rest = cmd[2], cmd[4:]
            if "printf" in script:  # skill install: [content, path]
                self.arm = "treatment"
                self.installed.append(rest[-1])
                return (0, "", "")
            if 'exec "$@"' in script:
                if rest and rest[0] in ("claude", "hermes"):
                    self.harness_runs.append(rest[0])
                    return (0, json.dumps({"result": "done"}), "")
                return (0, "", "")  # acceptance command passes
        raise AssertionError(f"fake sandbox got an unexpected command: {cmd}")


def _judge(system: str, user: str) -> str:
    return json.dumps(
        {"dimension_scores": {"approach_quality": 0.8},
         "rationale": "sound approach", "injection_suspected": False}
    )


def _fixture() -> TaskFixture:
    return TaskFixture(
        fixture_id="kyc-fix",
        repo="/srv/fixtures/atlas-kyc-svc.git",
        ref="deadbeef",
        task_prompt="Fix the KYC silent-drop bug.",
        acceptance_command=("true",),
        max_turns=8,
        wall_clock_cap_s=120,
    )


SKILL_FILES = {"SKILL.md": "# kyc-fix\nAlways add the missing validation gate."}


# --- layer 1: the subprocess bridge -----------------------------------------
async def test_run_harness_trial_baseline_has_no_skill():
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    cfg = HarnessTrialConfig(
        runner=runner, task_prompt="fix it", fixture_repo="r", fixture_ref="ref",
        arm="baseline", max_turns=8,
    )
    result = await run_harness_trial(fake.exec, cfg)
    assert result.exit_code == 0
    assert "verified (baseline)" in result.diff
    assert result.turns == BASELINE_TURNS
    assert fake.installed == []  # baseline never touches the profile


async def test_run_harness_trial_treatment_installs_skill_in_profile():
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    cfg = HarnessTrialConfig(
        runner=runner, task_prompt="fix it", fixture_repo="r", fixture_ref="ref",
        arm="treatment", max_turns=8, skill_slug="kyc-fix", skill_files=SKILL_FILES,
    )
    result = await run_harness_trial(fake.exec, cfg)
    assert result.turns == TREATMENT_TURNS
    # profile-scoped install, inside the sandbox, under the harness skills dir
    assert fake.installed == ["/profile/skills/kyc-fix/SKILL.md"]


# --- layer 2: the Inspect adapter wiring ------------------------------------
async def test_run_task_uplift_adapter_end_to_end():
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    outcome = await asyncio.to_thread(
        run_task_uplift,
        _fixture(), runner, 2, "kyc-fix", SKILL_FILES, _judge,
        k=5, exec_factory=lambda: fake.exec,
    )
    assert outcome.status == "completed", outcome.error
    assert len(outcome.trials) == 4  # 2 arms x 2 trials
    by_arm = {"baseline": [], "treatment": []}
    for tr in outcome.trials:
        by_arm[tr["arm"]].append(tr["composite"])
    # treatment spends fewer turns -> strictly higher composite every pair
    assert min(by_arm["treatment"]) > max(by_arm["baseline"])
    assert fake.harness_runs.count("claude") == 4  # real harness cmd per sample


# --- layer 3: the worker, per harness ---------------------------------------
@pytest.fixture()
def cipher(tmp_path):
    return EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "keyring.json"))


async def _make_uplift_run(engine, cipher, harness) -> str:
    payload = {
        "harness": harness,
        "skill_slug": "kyc-fix",
        "skill_files": SKILL_FILES,
        "model": "claude-sonnet-4",
        "task_fixture": {
            "fixture_id": "kyc-fix",
            "repo": "/srv/fixtures/atlas-kyc-svc.git",
            "ref": "deadbeef",
            "task_prompt": "Fix the KYC silent-drop bug.",
            "acceptance_command": ["true"],
            "max_turns": 8,
            "wall_clock_cap_s": 120,
        },
    }
    ct, kid, sha = cipher.encrypt_payload(TENANT, payload)
    cid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    sv = versioning.suite_version(suites_base.suite_dir("task-uplift"))
    rv = versioning.rubric_version(suites_base.rubric_path("task-uplift"))
    async with tenant_txn(engine, TENANT) as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.candidates "
                "(id, tenant_id, candidate_type, candidate_ref, proposed_by, "
                " baseline_ref, payload_ciphertext, payload_key_id, payload_sha256) "
                "values (:id,:t,'skill',:ref,'actor','kyc@0',:ct,:kid,:sha)"
            ),
            {"id": cid, "t": TENANT, "ref": f"kyc-fix@{harness}", "ct": ct,
             "kid": kid, "sha": sha},
        )
        await conn.execute(
            text(
                "insert into wololo_evals.runs "
                "(id, tenant_id, candidate_id, eval_suite, status, suite_version, "
                " rubric_version, n_trials, eval_log_prefix) "
                "values (:id,:t,:c,'task-uplift','queued',:sv,:rv,5,:lp)"
            ),
            {"id": rid, "t": TENANT, "c": cid, "sv": sv, "rv": rv,
             "lp": f"evals/{TENANT}/{rid}/"},
        )
    return rid


@pytest.mark.parametrize("harness", ["claude-code", "hermes"])
async def test_worker_task_uplift_per_harness(app_engine, cipher, harness):
    rid = await _make_uplift_run(app_engine, cipher, harness)
    fake = FakeSandbox()
    async with tenant_txn(app_engine, TENANT) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_judge, k=5,
            exec_factory=lambda: fake.exec,
        )

    assert out["status"] == "passed", out
    vs = out["verdict_stats"]
    assert vs["n"] == 5 and vs["wins"] == 5
    assert vs["mean"] == pytest.approx(5.952, abs=0.01)  # real paired uplift_pct
    assert out["composite_score"] == pytest.approx(0.89, abs=1e-6)

    async with tenant_txn(app_engine, TENANT) as conn:
        trials = (await conn.execute(text(
            "select arm, count(*) from wololo_evals.trials where run_id = :r group by arm"
        ), {"r": rid})).all()
        lb = (await conn.execute(text(
            "select harness, model, composite_score from wololo_evals.leaderboard "
            "where run_id = :r"), {"r": rid})).first()
        run = (await conn.execute(text(
            "select status, composite_score, verdict_stats from wololo_evals.runs "
            "where id = :r"), {"r": rid})).first()

    counts = {arm: n for arm, n in trials}
    assert counts == {"baseline": 5, "treatment": 5}
    assert lb.harness == harness and lb.model == "claude-sonnet-4"
    assert run.status == "passed"
    stats = run.verdict_stats if isinstance(run.verdict_stats, dict) else json.loads(run.verdict_stats)
    assert stats["uplift_pct"] == pytest.approx(5.952, abs=0.01)
    # every harness resolved through the adapter-manifest, no hard-coded name
    assert set(fake.harness_runs) == {harness.split("-")[0]}
