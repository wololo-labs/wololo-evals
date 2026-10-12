"""examples/hello-skill end to end: the fully working eval.yaml example.

Runs ``wololo-evals-cli run --config eval.yaml --replay replay`` over a copy of
the committed example and checks the report. The recorded streams are
hand-authored, so the proof that no number is faked is independent: the test
recomputes each task-outcome signal straight from the files the recorded runs
left in their workspaces and requires the gate to have reported the same.
"""
from __future__ import annotations

import json
import os
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


class _RecordedEngine:
    """A live-engine stand-in that behaves as the recorded runs did and costs money."""

    engine_id = "claude"
    COST = 0.05

    def cli_version(self) -> str:
        return "2.2.0"

    def build_command(self, spec):  # noqa: ANN001
        return []

    def raw_event_path(self, spec) -> pathlib.Path:  # noqa: ANN001
        return pathlib.Path(spec.cwd) / ".gh-aw-advisory" / "claude.raw.jsonl"

    def run(self, spec):  # noqa: ANN001
        from wololo_evals.core import interfaces as ci

        arm = "skill" if spec.asset.files else "no-skill"
        src = _EXAMPLE / "replay" / arm / spec.case.case_id / "seed-0"
        shutil.copytree(src / "workspace", spec.cwd, dirs_exist_ok=True)
        shutil.copy(src / "claude.raw.jsonl", self.raw_event_path(spec))
        return ci.RunResult(
            run_id=spec.run_id, status="ok", artefact_dir=spec.cwd,
            runtime_seconds=1.0, cost_usd=self.COST, tokens_in=1, tokens_out=1,
        )


def test_a_live_run_recorded_then_replayed_scores_the_same_at_zero_spend(tmp_path):
    """The path an adopter takes: run live on dispatch, commit the archive, replay on PRs."""
    from wololo_evals.cli import run as run_mod

    work = tmp_path / "hello-skill"
    shutil.copytree(_EXAMPLE, work, ignore=_IGNORE)
    shutil.rmtree(work / "replay")
    cfg = str(work / "eval.yaml")
    archive = work / "recorded"

    args = run_mod_args(cfg, record=str(archive))
    assert run_mod.run(args, adapter_factory=lambda engine: _RecordedEngine()) == 0
    live = json.loads((work / "eval-gate-report.json").read_text(encoding="utf-8"))
    assert live["mode"] == "live"
    assert live["spend_usd"] == pytest.approx(8 * _RecordedEngine.COST)

    assert main(["run", "--config", cfg, "--replay", str(archive)]) == 0
    replayed = json.loads((work / "eval-gate-report.json").read_text(encoding="utf-8"))
    assert replayed["mode"] == "replay" and replayed["spend_usd"] == 0
    assert replayed["signals"] == live["signals"]
    assert replayed["scores"] == live["scores"]


def run_mod_args(config: str, **overrides):
    import argparse

    from wololo_evals.cli import run as run_mod

    parser = argparse.ArgumentParser()
    run_mod.add_arguments(parser)
    args = parser.parse_args(["--config", config])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class _LeakyEngine(_RecordedEngine):
    """Echoes a credential into its own stream and plants a link to a host file."""

    def __init__(self, secret_file: pathlib.Path) -> None:
        self._secret_file = secret_file

    def run(self, spec):  # noqa: ANN001
        result = super().run(spec)
        raw = self.raw_event_path(spec)
        raw.write_text(
            raw.read_text(encoding="utf-8").replace(
                "wrote greeting.txt", "key=sk-test-placeholder-not-a-secret"
            ),
            encoding="utf-8",
        )
        (pathlib.Path(spec.cwd) / "stolen.txt").symlink_to(self._secret_file)
        os.link(self._secret_file, pathlib.Path(spec.cwd) / "hard.txt")
        return result


def test_a_recorded_archive_holds_no_credentials_and_no_followed_links(tmp_path, monkeypatch):
    from wololo_evals.cli import run as run_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder-not-a-secret")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("PRIVATE-HOST-DATA", encoding="utf-8")
    work = tmp_path / "hello-skill"
    shutil.copytree(_EXAMPLE, work, ignore=_IGNORE)
    shutil.rmtree(work / "replay")

    args = run_mod_args(str(work / "eval.yaml"), record=str(work / "recorded"))
    assert run_mod.run(args, adapter_factory=lambda e: _LeakyEngine(secret)) == 0

    files = [f for f in (work / "recorded").rglob("*") if f.is_file() or f.is_symlink()]
    assert files
    assert not any(f.is_symlink() for f in files)
    blob = "".join(f.read_text(encoding="utf-8") for f in files if f.suffix in (".jsonl", ".json", ".txt"))
    assert "sk-test-placeholder-not-a-secret" not in blob and "[redacted]" in blob
    assert "PRIVATE-HOST-DATA" not in blob
    # Neither the hardlink's content nor the file itself reached the archive or the kept tree.
    assert not list((work / "recorded").rglob("hard.txt"))
    assert not any(
        "PRIVATE-HOST-DATA" in f.read_text(encoding="utf-8")
        for f in (work / ".wololo-evals").rglob("*") if f.is_file() and not f.is_symlink()
    )
    # The run tree the matrix kept has the link as a link, not a copy of its target.
    kept = [p for p in (work / ".wololo-evals").rglob("stolen.txt")]
    assert kept and all(p.is_symlink() for p in kept)


def test_redaction_scrubs_binary_files_too(tmp_path):
    from wololo_evals.cli.run import _redact

    f = tmp_path / "blob.bin"
    f.write_bytes(b"\xff\xfe sk-test-placeholder-not-a-secret \x00")
    _redact(tmp_path, ["sk-test-placeholder-not-a-secret"])
    assert b"sk-live" not in f.read_bytes() and b"[redacted]" in f.read_bytes()
