"""``wololo-evals-cli run``: refusals, the live path (fake engine) and the report.

The live path is driven through a fake engine and a fake grader so no model is
called. The replay path against a real archive is proved end to end in
``test_hello_skill.py``.
"""
from __future__ import annotations

import importlib
import json
import pathlib

import pytest

from wololo_evals.cli import matrix as matrix_mod
from wololo_evals.cli.main import main
from wololo_evals.core import interfaces as ci

# `wololo_evals.cli.main` the attribute is the function; the module is needed here.
main_mod = importlib.import_module("wololo_evals.cli.main")
_COST_PER_RUN = 0.01


class _Engine:
    def __init__(self, engine_id: str) -> None:
        self.engine_id = engine_id

    def cli_version(self) -> str:
        return "2.2.0"

    def build_command(self, spec):  # noqa: ANN001
        return ["true"]

    def raw_event_path(self, spec) -> pathlib.Path:  # noqa: ANN001
        return pathlib.Path(spec.cwd) / f"{self.engine_id}.raw.jsonl"

    def run(self, spec):  # noqa: ANN001
        return ci.RunResult(
            run_id=spec.run_id, status="ok", artefact_dir=spec.cwd,
            runtime_seconds=1.0, cost_usd=_COST_PER_RUN, tokens_in=1, tokens_out=1,
        )


def _grader(spec, result, trace):  # noqa: ANN001
    # The skill arm is the only one that has an asset mounted.
    good = 1.0 if spec.asset.body or spec.asset.files else 0.0
    return [
        ci.ScoredSignal("act", "gate", 1.0, 1.0, "pass", slot="activation_correct"),
        ci.ScoredSignal(
            "outcome", "assertion", good, 1.0, "pass" if good else "fail", slot="task_outcome"
        ),
    ]


@pytest.fixture
def skill_repo(tmp_path, monkeypatch):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\nDo a thing.\n", encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(
        "asset: skill\nbenchmark: qe-skill\nengine: claude\nmodel: m\nmax_cost_usd: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        main_mod, "_live_seams", lambda cfg, args: (lambda e: _Engine(e), None, _grader)
    )
    return tmp_path


def _run(repo: pathlib.Path, *extra: str) -> int:
    return main(["run", "--config", str(repo / "eval.yaml"), *extra])


def _report(repo: pathlib.Path) -> dict:
    return json.loads((repo / "eval-gate-report.json").read_text(encoding="utf-8"))


def test_live_run_gates_skill_against_no_skill_and_writes_both_reports(skill_repo):
    assert _run(skill_repo) == 0
    report = _report(skill_repo)
    assert report["verdict"] == "pass"
    assert report["mode"] == "live"
    assert report["scores"]["skill"] > report["scores"]["no_skill"]
    assert report["spend_usd"] == pytest.approx(24 * _COST_PER_RUN)
    assert report["stats"]["significant_improvement"] is True
    md = (skill_repo / "eval-gate-report.md").read_text(encoding="utf-8")
    assert md.startswith("# Gate verdict: PASS")


def test_the_cost_cap_is_shared_by_both_arms(skill_repo, monkeypatch):
    caps = []
    real = matrix_mod.run_matrix

    def spy(config, *a, **kw):
        caps.append(kw["max_cost_usd"])
        return real(config, *a, **kw)

    monkeypatch.setattr(matrix_mod, "run_matrix", spy)
    _run(skill_repo)
    assert caps[0] == 5
    assert caps[1] == pytest.approx(5 - 12 * _COST_PER_RUN)


def test_live_run_without_max_cost_usd_is_refused_before_anything_runs(skill_repo, capsys):
    cfg = skill_repo / "eval.yaml"
    cfg.write_text(cfg.read_text().replace("max_cost_usd: 5\n", ""), encoding="utf-8")
    assert _run(skill_repo) == 2
    assert "max_cost_usd" in capsys.readouterr().err
    assert not (skill_repo / "eval-gate-report.json").exists()
    assert not (skill_repo / ".wololo-evals").exists()


def test_flag_overrides_the_file(skill_repo):
    cfg = skill_repo / "eval.yaml"
    cfg.write_text(cfg.read_text().replace("max_cost_usd: 5\n", ""), encoding="utf-8")
    assert _run(skill_repo, "--max-cost-usd", "5") == 0


def test_bad_key_is_refused_naming_the_key(skill_repo, capsys):
    cfg = skill_repo / "eval.yaml"
    cfg.write_text(cfg.read_text() + "max_cost: 1\n", encoding="utf-8")
    assert _run(skill_repo) == 2
    assert "`max_cost`" in capsys.readouterr().err


def test_replay_needs_both_arm_directories(skill_repo, capsys):
    archive = skill_repo / "archive"
    (archive / "skill").mkdir(parents=True)
    assert _run(skill_repo, "--replay", str(archive)) == 2
    assert "no-skill/" in capsys.readouterr().err


def _grader_with(skill_outcome: float, bare_outcome: float):
    def grader(spec, result, trace):  # noqa: ANN001
        outcome = skill_outcome if spec.asset.files else bare_outcome
        return [
            ci.ScoredSignal("act", "gate", 1.0, 1.0, "pass", slot="activation_correct"),
            ci.ScoredSignal(
                "outcome", "assertion", outcome, 1.0,
                "pass" if outcome >= 0.5 else "fail", slot="task_outcome",
            ),
        ]

    return grader


def _use(monkeypatch, grader) -> None:
    monkeypatch.setattr(
        main_mod, "_live_seams", lambda cfg, args: (lambda e: _Engine(e), None, grader)
    )


def test_a_skill_worse_than_no_skill_exits_1(skill_repo, monkeypatch):
    _use(monkeypatch, _grader_with(skill_outcome=0.0, bare_outcome=1.0))
    assert _run(skill_repo) == 1
    assert _report(skill_repo)["verdict"] == "regression"


def test_thresholds_pass_is_an_absolute_floor(skill_repo, monkeypatch):
    # Better than no skill (0.0 -> 0.6) but under the 0.95 bar the file sets.
    _use(monkeypatch, _grader_with(skill_outcome=0.6, bare_outcome=0.0))
    cfg = skill_repo / "eval.yaml"
    cfg.write_text(cfg.read_text() + "thresholds:\n  pass: 0.95\n", encoding="utf-8")
    assert _run(skill_repo) == 1
    report = _report(skill_repo)
    assert report["verdict"] == "regression"
    assert "thresholds.pass" in report["reason"]


def test_report_paths_are_relative_and_subject_comes_from_ci(skill_repo, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/skills")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    _run(skill_repo)
    report = _report(skill_repo)
    assert not pathlib.PurePath(report["run"]).is_absolute()
    assert str(skill_repo) not in json.dumps(report)
    assert report["subject"] == {
        "repo": "https://github.com/acme/skills", "path": "skill", "commit": "a" * 40,
    }


def test_no_subject_outside_ci(skill_repo, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    _run(skill_repo)
    assert "subject" not in _report(skill_repo)


def test_record_under_replay_is_refused(skill_repo, capsys):
    archive = skill_repo / "archive"
    for arm in ("skill", "no-skill"):
        (archive / arm).mkdir(parents=True)
    assert _run(skill_repo, "--replay", str(archive), "--record", str(skill_repo / "out")) == 2
    assert "--record" in capsys.readouterr().err


def test_a_replay_archive_with_a_symlink_is_refused(skill_repo, capsys):
    archive = skill_repo / "archive"
    for arm in ("skill", "no-skill"):
        (archive / arm).mkdir(parents=True)
    (archive / "skill" / "case").symlink_to(skill_repo / "eval.yaml")
    assert _run(skill_repo, "--replay", str(archive)) == 2
    assert "symlink" in capsys.readouterr().err
