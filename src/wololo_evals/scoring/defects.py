"""Seeded-defect detection: does this suite go red when the bug is planted?

Mutation testing answers "how much of the code does the suite pin"; this
answers the sharper question a QE gate is actually asked - *would it have
caught the bug*. A benchmark declares named defects next to the fixture
(``seeded_defects.json``), each an exact-string edit; for every one the scorer
stages a copy of the target, applies the edit, runs the candidate's tests, and
records whether the suite failed.

A suite that stays green against a planted defect did not catch it, however
many mutants it kills elsewhere. That is the number a sponsor asks for, so it
is its own signal rather than a component of the kill rate.

The edits are exact-string, never regexes or line numbers: an edit that no
longer matches is a loud :class:`DefectApplyError`, so a defect manifest that
drifted from the fixture fails the gate instead of quietly scoring zero
defects caught.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from wololo_evals.core.interfaces import ScoredSignal
from wololo_evals.scoring.mutation import COPY_IGNORE_PATTERNS, stage_analysis_workspace

__all__ = [
    "DEFECT_EVALUATOR_ID",
    "DEFECT_SLOT",
    "DefectError",
    "DefectApplyError",
    "SeededDefect",
    "DefectReport",
    "load_defects",
    "apply_defect",
    "run_defect_detection",
    "defect_signal",
    "error_signal",
]

#: §16.1 slot the catch rate feeds: mechanical evidence about the run's output.
DEFECT_SLOT = "task_outcome"

DEFECT_EVALUATOR_ID = "seeded_defects_caught"

_DEFAULT_TIMEOUT_S = 120


class DefectError(RuntimeError):
    """Defect detection could not produce a catch rate."""


class DefectApplyError(DefectError):
    """A defect's ``find`` text is not in the file it names: the manifest has
    drifted from the fixture."""


@dataclass(frozen=True)
class SeededDefect:
    id: str
    title: str
    file: str
    find: str
    replace: str
    rationale: str = ""


@dataclass(frozen=True)
class DefectReport:
    """Which seeded defects the candidate's tests noticed."""

    caught: Sequence[str]
    missed: Sequence[str]

    @property
    def total(self) -> int:
        return len(self.caught) + len(self.missed)

    @property
    def catch_rate(self) -> float:
        return (len(self.caught) / self.total) if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            "caught": list(self.caught),
            "missed": list(self.missed),
            "total": self.total,
            "catch_rate": self.catch_rate,
        }


def load_defects(manifest_path: str | pathlib.Path) -> list[SeededDefect]:
    """Read a ``seeded_defects.json`` manifest."""
    data = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
    rows = data.get("defects", data if isinstance(data, list) else [])
    if not rows:
        raise DefectError(f"{manifest_path} declares no defects")
    return [
        SeededDefect(
            id=str(row["id"]),
            title=str(row.get("title", row["id"])),
            file=str(row["file"]),
            find=str(row["find"]),
            replace=str(row.get("replace", "")),
            rationale=str(row.get("rationale", "")),
        )
        for row in rows
    ]


def apply_defect(root: pathlib.Path, defect: SeededDefect) -> pathlib.Path:
    """Apply one defect to a staged tree. Returns the file it changed.

    ``root`` is the analysis workspace, so ``defect.file`` is read as
    ``<package>/<module>.py`` relative to it - the same shape the changed-line
    manifest and the coverage report use.
    """
    path = root / defect.file
    if not path.is_file():
        raise DefectApplyError(f"defect {defect.id!r}: no file at {path}")
    source = path.read_text(encoding="utf-8")
    occurrences = source.count(defect.find)
    if occurrences != 1:
        raise DefectApplyError(
            f"defect {defect.id!r}: its `find` text matches {occurrences} times "
            f"in {defect.file} (expected exactly 1); the manifest has drifted "
            f"from the fixture"
        )
    path.write_text(source.replace(defect.find, defect.replace, 1), encoding="utf-8")
    return path


def _suite_fails(workspace: pathlib.Path, timeout_s: int) -> bool:
    """True iff the staged suite reports at least one failure or error."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no",
         "-p", "no:cacheprovider"],
        cwd=workspace,
        env={
            **os.environ,
            "PYTHONPATH": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True, text=True, timeout=timeout_s,
    )
    # pytest: 0 = all passed, 1 = tests failed, 2..5 = usage/collection/internal.
    if proc.returncode not in (0, 1):
        raise DefectError(
            f"pytest could not run the staged suite (exit {proc.returncode}): "
            f"{(proc.stdout or proc.stderr).strip()[:400]}"
        )
    return proc.returncode == 1


def run_defect_detection(
    target_package: str | pathlib.Path,
    test_paths: Sequence[str | pathlib.Path],
    defects: Sequence[SeededDefect],
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    workspace: Optional[str | pathlib.Path] = None,
) -> DefectReport:
    """Plant each defect in turn and record whether the suite noticed.

    Refuses to score a suite that is already red against the clean fixture: a
    catch rate measured off a broken baseline says nothing, since every defect
    would "fail" for a reason that has nothing to do with the defect.
    """
    if not defects:
        raise DefectError("no seeded defects to detect")
    target = pathlib.Path(target_package).resolve()
    tests = [pathlib.Path(p).resolve() for p in test_paths]

    owned_tmp = workspace is None
    root = pathlib.Path(
        tempfile.mkdtemp(prefix="we-defects-") if owned_tmp else workspace
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        clean = root / "clean"
        stage_analysis_workspace(clean, target, tests)
        if _suite_fails(clean, timeout_s):
            raise DefectError(
                "the candidate suite is already failing against the clean "
                "fixture; a seeded-defect catch rate off a red baseline is "
                "meaningless"
            )

        caught: list[str] = []
        missed: list[str] = []
        for defect in defects:
            planted = root / f"defect-{defect.id}"
            shutil.copytree(
                clean, planted,
                ignore=shutil.ignore_patterns(*COPY_IGNORE_PATTERNS),
            )
            apply_defect(planted, defect)
            (caught if _suite_fails(planted, timeout_s) else missed).append(defect.id)
        return DefectReport(caught=tuple(caught), missed=tuple(missed))
    finally:
        if owned_tmp:
            shutil.rmtree(root, ignore_errors=True)


def defect_signal(
    report: DefectReport,
    *,
    threshold: float,
    weight: float = 1.0,
    evaluator_id: str = DEFECT_EVALUATOR_ID,
) -> ScoredSignal:
    """Adapt a report to a ``ScoredSignal``: score is the catch rate."""
    rate = report.catch_rate
    return ScoredSignal(
        evaluator_id=evaluator_id,
        kind="assertion",
        score=rate,
        weight=weight,
        verdict="pass" if rate >= threshold else "fail",
        slot=DEFECT_SLOT,
        details=report.as_dict() | {"threshold": threshold},
        evidence=(
            f"caught {len(report.caught)}/{report.total} seeded defects "
            f"(catch rate {rate:.3f}, threshold {threshold:.2f})"
            + (f"; missed: {', '.join(report.missed)}" if report.missed else "")
        ),
    )


def error_signal(
    reason: str, *, weight: float = 1.0, evaluator_id: str = DEFECT_EVALUATOR_ID
) -> ScoredSignal:
    """Detection could not run: never a zero that reads as "caught nothing"."""
    return ScoredSignal(
        evaluator_id=evaluator_id,
        kind="assertion",
        score=0.0,
        weight=weight,
        verdict="evaluator_error",
        slot=DEFECT_SLOT,
        details={"error": reason},
        evidence=f"seeded-defect detection unavailable: {reason}",
    )
