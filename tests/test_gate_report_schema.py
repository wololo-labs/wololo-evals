"""The published gate-report schema: vectors behave, and real output conforms.

The schema is the contract with whatever reads a verdict (a skill catalogue, a
dashboard), so the producers are held to it here rather than the other way
round. Nothing under ``specs/`` imports, or is imported by, a consumer.
"""
from __future__ import annotations

import json
import pathlib
import shutil

import jsonschema
import pytest

from wololo_evals.cli.main import main

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = _ROOT / "specs" / "eval-gate-report"
_SCHEMA = json.loads((_SPEC / "schemas" / "eval-gate-report.v1.schema.json").read_text("utf-8"))
_VECTORS = sorted((_SPEC / "test-vectors").glob("*.json"))
_FORMATS = jsonschema.FormatChecker()


def _errors(doc) -> list:
    return list(jsonschema.Draft202012Validator(_SCHEMA, format_checker=_FORMATS).iter_errors(doc))


def test_schema_is_itself_valid_and_versioned():
    jsonschema.Draft202012Validator.check_schema(_SCHEMA)
    assert _SCHEMA["$id"].endswith("eval-gate-report.v1.schema.json")
    assert _SCHEMA["properties"]["schema_version"] == {"const": "1.0.0"}


@pytest.mark.parametrize("vector", _VECTORS, ids=lambda p: p.stem)
def test_vector(vector):
    errors = _errors(json.loads(vector.read_text(encoding="utf-8")))
    if vector.name.startswith("valid-"):
        assert not errors, [e.message for e in errors]
    else:
        assert vector.name.startswith("invalid-")
        assert errors, f"{vector.name} should be rejected"


def test_there_are_vectors_of_both_kinds():
    names = [v.name for v in _VECTORS]
    assert any(n.startswith("valid-") for n in names)
    assert any(n.startswith("invalid-") for n in names)


def test_a_real_replay_report_conforms(tmp_path):
    work = tmp_path / "hello-skill"
    shutil.copytree(_ROOT / "examples" / "hello-skill", work)
    assert main(["run", "--config", str(work / "eval.yaml"), "--replay", str(work / "replay")]) == 0
    report = json.loads((work / "eval-gate-report.json").read_text(encoding="utf-8"))
    assert not _errors(report), [e.message for e in _errors(report)]


def test_a_real_regression_report_conforms(tmp_path):
    """The no-skill arm as the 'skill' and the skill arm as the control."""
    work = tmp_path / "hello-skill"
    shutil.copytree(_ROOT / "examples" / "hello-skill", work)
    replay = work / "replay"
    (replay / "skill").rename(replay / "tmp")
    (replay / "no-skill").rename(replay / "skill")
    (replay / "tmp").rename(replay / "no-skill")
    assert main(["run", "--config", str(work / "eval.yaml"), "--replay", str(replay)]) == 1
    report = json.loads((work / "eval-gate-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "regression"
    assert not _errors(report), [e.message for e in _errors(report)]


def test_the_gate_command_output_conforms_too(tmp_path):
    """`gate` writes the same shape, minus the run-only fields."""
    corpus = _ROOT / "examples" / "runs" / "dropdowns-synthetic"
    out = tmp_path / "verdict.json"
    code = main([
        "gate", "--run", str(corpus / "candidate-v2"), "--baseline", str(corpus / "baseline-v1"),
        "--benchmark", str(corpus / "benchmark"), "--golden-agreement", "1.0", "-o", str(out),
    ])
    assert code == 1
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert not _errors(verdict), [e.message for e in _errors(verdict)]
