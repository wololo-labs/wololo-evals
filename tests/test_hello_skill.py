"""examples/hello-skill end to end: the fully working eval.yaml example.

Runs ``wololo-evals-cli run --config eval.yaml --replay replay`` over a copy of
the committed example and checks the report. The recorded streams are
hand-authored, so the proof that no number is faked is independent: the test
recomputes each task-outcome signal straight from the files the recorded runs
left in their workspaces and requires the gate to have reported the same.
"""
from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from wololo_evals.cli import cases_compile
from wololo_evals.cli.main import main

_EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "hello-skill"
_IGNORE = shutil.ignore_patterns("eval-gate-report.*", ".wololo-evals")
_CASES = {"ada": "hello-01-ada", "grace": "hello-02-grace", "linus": "hello-03-linus"}


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    work = tmp_path_factory.mktemp("hello") / "hello-skill"
    shutil.copytree(_EXAMPLE, work, ignore=_IGNORE)
    assert main(["run", "--config", str(work / "eval.yaml"), "--replay", str(work / "replay")]) == 0
    return json.loads((work / "eval-gate-report.json").read_text(encoding="utf-8"))


def _recorded(arm: str, case: str) -> pathlib.Path:
    return _EXAMPLE / "replay" / arm / case / "seed-0" / "workspace"


def test_report_says_the_skill_passes_at_zero_spend(report):
    assert report["verdict"] == "pass"
    assert report["mode"] == "replay"
    assert report["spend_usd"] == 0
    assert report["stats"]["significant_improvement"] is True
    assert report["stats"]["buckets"]["regression"] == 0
    assert report["scores"]["skill"] > report["scores"]["no_skill"]


@pytest.mark.parametrize("name", sorted(_CASES))
def test_task_outcome_is_recomputed_from_the_recorded_workspaces(report, name):
    signal = next(s for s in report["signals"] if s["evaluator_id"] == f"greeted_{name}")
    for arm, key in (("no-skill", "baseline_mean"), ("skill", "candidate_mean")):
        written = (_recorded(arm, _CASES[name]) / "greeting.txt").read_text(encoding="utf-8")
        assert signal[key] == float(written == f"Hello, {name.capitalize()}!")


@pytest.mark.parametrize("arm", ["no-skill", "skill"])
@pytest.mark.parametrize("name", sorted(_CASES))
def test_recorded_output_blob_agrees_with_the_file_it_describes(arm, name):
    ws = _recorded(arm, _CASES[name])
    blob = json.loads((ws / ".gh-aw-advisory" / "output.json").read_text(encoding="utf-8"))
    assert blob["greeting"] == (ws / "greeting.txt").read_text(encoding="utf-8")


def test_the_no_skill_arm_is_a_real_control_not_all_zeros(report):
    assert 0 < report["scores"]["no_skill"] < report["scores"]["skill"]


def test_recorded_streams_do_not_claim_a_model_produced_them():
    for prov in (_EXAMPLE / "replay").rglob("provenance.json"):
        assert json.loads(prov.read_text(encoding="utf-8"))["model_id"] == "synthetic:hand-authored"


def test_committed_casebook_is_compiled():
    ok, why = cases_compile.check(
        _EXAMPLE / "benchmark" / "cases.toml", _EXAMPLE / "benchmark" / "cases.jsonl"
    )
    assert ok, why
