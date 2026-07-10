"""repo-eval suite (SPEC S5.3), exercised end to end without a docker daemon.

Three layers, mirroring test_harness_uplift:
1. ``generate.py`` against a REAL throwaway git repo: the commit-history walk
   picks the isolable ``fix:`` commit and builds a fixture with the right
   parent ref + historical line count;
2. ``inspect_adapter.run_repo_eval`` over a fake exec seam: single-arm
   harness-under-eval, correctness (acceptance in-sandbox) + rule-based
   churn_penalty + hardened judge, three non-trivial dimensions;
3. ``worker.execute_run`` on real Postgres: per-trial rows, aggregate score
   rows, frozen composite, single-arm verdict_stats, leaderboard row.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from wololo_evals.harness_solver import PROFILE, TRANSCRIPT, WORKSPACE

from wololo_evals import versioning
from wololo_evals.db import tenant_txn
from wololo_evals.harnesses import resolve_runner
from wololo_evals.inspect_adapter import run_repo_eval
from wololo_evals.jobs.worker import execute_run
from wololo_evals.suites import base as suites_base
from wololo_evals.suites.repo_eval import generate as repo_gen
from wololo_evals.suites.repo_eval.task import RepoEvalFixture, churn_penalty_score

TENANT = "meridian"

# The fake harness always changes exactly 3 diff lines; a fixture historical
# count of 3 makes churn_penalty a clean 1.0 and 6 makes it a partial 0.5.
DIFF = (
    "diff --git a/kyc.py b/kyc.py\n"
    "--- a/kyc.py\n"
    "+++ b/kyc.py\n"
    "@@ -1,2 +1,3 @@\n"
    "+    if amount is None: return  # validation gate\n"
    "+    log.info('validated')\n"
    "-    process(amount)\n"
)


def _events(n: int) -> str:
    return "\n".join(json.dumps({"stage": "post-llm-event"}) for _ in range(n))


class FakeRepoSandbox:
    """Single-arm fake executor: clone/checkout succeed, the harness produces
    DIFF, the acceptance test passes. No skill is ever installed (repo-eval is
    single-arm), which the test asserts."""

    def __init__(self, turns: int = 4):
        self.turns = turns
        self.installed: list[str] = []
        self.harness_runs: list[str] = []

    async def exec(self, cmd, env=None, timeout=0):
        if cmd[0] == "git" and "diff" in cmd:
            return (0, DIFF, "")
        if cmd[0] == "git" and "status" in cmd:
            return (0, "", "")
        if cmd[0] == "cat":
            return (0, self._events, "")
        if cmd[0] in ("rm", "mkdir", "git"):
            return (0, "", "")
        if cmd[0] == "sh":
            script = cmd[2]
            rest = cmd[4:]
            if "printf" in script:
                self.installed.append(rest[-1])  # would be a skill install
                return (0, "", "")
            if 'exec "$@"' in script:
                if rest and rest[0] in ("claude", "hermes"):
                    self.harness_runs.append(rest[0])
                    self._events = _events(self.turns)
                    return (0, json.dumps({"result": "done"}), "")
                return (0, "", "")  # acceptance command passes
        raise AssertionError(f"unexpected command: {cmd}")

    _events = ""


def _judge(system: str, user: str) -> str:
    return json.dumps(
        {"dimension_scores": {"abstraction_adherence": 0.8},
         "rationale": "reuses helpers", "injection_suspected": False}
    )


def _fixture(historical: int = 3) -> RepoEvalFixture:
    return RepoEvalFixture(
        fixture_id="kyc-onboarding",
        repo="/srv/fixtures/atlas-onboarding-api.git",
        parent_ref="cafef00d^",
        commit_ref="cafef00d",
        task_prompt="fix: reject onboarding with a null amount",
        acceptance_command=("true",),
        historical_lines_changed=historical,
        max_turns=8,
    )


# --- layer 1: generate.py over a real git repo ------------------------------
def _init_repo(path) -> str:
    def git(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "app.py").write_text("def process(x):\n    return x\n")
    git("add", "app.py")
    git("commit", "-q", "-m", "chore: scaffold")
    # a qualifying fix: touches source + a paired test, conventional prefix
    (path / "app.py").write_text("def process(x):\n    if x is None:\n        raise ValueError\n    return x\n")
    (path / "test_app.py").write_text("from app import process\n\ndef test_none():\n    pass\n")
    git("add", "app.py", "test_app.py")
    git("commit", "-q", "-m", "fix: reject None amount in process")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_generate_picks_qualifying_commit(tmp_path):
    head = _init_repo(tmp_path)
    fx = repo_gen.generate(str(tmp_path))
    assert fx.commit_ref == head
    assert fx.parent_ref == f"{head}^"
    assert fx.task_prompt.startswith("fix: reject None amount")
    # source (2 added) + brand-new test file (4 added) = 6 changed lines
    assert fx.historical_lines_changed == 6
    assert fx.acceptance_command[:3] == ("python", "-m", "pytest")
    assert "test_app.py" in fx.acceptance_command
    # the commit's own test is the correctness oracle (materialized from
    # commit_ref before acceptance); source files are not in oracle_files.
    assert fx.oracle_files == ("test_app.py",)


def test_generate_skips_when_no_qualifying_commit(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "docs: note"],
                   check=True, capture_output=True)
    with pytest.raises(ValueError, match="no qualifying"):
        repo_gen.generate(str(tmp_path))


# --- churn_penalty rule ------------------------------------------------------
def test_churn_penalty_rule():
    assert churn_penalty_score(6, 3) == 1.0    # smaller than historical: full marks
    assert churn_penalty_score(3, 6) == 0.5    # twice the churn: half marks
    assert churn_penalty_score(3, 0) == 0.0    # changed nothing: no work done


# --- layer 2: inspect adapter -----------------------------------------------
async def test_run_repo_eval_adapter_three_dimensions():
    fake = FakeRepoSandbox()
    runner = resolve_runner("claude-code")
    outcome = await asyncio.to_thread(
        run_repo_eval, _fixture(historical=3), runner, 5, _judge,
        k=5, exec_factory=lambda: fake.exec,
    )
    assert outcome.status == "completed", outcome.error
    assert len(outcome.trials) == 5
    assert fake.installed == []  # single-arm: no skill ever installed
    tr = outcome.trials[0]
    dv = tr["dimension_values"]
    assert dv["correctness"] == 1.0
    assert dv["churn_penalty"] == 1.0            # 3 historical / 3 candidate
    assert dv["abstraction_adherence"] == 0.8
    # 0.5*1 + 0.25*1 + 0.25*0.8
    assert tr["composite"] == pytest.approx(0.95, abs=1e-6)


# --- layer 2b: REAL generate->run over an added-test commit -----------------
# The prior fake proves nothing about correctness: FakeRepoSandbox returns
# acceptance-pass unconditionally. This exercises the whole path for real git +
# real pytest, substituting only the harness CLI (which can't run here). It is
# the regression guard for the "correctness scored against the WRONG test" bug:
# the fix commit ADDS its oracle test, so at the redacted parent that file does
# not exist, and without materialising it from commit_ref a genuinely-correct
# fix scores correctness=0.
class LocalExec:
    """Runs every command for real via subprocess, remapping the container
    paths (/workspace,/profile,/transcript) under a host temp root. The harness
    CLI is the one substituted step: it writes the fix to the workspace SOURCE
    (never the oracle test) plus a fleet-hooks transcript, exactly as a real
    harness would; clone, checkout, oracle materialise, and pytest are real."""

    def __init__(self, root: pathlib.Path, fix_rel: str, fix_content: str):
        self.root = str(root)
        self.fix_rel = fix_rel
        self.fix_content = fix_content
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
                (ws / self.fix_rel).write_text(self.fix_content)
                ev = pathlib.Path(f"{self.root}/transcript/events.jsonl")
                ev.parent.mkdir(parents=True, exist_ok=True)
                ev.write_text(_events(3))
                return (0, json.dumps({"result": "applied fix"}), "")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout or None
        )
        return (proc.returncode, proc.stdout, proc.stderr)


def _init_added_test_repo(path) -> str:
    """Buggy source, then a fix commit that fixes source AND adds a brand-new
    oracle test file (absent at the parent)."""
    def git(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "app.py").write_text("def process(amount):\n    return amount * 2\n")
    git("add", "app.py")
    git("commit", "-q", "-m", "chore: scaffold onboarding")
    (path / "app.py").write_text(
        "def process(amount):\n"
        "    if amount is None:\n"
        "        raise ValueError('amount required')\n"
        "    return amount * 2\n"
    )
    (path / "test_app.py").write_text(
        "import pytest\n"
        "from app import process\n\n"
        "def test_rejects_none():\n"
        "    with pytest.raises(ValueError):\n"
        "        process(None)\n\n"
        "def test_doubles():\n"
        "    assert process(3) == 6\n"
    )
    git("add", "app.py", "test_app.py")
    git("commit", "-q", "-m", "fix: reject None amount in process")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


# the correct fix the "harness" applies (source only; the oracle test must come
# from commit_ref via materialisation, not from the harness)
_CORRECT_FIX = (
    "def process(amount):\n"
    "    if amount is None:\n"
    "        raise ValueError('amount required')\n"
    "    return amount * 2\n"
)


async def test_repo_eval_real_added_test_commit_scores_correct(tmp_path):
    repo = tmp_path / "repo"
    _init_added_test_repo(repo)
    # explicit interpreter so pytest resolves inside this venv, not host python
    fx = repo_gen.generate(
        str(repo),
        acceptance_command=[sys.executable, "-m", "pytest", "-q", "test_app.py"],
    )
    assert fx.oracle_files == ("test_app.py",)
    runner = resolve_runner("claude-code")

    # WITH the oracle materialised: a correct fix scores correctness 1.0
    exe = LocalExec(tmp_path / "sbx-ok", "app.py", _CORRECT_FIX)
    outcome = await asyncio.to_thread(
        run_repo_eval, fx, runner, 1, _judge, k=5, exec_factory=lambda: exe.exec,
    )
    assert outcome.status == "completed", outcome.error
    assert exe.harness_runs == 1
    assert outcome.trials[0]["dimension_values"]["correctness"] == 1.0

    # WITHOUT the oracle (the bug): the added test is absent at the parent, so
    # the same correct fix runs pytest against nothing and scores correctness 0.
    import dataclasses
    fx_no_oracle = dataclasses.replace(fx, oracle_files=())
    exe2 = LocalExec(tmp_path / "sbx-bug", "app.py", _CORRECT_FIX)
    outcome2 = await asyncio.to_thread(
        run_repo_eval, fx_no_oracle, runner, 1, _judge, k=5,
        exec_factory=lambda: exe2.exec,
    )
    assert outcome2.trials[0]["dimension_values"]["correctness"] == 0.0


# --- layer 3: worker on real Postgres ---------------------------------------
async def _make_repo_run(engine, cipher, *, historical=3):
    fx = _fixture(historical=historical)
    payload = {
        "harness": "claude-code",
        "model": "claude-sonnet-4",
        "repo_eval_fixture": {
            "fixture_id": fx.fixture_id, "repo": fx.repo,
            "parent_ref": fx.parent_ref, "commit_ref": fx.commit_ref,
            "task_prompt": fx.task_prompt,
            "acceptance_command": list(fx.acceptance_command),
            "historical_lines_changed": fx.historical_lines_changed,
            "max_turns": fx.max_turns,
        },
    }
    ct, kid, sha = cipher.encrypt_payload(TENANT, payload)
    cid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    sv = versioning.suite_version(suites_base.suite_dir("repo-eval"))
    rv = versioning.rubric_version(suites_base.rubric_path("repo-eval"))
    async with tenant_txn(engine, TENANT) as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.candidates "
                "(id, tenant_id, candidate_type, candidate_ref, proposed_by, "
                " baseline_ref, payload_ciphertext, payload_key_id, payload_sha256) "
                "values (:id,:t,'harness-config',:ref,'actor','onboarding@0',:ct,:kid,:sha)"
            ),
            {"id": cid, "t": TENANT, "ref": "onboarding-repo-eval", "ct": ct,
             "kid": kid, "sha": sha},
        )
        await conn.execute(
            text(
                "insert into wololo_evals.runs "
                "(id, tenant_id, candidate_id, eval_suite, status, suite_version, "
                " rubric_version, n_trials, eval_log_prefix) "
                "values (:id,:t,:c,'repo-eval','queued',:sv,:rv,5,:lp)"
            ),
            {"id": rid, "t": TENANT, "c": cid, "sv": sv, "rv": rv,
             "lp": f"evals/{TENANT}/{rid}/"},
        )
    return rid


async def test_worker_repo_eval_end_to_end(app_engine, cipher):
    rid = await _make_repo_run(app_engine, cipher)
    fake = FakeRepoSandbox()
    async with tenant_txn(app_engine, TENANT) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_judge, k=5,
            exec_factory=lambda: fake.exec,
        )
    assert out["status"] == "passed", out
    assert out["composite_score"] == pytest.approx(0.95, abs=1e-6)
    assert out["verdict_stats"]["n"] == 5

    async with tenant_txn(app_engine, TENANT) as conn:
        trials = (await conn.execute(text(
            "select arm, count(*) from wololo_evals.trials where run_id = :r group by arm"
        ), {"r": rid})).all()
        scores = (await conn.execute(text(
            "select dimension from wololo_evals.scores "
            "where run_id = :r and trial_index is null order by dimension"
        ), {"r": rid})).all()
        lb = (await conn.execute(text(
            "select harness, model, composite_score from wololo_evals.leaderboard "
            "where run_id = :r"), {"r": rid})).first()
    assert {arm: n for arm, n in trials} == {"single": 5}
    assert [d for (d,) in scores] == ["abstraction_adherence", "churn_penalty", "correctness"]
    assert lb.harness == "claude-code" and lb.model == "claude-sonnet-4"
    assert lb.composite_score == pytest.approx(0.95, abs=1e-6)
