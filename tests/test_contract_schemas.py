"""Contract drift + test-vector conformance (validation rung 1).

Mirrors the kernel's ``tests/test_contract_schemas.py``: walk the open
``specs/eval-candidate`` and ``specs/eval-result`` schema trees, assert every
schema pins the ``schemaVersion`` the registry expects, and hold the
committed test-vectors to their name: ``valid*`` MUST validate, ``invalid-*``
MUST fail (a MUST-FAIL vector that silently passes means the schema stopped
guarding a required field).
"""
from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator, ValidationError

from wololo_evals.contracts.eval_candidate import build_eval_candidate
from wololo_evals.contracts.eval_result import (
    build_eval_run,
    build_eval_score,
    build_eval_trial,
    build_leaderboard_entry,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The registry the kernel's core/contracts/version.py carries for these
# families; every schema file must agree with it.
SCHEMA_VERSIONS = {
    "eval-candidate": 1,
    "eval-run": 1,
    "eval-trial": 1,
    "eval-score": 1,
    "leaderboard-entry": 1,
}

SCHEMA_DIRS = [
    ROOT / "specs" / "eval-candidate" / "schemas",
    ROOT / "specs" / "eval-result" / "schemas",
]


def _schema_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for d in SCHEMA_DIRS:
        files.extend(sorted(d.glob("*.schema.json")))
    return files


def test_every_kind_has_a_schema():
    titles = {json.loads(f.read_text())["title"] for f in _schema_files()}
    assert titles == set(SCHEMA_VERSIONS), (
        f"schema kinds {titles} do not match registry {set(SCHEMA_VERSIONS)}"
    )


@pytest.mark.parametrize("schema_file", _schema_files(), ids=lambda p: p.name)
def test_schema_version_matches_registry(schema_file: pathlib.Path):
    schema = json.loads(schema_file.read_text())
    kind = schema["title"]
    pinned = schema["properties"]["schemaVersion"]["const"]
    assert pinned == SCHEMA_VERSIONS[kind], (
        f"{schema_file.name} pins schemaVersion {pinned}, registry says "
        f"{SCHEMA_VERSIONS[kind]}"
    )
    # A valid schema in its own right (catches typos that make it un-loadable).
    Draft202012Validator.check_schema(schema)


def _vector_cases() -> list[tuple[pathlib.Path, pathlib.Path]]:
    cases: list[tuple[pathlib.Path, pathlib.Path]] = []
    for schema_file in _schema_files():
        kind = json.loads(schema_file.read_text())["title"]
        vec_dir = schema_file.parents[1] / "test-vectors" / kind
        for vec in sorted(vec_dir.glob("*.json")):
            cases.append((schema_file, vec))
    return cases


@pytest.mark.parametrize(
    "schema_file,vector", _vector_cases(), ids=lambda p: p.name if p.suffix else str(p)
)
def test_vector_matches_its_name(schema_file: pathlib.Path, vector: pathlib.Path):
    schema = json.loads(schema_file.read_text())
    payload = json.loads(vector.read_text())
    validator = Draft202012Validator(schema)
    if vector.name.startswith("invalid-"):
        with pytest.raises(ValidationError):
            validator.validate(payload)
    else:
        validator.validate(payload)  # valid* must pass


def test_builders_emit_schema_valid_records():
    """The Python builders and the published schemas cannot drift apart."""
    records = {
        "eval-candidate": build_eval_candidate(
            tenant_id="meridian",
            candidate_type="skill",
            candidate_ref="simplify@1.2.0",
            proposed_by="autoresearch:gen-7",
            baseline_ref="simplify@1.1.0",
            payload={"SKILL.md": "# simplify"},
        ),
        "eval-run": build_eval_run(
            tenant_id="meridian",
            run_id="11111111-1111-1111-1111-111111111111",
            candidate_id="22222222-2222-2222-2222-222222222222",
            eval_suite="skill-review",
            status="queued",
            suite_version="a" * 64,
            rubric_version="b" * 64,
            n_trials=5,
        ),
        "eval-trial": build_eval_trial(
            tenant_id="meridian",
            run_id="11111111-1111-1111-1111-111111111111",
            trial_index=0,
            arm="single",
            status="completed",
        ),
        "eval-score": build_eval_score(
            tenant_id="meridian",
            run_id="11111111-1111-1111-1111-111111111111",
            dimension="validation_gate",
            value=0.8,
            suite_version="a" * 64,
            rubric_version="b" * 64,
        ),
        "leaderboard-entry": build_leaderboard_entry(
            tenant_id="meridian",
            model="claude-sonnet",
            harness="claude-code",
            skill_ref="simplify@1.2.0",
            composite_score=0.82,
            suite_version="a" * 64,
            rubric_version="b" * 64,
            run_id="11111111-1111-1111-1111-111111111111",
        ),
    }
    by_title = {
        json.loads(f.read_text())["title"]: json.loads(f.read_text())
        for f in _schema_files()
    }
    for kind, record in records.items():
        Draft202012Validator(by_title[kind]).validate(record)
