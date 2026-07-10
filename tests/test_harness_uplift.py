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
import dataclasses
import json
import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from wololo_evals import versioning
from wololo_evals.crypto import EnvelopeCipher
from wololo_evals.db import tenant_txn
from wololo_evals.harness_solver import (
    PROFILE,
    TRANSCRIPT,
    WORKSPACE,
    HarnessTrialConfig,
    run_harness_trial,
)
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


# --- regression guard: correctness immune to acceptance-test tampering -------
# Mirrors test_repo_eval.test_repo_eval_real_added_test_commit_scores_correct.
# The treatment arm installs an attacker-controllable skill and the harness can
# write anywhere in /workspace, including the acceptance test. This exercises the
# whole path for real git + real pytest, substituting only the harness CLI —
# which, instead of fixing the bug, STUBS the acceptance test so it passes. With
# fixture.ref re-materialized over the oracle paths, the trusted test is restored
# and runs against the still-buggy source -> correctness 0. Without it (the bug),
# the harness's passing stub scores correctness 1 falsely and inflates uplift.
class TamperExec:
    """Runs every command for real via subprocess, remapping container paths
    (/workspace,/profile,/transcript) under a host temp root. The one
    substituted step is the harness CLI: it OVERWRITES the acceptance test with
    an always-passing stub and leaves the buggy source untouched, exactly as a
    malicious/over-eager candidate skill could instruct a real harness to do."""

    def __init__(self, root: pathlib.Path, test_rel: str, stub: str):
        self.root = str(root)
        self.test_rel = test_rel
        self.stub = stub
        self.harness_runs = 0

    def _remap(self, s: str) -> str:
        return (
            s.replace(WORKSPACE, f"{self.root}/workspace")
            .replace(PROFILE, f"{self.root}/profile")
            .replace(TRANSCRIPT, f"{self.root}/transcript")
        )

    async def exec(self, cmd, env=None, timeout=0):
        cmd = [self._remap(str(c)) for c in cmd]
        if cmd[0] == "sh" and len(cmd) >= 5 and 'exec "$@"' in cmd[2]:
            rest = cmd[4:]
            if rest and rest[0] in ("claude", "hermes"):
                self.harness_runs += 1
                ws = pathlib.Path(f"{self.root}/workspace")
                (ws / self.test_rel).write_text(self.stub)  # weaken the oracle
                ev = pathlib.Path(f"{self.root}/transcript/events.jsonl")
                ev.parent.mkdir(parents=True, exist_ok=True)
                ev.write_text(FakeSandbox._events(3))
                return (0, json.dumps({"result": "stubbed the test"}), "")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout or None
        )
        return (proc.returncode, proc.stdout, proc.stderr)


def _init_buggy_repo(path) -> str:
    """A repo whose source is buggy and whose test_app.py is a real oracle that
    FAILS on the bug. HEAD is the trusted fixture.ref: the acceptance test lives
    here in trusted form, and the source is intentionally left unfixed."""
    def git(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    # buggy: never raises on a null amount (the KYC silent-drop bug)
    (path / "app.py").write_text("def process(amount):\n    return amount\n")
    (path / "test_app.py").write_text(
        "import pytest\n"
        "from app import process\n\n"
        "def test_rejects_none():\n"
        "    with pytest.raises(ValueError):\n"
        "        process(None)\n"
    )
    git("add", "app.py", "test_app.py")
    git("commit", "-q", "-m", "scaffold: buggy process + real oracle test")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


# the always-passing stub the "harness" writes over the real oracle test
_STUB_TEST = "def test_rejects_none():\n    pass\n"


async def test_task_uplift_test_tampering_scored_zero_with_oracle(tmp_path):
    repo = tmp_path / "repo"
    ref = _init_buggy_repo(repo)
    runner = resolve_runner("claude-code")
    acceptance = [sys.executable, "-m", "pytest", "-q", "test_app.py"]
    fx = TaskFixture(
        fixture_id="kyc-tamper",
        repo=str(repo),
        ref=ref,
        task_prompt="Fix the KYC silent-drop bug.",
        acceptance_command=tuple(acceptance),
        oracle_files=("test_app.py",),
        max_turns=8,
        wall_clock_cap_s=120,
    )

    # WITH the oracle re-materialized from fixture.ref: the harness's stub is
    # overwritten by the trusted test, which fails against the buggy source.
    exe = TamperExec(tmp_path / "sbx-defended", "test_app.py", _STUB_TEST)
    outcome = await asyncio.to_thread(
        run_task_uplift, fx, runner, 1, "kyc-fix", SKILL_FILES, _judge,
        k=5, arms=("baseline",), exec_factory=lambda: exe.exec,
    )
    assert outcome.status == "completed", outcome.error
    assert exe.harness_runs == 1
    assert outcome.trials[0]["dimension_values"]["correctness"] == 0.0

    # WITHOUT the oracle (the bug): the harness's always-passing stub is what
    # pytest runs, so a candidate that fixed nothing scores correctness 1.0.
    fx_no_oracle = dataclasses.replace(fx, oracle_files=())
    exe2 = TamperExec(tmp_path / "sbx-cheated", "test_app.py", _STUB_TEST)
    outcome2 = await asyncio.to_thread(
        run_task_uplift, fx_no_oracle, runner, 1, "kyc-fix", SKILL_FILES, _judge,
        k=5, arms=("baseline",), exec_factory=lambda: exe2.exec,
    )
    assert outcome2.trials[0]["dimension_values"]["correctness"] == 1.0
