"""Mutation kill-rate scorer (open, Apache-2.0).

A generated test suite that runs green proves nothing on its own: the same
green comes from a suite that asserts nothing. Mutation testing is the
mechanical answer. Seed defects into the code under test, run the candidate's
tests against each mutant, and count how many mutants the suite *notices*. The
kill rate is the number a QE gate can actually defend:

    kill_rate = killed / (killed + survived + never_reached)

A mutant no test even reaches is a survival, not an exclusion: the suite failed
to notice it, which is the whole question being asked. Only outcomes that say
something about the *tool* rather than the tests - a timeout, a segfault, a
mutant mutmut declined to run - leave the denominator, so a loaded CI machine
lowers confidence instead of silently moving the score. Both readings are kept
in the report (``kill_rate`` strict, ``kill_rate_executed`` over reached
mutants only) because a reviewer comparing two arms deserves to see whether a
gap came from weak assertions or from untouched code.

The mutants come from `mutmut <https://github.com/boxed/mutmut>`_ (>= 3.0),
driven as a subprocess in a scratch workspace that holds a fresh copy of the
target package plus the candidate's tests. Running out-of-tree matters: mutmut
rewrites the sources it mutates and keeps state under ``mutants/``, and the
gate must never mutate the repo it is scoring from. The machine-readable
``mutants/mutmut-cicd-stats.json`` it writes is the only output parsed - no
scraping of the progress display.

``mutmut`` is an optional dependency (``pip install 'wololo-evals[mutation]'``).
Without it :func:`run_mutation_analysis` raises :class:`MutationToolMissing`;
the grader turns that into an ``evaluator_error`` signal rather than a zero, so
a missing tool never reads as a candidate that failed to kill anything.
"""
from __future__ import annotations

import hashlib
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

__all__ = [
    "MutationError",
    "MutationToolMissing",
    "MutationReport",
    "MUTATION_EVALUATOR_ID",
    "MUTATION_SLOT",
    "run_mutation_analysis",
    "mutation_signal",
    "error_signal",
    "workspace_digest",
    "stage_analysis_workspace",
]

#: §16.1 slot the kill rate feeds: it is mechanical evidence about the run's
#: output, exactly like a ``code:`` acceptance assertion.
MUTATION_SLOT = "task_outcome"

#: Stable signal id so ``gate`` can pair the metric across two runs.
MUTATION_EVALUATOR_ID = "mutation_kill_rate"

_STATS_FILE = pathlib.Path("mutants") / "mutmut-cicd-stats.json"
_DEFAULT_TIMEOUT_S = 600


class MutationError(RuntimeError):
    """Mutation analysis could not produce a kill rate."""


class MutationToolMissing(MutationError):
    """``mutmut`` is not installed in the interpreter running the gate."""


@dataclass(frozen=True)
class MutationReport:
    """One mutation run's counts.

    ``no_tests`` is mutmut's "no test exercises this mutant": counted as a
    survival in :attr:`kill_rate` and excluded from :attr:`kill_rate_executed`.
    ``undecided`` is tool noise only.
    """

    killed: int
    survived: int
    timeout: int = 0
    no_tests: int = 0
    suspicious: int = 0
    skipped: int = 0
    segfault: int = 0
    target: str = ""
    tests: str = ""

    @property
    def never_reached(self) -> int:
        """Mutants no test executes. A survival, scored as one."""
        return self.no_tests

    @property
    def decided(self) -> int:
        """Mutants the suite got a verdict on, unreached ones included."""
        return self.killed + self.survived + self.no_tests

    @property
    def executed(self) -> int:
        """Mutants a test actually ran."""
        return self.killed + self.survived

    @property
    def undecided(self) -> int:
        """Tool noise: excluded from every rate."""
        return self.timeout + self.suspicious + self.skipped + self.segfault

    @property
    def total(self) -> int:
        return self.decided + self.undecided

    @property
    def kill_rate(self) -> float:
        """Killed over every mutant the suite was responsible for; ``0.0`` when
        there were none (an empty denominator is not a perfect score)."""
        return (self.killed / self.decided) if self.decided else 0.0

    @property
    def kill_rate_executed(self) -> float:
        """Killed over mutants a test reached: the assertion-quality reading,
        with coverage gaps factored out."""
        return (self.killed / self.executed) if self.executed else 0.0

    def as_dict(self) -> dict:
        return {
            "killed": self.killed,
            "survived": self.survived,
            "no_tests": self.no_tests,
            "timeout": self.timeout,
            "suspicious": self.suspicious,
            "skipped": self.skipped,
            "segfault": self.segfault,
            "decided": self.decided,
            "executed": self.executed,
            "undecided": self.undecided,
            "total": self.total,
            "kill_rate": self.kill_rate,
            "kill_rate_executed": self.kill_rate_executed,
            "target": self.target,
            "tests": self.tests,
        }


def workspace_digest(*roots: pathlib.Path) -> str:
    """Content hash over every ``*.py`` under ``roots``.

    Two runs whose target and test sources hash the same have the same kill
    rate by construction, so the grader caches on this instead of re-running
    mutmut once per seed.
    """
    h = hashlib.sha256()
    for root in roots:
        root = pathlib.Path(root)
        if not root.exists():
            h.update(b"\x00missing\x00")
            continue
        files = sorted(
            p for p in ([root] if root.is_file() else root.rglob("*.py"))
            if p.is_file() and "__pycache__" not in p.parts
        )
        for p in files:
            h.update(str(p.name if root.is_file() else p.relative_to(root)).encode())
            h.update(b"\x00")
            h.update(p.read_bytes())
            h.update(b"\x00")
    return h.hexdigest()


def _copy_tree(src: pathlib.Path, dest: pathlib.Path) -> None:
    shutil.copytree(
        src, dest, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "mutants"),
    )


def stage_analysis_workspace(
    workspace: pathlib.Path,
    target_package: pathlib.Path,
    test_paths: Sequence[pathlib.Path],
) -> tuple[pathlib.Path, pathlib.Path]:
    """Lay out ``workspace`` as ``<package>/`` + ``tests/`` + ``setup.cfg``."""
    pkg_dest = workspace / target_package.name
    _copy_tree(target_package, pkg_dest)

    tests_dest = workspace / "tests"
    tests_dest.mkdir(parents=True, exist_ok=True)
    staged = 0
    for path in test_paths:
        path = pathlib.Path(path)
        if path.is_dir():
            _copy_tree(path, tests_dest)
            staged += sum(1 for _ in tests_dest.rglob("test_*.py"))
        elif path.is_file():
            shutil.copy(path, tests_dest / path.name)
            staged += 1
    if not any(tests_dest.rglob("test_*.py")):
        raise MutationError(
            f"no test_*.py staged for mutation analysis from {[str(p) for p in test_paths]}"
        )

    (workspace / "setup.cfg").write_text(
        "[mutmut]\n"
        f"source_paths={target_package.name}/\n"
        "tests_dir=tests/\n",
        encoding="utf-8",
    )
    return pkg_dest, tests_dest


def _mutmut_argv() -> list[str]:
    return [sys.executable, "-m", "mutmut", "run"]


def _require_mutmut() -> None:
    try:
        import mutmut  # noqa: F401
    except ImportError as exc:
        raise MutationToolMissing(
            "mutmut is not installed; the mutation kill-rate scorer needs it "
            "(pip install 'wololo-evals[mutation]')"
        ) from exc


def run_mutation_analysis(
    target_package: str | pathlib.Path,
    test_paths: Sequence[str | pathlib.Path],
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    workspace: Optional[str | pathlib.Path] = None,
) -> MutationReport:
    """Mutate ``target_package``, run ``test_paths`` against every mutant.

    ``workspace`` is the scratch root (a fresh temp dir by default); the target
    and tests are copied into it so the analysis never writes to the sources it
    scores. Returns a :class:`MutationReport`; raises :class:`MutationError`
    when mutmut cannot produce stats at all.
    """
    _require_mutmut()
    target = pathlib.Path(target_package).resolve()
    if not target.is_dir():
        raise MutationError(f"mutation target package {target} is not a directory")
    tests = [pathlib.Path(p).resolve() for p in test_paths]

    owned_tmp = workspace is None
    root = pathlib.Path(
        tempfile.mkdtemp(prefix="we-mutation-") if owned_tmp else workspace
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        stage_analysis_workspace(root, target, tests)
        env = {
            **os.environ,
            # Keep the analysis hermetic: it must import the staged copy, never
            # an installed build of the same package name.
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MUTMUT_NO_PROGRESS": "1",
        }
        try:
            proc = subprocess.run(
                _mutmut_argv(), cwd=root, env=env, capture_output=True,
                text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise MutationError(
                f"mutation analysis of {target.name} exceeded {timeout_s}s"
            ) from exc

        stats_path = root / _STATS_FILE
        if not stats_path.is_file():
            # `mutmut run` writes the stats file itself on success; when it did
            # not, ask for the export explicitly before giving up, so a clean
            # run is never reported as a tool failure.
            subprocess.run(
                [sys.executable, "-m", "mutmut", "export-cicd-stats"],
                cwd=root, env=env, capture_output=True, text=True, timeout=timeout_s,
            )
        if not stats_path.is_file():
            raise MutationError(
                f"mutmut produced no {_STATS_FILE} (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
            )
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        return MutationReport(
            killed=int(stats.get("killed", 0)),
            survived=int(stats.get("survived", 0)),
            timeout=int(stats.get("timeout", 0)),
            no_tests=int(stats.get("no_tests", 0)),
            suspicious=int(stats.get("suspicious", 0)),
            skipped=int(stats.get("skipped", 0)),
            segfault=int(stats.get("segfault", 0)),
            target=target.name,
            tests=",".join(sorted(p.name for p in tests)),
        )
    finally:
        if owned_tmp:
            shutil.rmtree(root, ignore_errors=True)


def mutation_signal(
    report: MutationReport,
    *,
    threshold: float,
    weight: float = 1.0,
    evaluator_id: str = MUTATION_EVALUATOR_ID,
) -> ScoredSignal:
    """Adapt a report to a ``ScoredSignal``.

    ``score`` is the kill rate itself (a continuous mechanical measure, not a
    boolean), while ``verdict`` reports it against ``threshold`` so the four
    bucket classification and the hard-gate logic still see a pass/fail.
    """
    rate = report.kill_rate
    rate_executed = report.kill_rate_executed
    return ScoredSignal(
        evaluator_id=evaluator_id,
        kind="assertion",
        score=rate,
        weight=weight,
        verdict="pass" if rate >= threshold else "fail",
        slot=MUTATION_SLOT,
        details=report.as_dict() | {"threshold": threshold},
        evidence=(
            f"killed {report.killed}/{report.decided} mutants "
            f"(kill rate {rate:.3f}, threshold {threshold:.2f}; "
            f"{report.no_tests} never reached, "
            f"{rate_executed:.3f} over the {report.executed} reached"
            + (f", {report.undecided} undecided" if report.undecided else "")
            + ")"
        ),
    )


def error_signal(
    reason: str,
    *,
    weight: float = 1.0,
    evaluator_id: str = MUTATION_EVALUATOR_ID,
) -> ScoredSignal:
    """The scorer could not run. ``evaluator_error`` keeps this visibly
    distinct from a candidate that genuinely killed nothing."""
    return ScoredSignal(
        evaluator_id=evaluator_id,
        kind="assertion",
        score=0.0,
        weight=weight,
        verdict="evaluator_error",
        slot=MUTATION_SLOT,
        details={"error": reason},
        evidence=f"mutation analysis unavailable: {reason}",
    )
