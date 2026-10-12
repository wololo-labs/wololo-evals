"""action.yml: the composite action's shape, and its gate step run for real.

GitHub is not available here, so the step's bash is extracted and executed with
the environment the runner would give it. That covers the logic that matters
(which mode an event selects, the output contract, the exit code); the
`uses:` plumbing around it is covered by the repo's own workflow, which runs
this action on every pull request.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_IGNORE = shutil.ignore_patterns("eval-gate-report.*", ".wololo-evals")
_ACTION = yaml.safe_load((_ROOT / "action.yml").read_text(encoding="utf-8"))
_STEPS = _ACTION["runs"]["steps"]
_GATE = next(s for s in _STEPS if s.get("id") == "gate")


def test_it_is_a_composite_action_with_the_documented_inputs():
    assert _ACTION["runs"]["using"] == "composite"
    assert set(_ACTION["inputs"]) == {"config", "replay", "max-cost-usd", "python-version"}
    assert set(_ACTION["outputs"]) == {"verdict", "report"}
    assert all("run" not in s or s["shell"] == "bash" for s in _STEPS)


def test_the_report_is_uploaded_even_when_the_gate_fails_and_failure_comes_last():
    upload = next(s for s in _STEPS if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["if"] == "always()"
    assert _STEPS.index(upload) > _STEPS.index(_GATE)
    assert _STEPS[-1]["run"].startswith("exit ")


def _run_gate(tmp_path: pathlib.Path, event: str, *, replay: str = "", cap: str = "", cfg_text=None):
    work = tmp_path / "repo"
    shutil.copytree(_ROOT / "examples" / "hello-skill", work, ignore=_IGNORE)
    if cfg_text is not None:
        (work / "eval.yaml").write_text(cfg_text, encoding="utf-8")
    out, summary = tmp_path / "output", tmp_path / "summary"
    env = {
        **os.environ,
        "PATH": f"{pathlib.Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
        "EVAL_CONFIG": "eval.yaml", "EVAL_REPLAY": replay,
        "EVAL_MAX_COST_USD": cap, "EVAL_EVENT": event,
    }
    done = subprocess.run(
        ["bash", "-e", "-c", _GATE["run"]], cwd=work, env=env, capture_output=True, text=True
    )
    outputs = dict(l.split("=", 1) for l in out.read_text(encoding="utf-8").splitlines())
    return done, outputs, work, summary


def test_pull_request_scores_the_archive_and_reports_a_pass(tmp_path):
    done, outputs, work, summary = _run_gate(tmp_path, "pull_request", replay="replay")
    assert done.returncode == 0, done.stderr
    assert outputs["verdict"] == "pass" and outputs["code"] == "0"
    assert (work / outputs["report"]).is_file()
    assert summary.read_text(encoding="utf-8").startswith("# Gate verdict: PASS")


def test_pull_request_without_an_archive_is_refused_not_run_live(tmp_path):
    done, outputs, work, _ = _run_gate(tmp_path, "pull_request")
    assert outputs["code"] == "2" and outputs["verdict"] == "refused"
    assert "::error::" in done.stdout
    assert not (work / "eval-gate-report.json").exists()


def test_dispatch_runs_live_and_so_refuses_without_a_cost_cap(tmp_path):
    no_cap = "asset: skill\nbenchmark: benchmark\nengine: claude\nmodel: m\n"
    done, outputs, work, _ = _run_gate(tmp_path, "workflow_dispatch", cfg_text=no_cap)
    assert outputs["code"] == "2"
    assert "max_cost_usd" in done.stderr
    assert not (work / ".wololo-evals").exists()  # nothing ran, so nothing was spent


def test_dispatch_ignores_the_replay_archive(tmp_path):
    """Dispatch is live even when a replay archive is configured."""
    no_cap = "asset: skill\nbenchmark: benchmark\nengine: claude\nmodel: m\n"
    done, outputs, work, _ = _run_gate(
        tmp_path, "workflow_dispatch", replay="replay", cfg_text=no_cap
    )
    assert outputs["code"] == "2" and "max_cost_usd" in done.stderr


def test_a_regression_fails_the_step_chain_with_exit_1(tmp_path):
    work = tmp_path / "probe"
    shutil.copytree(_ROOT / "examples" / "hello-skill" / "replay", work / "replay")
    (work / "replay" / "skill").rename(work / "replay" / "tmp")
    (work / "replay" / "no-skill").rename(work / "replay" / "skill")
    (work / "replay" / "tmp").rename(work / "replay" / "no-skill")
    done, outputs, _, _ = _run_gate(tmp_path, "pull_request", replay=str(work / "replay"))
    assert outputs["verdict"] == "regression" and outputs["code"] == "1"
    final = subprocess.run(
        ["bash", "-c", _STEPS[-1]["run"]], env={"EVAL_CODE": outputs["code"]}, capture_output=True
    )
    assert final.returncode == 1
