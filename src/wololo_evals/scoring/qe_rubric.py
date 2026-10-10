"""QE rubric scorer: grade a generated test suite mechanically (open, Apache-2.0).

Implements the four ``qe-review`` rubric dimensions (see
``suites/qe_review/rubric.yaml``, whose sha256 is the pinned rubric version).
Every one of them is computed by reading or running the candidate's tests, so
the whole rubric is reproducible offline at zero spend:

=============================  ==============================================
``assertion_strength``         static: share of test functions that assert on
                               a value the target package produced
``flake_resistance``           dynamic: share of tests whose outcome is
                               identical across repeated runs under different
                               ``PYTHONHASHSEED`` values
``changed_line_coverage``      dynamic: share of the declared changed lines
                               the suite executes (``coverage``)
``no_tautological_assertions`` static: share of assertions that are not
                               vacuous
=============================  ==============================================

Each dimension is emitted as its own standing ``ScoredSignal`` (``slot=None``,
so it is visible and comparable in a gate report without being double-counted)
plus one weighted composite that feeds the ``task_outcome`` slot.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import shutil
import subprocess
import sys
import warnings
import tempfile
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from wololo_evals.core.interfaces import ScoredSignal
from wololo_evals.scoring.mutation import stage_analysis_workspace

__all__ = [
    "QE_SUITE",
    "QE_RUBRIC_EVALUATOR_ID",
    "QE_RUBRIC_SLOT",
    "QeRubricError",
    "QeRubricReport",
    "score_qe_rubric",
    "qe_rubric_signals",
    "assertion_strength",
    "no_tautological_assertions",
    "flake_resistance",
    "changed_line_coverage",
    "error_signals",
]

#: Registry key of the rubric these dimensions come from.
QE_SUITE = "qe-review"

#: §16.1 slot the weighted composite feeds.
QE_RUBRIC_SLOT = "task_outcome"

QE_RUBRIC_EVALUATOR_ID = "qe_rubric"

_REPEATS = 3
_DEFAULT_TIMEOUT_S = 300


class QeRubricError(RuntimeError):
    """The rubric could not be scored (no tests staged, pytest unusable)."""


@dataclass(frozen=True)
class QeRubricReport:
    """Per-dimension scores plus the evidence behind each."""

    dimensions: Mapping[str, float]
    details: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "dimensions": dict(self.dimensions),
            "details": {k: dict(v) for k, v in self.details.items()},
        }


# ---------------------------------------------------------------------------
# Static dimensions: read the tests' syntax trees
# ---------------------------------------------------------------------------
def _test_files(tests_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in tests_dir.rglob("test_*.py") if p.is_file())


def _target_names(tree: ast.Module, target_package: str) -> set[str]:
    """Names this test module bound from the package under test.

    Both ``from quoting.pricing import quote_pence`` and ``import quoting``
    count, so an assertion is recognised as touching the target whether it
    calls the imported function directly or reaches it through the module.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == target_package or module.startswith(f"{target_package}."):
                names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == target_package or a.name.startswith(f"{target_package}."):
                    names.add(a.asname or a.name.split(".")[0])
    return names


def _references_target(node: ast.AST, target_names: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in target_names:
            return True
    return False


def _assigned_names(target: ast.expr) -> list[str]:
    """Every plain name an assignment target binds, tuple unpacking included."""
    names: list[str] = []
    for sub in ast.walk(target):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Load)):
            names.append(sub.id)
    return names


def _tainted_names(tree: ast.Module, target_package: str) -> set[str]:
    """Names that carry a value the code under test produced.

    Direct imports are only the start: a test suite almost always routes its
    calls through a module-level helper (``def _quote(**payload): return
    handle_quote(...)``) and then asserts on a local it unpacked from that
    helper. Scoring only the direct-import names would grade import style, not
    whether the assertion can see a regression, so the taint propagates two
    ways:

    * a module-level function whose body reaches a tainted name is itself
      tainted (iterated to a fixed point, so helpers chained through helpers
      are covered);
    * inside a test, a local bound from a tainted expression is tainted.

    Pytest fixtures are deliberately included: a fixture returning a value the
    target produced taints its parameter name in every test that requests it.
    """
    tainted = _target_names(tree, target_package)
    module_functions = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    changed = True
    while changed:
        changed = False
        for fn in module_functions:
            if fn.name in tainted:
                continue
            if any(_references_target(stmt, tainted) for stmt in fn.body):
                tainted.add(fn.name)
                changed = True
                # A fixture taints the parameter every test names after it.
                if any(
                    isinstance(d, (ast.Attribute, ast.Call, ast.Name))
                    for d in fn.decorator_list
                ):
                    pass
    return tainted


def _local_taint(fn: ast.AST, tainted: set[str]) -> set[str]:
    """Extend ``tainted`` with the locals this function binds from it."""
    local = set(tainted)
    # Fixture-style parameters: a test asking for a tainted module-level
    # function by name receives what that function produced.
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in fn.args.args:
            if arg.arg in tainted:
                local.add(arg.arg)
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None and _references_target(value, local):
                for target in targets:
                    local.update(_assigned_names(target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            # `with pytest.raises(X) as excinfo:` binds a value the *body*
            # produced, not the context expression, so a name is tainted when
            # either side reaches the target. Without the body check, every
            # error-path assertion (`assert excinfo.value.field == ...`) reads
            # as an assertion about nothing.
            body_touches_target = any(
                _references_target(stmt, local) for stmt in node.body
            )
            for item in node.items:
                if item.optional_vars is None:
                    continue
                if body_touches_target or _references_target(item.context_expr, local):
                    local.update(_assigned_names(item.optional_vars))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if _references_target(node.iter, local):
                local.update(_assigned_names(node.target))
    return local


def _is_tautological(test: ast.expr, tainted: set[str]) -> bool:
    """A vacuous assertion: one that cannot fail whatever the target does.

    Three shapes qualify: a constant-true expression, a comparison of a term
    against itself or of two constants, and an expression that never touches a
    value the code under test produced (see :func:`_tainted_names`).
    """
    if isinstance(test, ast.Constant):
        return bool(test.value) or test.value is None
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, right = test.left, test.comparators[0]
        if isinstance(test.ops[0], (ast.Eq, ast.Is)) and ast.dump(left) == ast.dump(right):
            return True
        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
            return True
    return not _references_target(test, tainted)


def _is_discriminating(test: ast.expr, tainted: set[str]) -> bool:
    """True iff the assertion pins a target-derived value to an expectation.

    ``assert quote == 515`` discriminates; ``assert quote`` does not - it holds
    for every non-zero price the code could ever return, so it survives most of
    the mutants that matter. Comparisons, membership tests and identity tests
    all count; a bare truthiness check does not, and neither does a comparison
    that is vacuous anyway (``assert price == price`` is a Compare, and pins
    nothing).
    """
    if _is_tautological(test, tainted):
        return False
    if not _references_target(test, tainted):
        return False
    for node in ast.walk(test):
        if isinstance(node, ast.Compare):
            return True
    return False


def _test_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    ]


def _raises_blocks(fn: ast.AST, target_names: set[str]) -> int:
    """``with pytest.raises(...)`` bodies that call into the target: an
    error-path expectation is a strong assertion even with no ``assert``."""
    count = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "raises"
                and any(_references_target(stmt, target_names) for stmt in node.body)
            ):
                count += 1
    return count


def assertion_strength(
    tests_dir: str | pathlib.Path, target_package: str
) -> tuple[float, dict]:
    """Share of test functions making at least one discriminating expectation.

    A test that only calls the code and never inspects what came back passes
    for as long as the call does not raise, which is exactly the shape a
    regression walks through; so does a test that asserts a returned value is
    merely truthy. Both are scored as weak here (see
    :func:`_is_discriminating`), and a ``pytest.raises`` on the target counts as
    strong: an error-path expectation is a real one.
    """
    tests_dir = pathlib.Path(tests_dir)
    total = 0
    strong = 0
    weak_names: list[str] = []
    for path in _test_files(tests_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_taint = _tainted_names(tree, target_package)
        for fn in _test_functions(tree):
            total += 1
            names = _local_taint(fn, module_taint)
            asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
            has_strong = any(_is_discriminating(a.test, names) for a in asserts)
            if has_strong or _raises_blocks(fn, names):
                strong += 1
            else:
                weak_names.append(f"{path.name}::{fn.name}")
    score = (strong / total) if total else 0.0
    return score, {
        "test_functions": total,
        "with_target_assertion": strong,
        "weak": sorted(weak_names),
    }


def no_tautological_assertions(
    tests_dir: str | pathlib.Path, target_package: str
) -> tuple[float, dict]:
    """Share of assertions that can actually fail."""
    tests_dir = pathlib.Path(tests_dir)
    total = 0
    tautological: list[str] = []
    for path in _test_files(tests_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_taint = _tainted_names(tree, target_package)
        for fn in _test_functions(tree):
            names = _local_taint(fn, module_taint)
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assert):
                    continue
                total += 1
                if _is_tautological(node.test, names):
                    tautological.append(f"{path.name}:{node.lineno}")
    score = ((total - len(tautological)) / total) if total else 0.0
    return score, {
        "assertions": total,
        "tautological": sorted(tautological),
    }


# ---------------------------------------------------------------------------
# Dynamic dimensions: run the tests
# ---------------------------------------------------------------------------
def _pytest_outcomes(
    workspace: pathlib.Path, *, env: Mapping[str, str], timeout_s: int
) -> dict[str, str]:
    """Run the staged suite once and return ``{test id: outcome}``.

    Uses pytest's own JSON-free reporting: ``-q --tb=no -rA`` short summary
    lines are stable across versions and need no plugin, keeping the fixture
    free of extra runtime dependencies.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests",
            "-q", "--tb=no", "-rA", "-p", "no:cacheprovider",
        ],
        cwd=workspace, env=dict(env), capture_output=True, text=True, timeout=timeout_s,
    )
    outcomes: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] in ("PASSED", "FAILED", "ERROR", "XFAIL", "XPASS", "SKIPPED"):
            outcomes[parts[1]] = parts[0]
    if not outcomes:
        raise QeRubricError(
            "pytest reported no test outcomes: "
            f"{(proc.stdout or proc.stderr).strip()[:400]}"
        )
    return outcomes


def flake_resistance(
    workspace: pathlib.Path, *, repeats: int = _REPEATS, timeout_s: int = _DEFAULT_TIMEOUT_S
) -> tuple[float, dict]:
    """Share of tests whose outcome is identical across ``repeats`` runs.

    Each run gets a different ``PYTHONHASHSEED``, which is what shakes out the
    common flake source in generated suites: an expectation that depends on
    dict or set iteration order.
    """
    base_env = {
        **os.environ,
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    runs: list[dict[str, str]] = []
    for i in range(repeats):
        runs.append(
            _pytest_outcomes(
                workspace,
                env={**base_env, "PYTHONHASHSEED": str(1 + i * 7919)},
                timeout_s=timeout_s,
            )
        )
    ids = set().union(*(set(r) for r in runs))
    unstable = sorted(
        tid for tid in ids
        if len({r.get(tid, "MISSING") for r in runs}) > 1
    )
    score = ((len(ids) - len(unstable)) / len(ids)) if ids else 0.0
    return score, {
        "repeats": repeats,
        "tests": len(ids),
        "unstable": unstable,
        "outcomes": runs[0],
    }


def changed_line_coverage(
    workspace: pathlib.Path,
    changed_lines: Mapping[str, Sequence[int]],
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> tuple[float, dict]:
    """Share of the declared changed lines the suite executes.

    ``changed_lines`` maps a path relative to the target package's parent
    (``quoting/pricing.py``) to the line numbers under review. Lines coverage
    reports as non-executable (blank, comment, ``def`` continuation) are
    dropped from the denominator rather than counted as misses.
    """
    if not changed_lines:
        raise QeRubricError("changed_line_coverage needs a non-empty changed-line map")
    env = {
        **os.environ,
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "COVERAGE_FILE": str(workspace / ".coverage"),
    }
    targets = sorted({p.split("/")[0] for p in changed_lines})
    subprocess.run(
        [
            sys.executable, "-m", "coverage", "run",
            f"--source={','.join(targets)}", "-m", "pytest", "tests",
            "-q", "--tb=no", "-p", "no:cacheprovider",
        ],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=timeout_s,
    )
    report_path = workspace / "coverage.json"
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(report_path)],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=timeout_s,
    )
    if not report_path.is_file():
        raise QeRubricError(
            f"coverage produced no report: {(proc.stderr or proc.stdout).strip()[:400]}"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report.get("files", {})

    covered = 0
    considered = 0
    missed: list[str] = []
    out_of_range: list[str] = []
    for rel, lines in changed_lines.items():
        entry = files.get(rel) or files.get(f"./{rel}")
        if entry is None:
            raise QeRubricError(
                f"changed-line file {rel!r} not in the coverage report "
                f"(have: {sorted(files)})"
            )
        executed = set(entry.get("executed_lines", []))
        missing = set(entry.get("missing_lines", []))
        executable = executed | missing
        source = workspace / rel
        n_lines = len(source.read_text(encoding="utf-8").splitlines()) if source.is_file() else None
        for line in lines:
            if n_lines is not None and not 1 <= line <= n_lines:
                # A line that does not exist behaves like a blank line below and
                # is dropped silently, so a manifest that has drifted from its
                # fixture scores exactly like a correct one. Say so instead.
                out_of_range.append(f"{rel}:{line}")
                continue
            if line not in executable:
                continue  # not an executable line: never a coverage miss
            considered += 1
            if line in executed:
                covered += 1
            else:
                missed.append(f"{rel}:{line}")
    if not considered:
        raise QeRubricError(
            f"no declared changed line is executable in {sorted(changed_lines)}"
        )
    if out_of_range:
        warnings.warn(
            f"changed-line manifest names lines past the end of the file or before "
            f"line 1: {sorted(out_of_range)}. They were ignored; regenerate the "
            f"manifest, which has drifted from its fixture.",
            UserWarning,
            stacklevel=2,
        )
    return covered / considered, {
        "covered": covered,
        "considered": considered,
        "missed": sorted(missed),
        "out_of_range": sorted(out_of_range),
    }


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
def score_qe_rubric(
    target_package: str | pathlib.Path,
    test_paths: Sequence[str | pathlib.Path],
    changed_lines: Mapping[str, Sequence[int]],
    *,
    repeats: int = _REPEATS,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    workspace: Optional[str | pathlib.Path] = None,
) -> QeRubricReport:
    """Score all four ``qe-review`` dimensions for one candidate suite.

    Stages the target package and the candidate's tests into a scratch
    workspace (the same layout the mutation scorer uses) so nothing here runs
    against, or writes to, the sources being scored.
    """
    target = pathlib.Path(target_package).resolve()
    if not target.is_dir():
        raise QeRubricError(f"target package {target} is not a directory")

    owned_tmp = workspace is None
    root = pathlib.Path(
        tempfile.mkdtemp(prefix="we-qe-rubric-") if owned_tmp else workspace
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        _, tests_dir = stage_analysis_workspace(
            root, target, [pathlib.Path(p) for p in test_paths]
        )
        strength, strength_details = assertion_strength(tests_dir, target.name)
        tautology, tautology_details = no_tautological_assertions(tests_dir, target.name)
        flake, flake_details = flake_resistance(root, repeats=repeats, timeout_s=timeout_s)
        coverage, coverage_details = changed_line_coverage(
            root, changed_lines, timeout_s=timeout_s
        )
        return QeRubricReport(
            dimensions={
                "assertion_strength": strength,
                "flake_resistance": flake,
                "changed_line_coverage": coverage,
                "no_tautological_assertions": tautology,
            },
            details={
                "assertion_strength": strength_details,
                "flake_resistance": flake_details,
                "changed_line_coverage": coverage_details,
                "no_tautological_assertions": tautology_details,
            },
        )
    finally:
        if owned_tmp:
            shutil.rmtree(root, ignore_errors=True)


def qe_rubric_signals(
    report: QeRubricReport,
    rubric: Mapping[str, object],
    *,
    threshold: float,
    weight: float = 1.0,
    rubric_version: str = "",
) -> list[ScoredSignal]:
    """One standing signal per dimension plus the weighted composite.

    The per-dimension signals carry ``slot=None``: they exist so an operator
    (and ``gate``'s per-signal delta table) can see *which* dimension moved,
    without being counted twice against the composite that feeds the family.
    """
    from wololo_evals.suites.base import composite

    dims = dict(report.dimensions)
    signals = [
        ScoredSignal(
            evaluator_id=f"qe_{name}",
            kind="assertion",
            score=value,
            weight=weight,
            verdict="pass" if value >= threshold else "fail",
            slot=None,
            details=dict(report.details.get(name, {})) | {"dimension": name},
            evidence=f"{name} = {value:.3f}",
        )
        for name, value in sorted(dims.items())
    ]
    overall = composite(rubric, dims)
    signals.append(
        ScoredSignal(
            evaluator_id=QE_RUBRIC_EVALUATOR_ID,
            kind="assertion",
            score=overall,
            weight=weight,
            verdict="pass" if overall >= threshold else "fail",
            slot=QE_RUBRIC_SLOT,
            details=report.as_dict() | {
                "composite": overall,
                "threshold": threshold,
                "rubric_version": rubric_version,
            },
            evidence=(
                f"qe-review composite {overall:.3f} (threshold {threshold:.2f}) over "
                + ", ".join(f"{k}={v:.2f}" for k, v in sorted(dims.items()))
            ),
        )
    )
    return signals


def error_signals(reason: str, *, weight: float = 1.0) -> list[ScoredSignal]:
    """The rubric could not be scored: one ``evaluator_error`` composite,
    visibly distinct from a suite that genuinely scored zero."""
    return [
        ScoredSignal(
            evaluator_id=QE_RUBRIC_EVALUATOR_ID,
            kind="assertion",
            score=0.0,
            weight=weight,
            verdict="evaluator_error",
            slot=QE_RUBRIC_SLOT,
            details={"error": reason},
            evidence=f"qe-review rubric unavailable: {reason}",
        )
    ]
