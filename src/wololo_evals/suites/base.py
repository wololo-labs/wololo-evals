"""Shared suite plumbing: the suite registry, rubric loading, judge-facing
rubric text, weighted composites. Suites are data + pure helpers;
``inspect_adapter`` turns them into Inspect AI Tasks (it alone imports
``inspect_ai``).

The registry is open. A suite that lives outside this package registers itself
through any of three routes, resolved in ascending precedence so a local
override always wins:

1. the built-in suites shipped here;
2. the ``wololo_evals.suites`` entry-point group - a distribution exposing
   ``my-suite = my_pkg.suites:DIRECTORY`` (a ``pathlib.Path``, a ``str``, or a
   zero-argument callable returning one) is picked up on import;
3. ``WOLOLO_EVALS_SUITE_DIRS``, a ``:``-separated list of ``<id>=<path>`` pairs,
   for a run-local suite with no packaging at all;
4. :func:`register_suite`, for a caller holding the directory in hand.

Every route lands in the same dict, so ``suite_dir``/``load_rubric`` and the
content-hash versioning in ``wololo_evals.versioning`` treat an external suite
exactly like a built-in one.
"""
from __future__ import annotations

import os
import pathlib

import yaml

SUITES_ROOT = pathlib.Path(__file__).resolve().parent

#: Environment override: ``id=path`` pairs separated by ``os.pathsep``.
SUITE_DIRS_ENV = "WOLOLO_EVALS_SUITE_DIRS"

#: ``importlib.metadata`` group third-party distributions publish suites under.
SUITE_ENTRY_POINT_GROUP = "wololo_evals.suites"

#: Suites shipped in this package. Kept separate from :data:`SUITE_DIRS` so a
#: caller can tell a built-in from a registration, and so the discovery pass is
#: idempotent.
BUILTIN_SUITE_DIRS = {
    "skill-review": SUITES_ROOT / "skill_review",
    "task-uplift": SUITES_ROOT / "task_uplift",
    "repo-eval": SUITES_ROOT / "repo_eval",
    "regression-replay": SUITES_ROOT / "regression_replay",
    "skill-behaviour": SUITES_ROOT / "skill_behaviour",
    "qe-review": SUITES_ROOT / "qe_review",
}

#: The live registry: built-ins plus everything discovered or registered.
SUITE_DIRS: dict[str, pathlib.Path] = dict(BUILTIN_SUITE_DIRS)


class SuiteRegistrationError(ValueError):
    """A suite was registered with a directory that does not exist, or an
    entry point that could not be resolved. Fail loudly: a suite silently
    missing from the registry reads as a typo in the caller's suite id."""


def register_suite(suite: str, directory: str | pathlib.Path) -> pathlib.Path:
    """Register (or override) ``suite`` at ``directory``. Returns the path.

    The directory must exist and hold a ``rubric.yaml`` - the two things every
    consumer of the registry immediately assumes.
    """
    path = pathlib.Path(directory).expanduser().resolve()
    if not path.is_dir():
        raise SuiteRegistrationError(
            f"suite {suite!r}: {path} is not a directory"
        )
    if not (path / "rubric.yaml").is_file():
        raise SuiteRegistrationError(
            f"suite {suite!r}: {path} has no rubric.yaml"
        )
    SUITE_DIRS[suite] = path
    return path


def _entry_point_suites() -> list[tuple[str, object]]:
    from importlib.metadata import entry_points

    try:
        return [(ep.name, ep) for ep in entry_points(group=SUITE_ENTRY_POINT_GROUP)]
    except Exception:  # noqa: BLE001 - a broken third-party dist must not break us
        return []


def discover_suites() -> dict[str, pathlib.Path]:
    """Refresh :data:`SUITE_DIRS` from the entry-point group and the
    environment override. Idempotent, and safe to call more than once."""
    SUITE_DIRS.clear()
    SUITE_DIRS.update(BUILTIN_SUITE_DIRS)

    for name, ep in _entry_point_suites():
        try:
            value = ep.load()
        except Exception as exc:  # noqa: BLE001
            raise SuiteRegistrationError(
                f"suite entry point {name!r} failed to load: {exc}"
            ) from exc
        register_suite(name, value() if callable(value) else value)

    raw = os.environ.get(SUITE_DIRS_ENV, "")
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SuiteRegistrationError(
                f"{SUITE_DIRS_ENV} entry {item!r} is not <suite-id>=<path>"
            )
        name, _, path = item.partition("=")
        register_suite(name.strip(), path.strip())
    return dict(SUITE_DIRS)


def suite_dir(suite: str) -> pathlib.Path:
    if suite not in SUITE_DIRS and (
        os.environ.get(SUITE_DIRS_ENV) or _entry_point_suites()
    ):
        discover_suites()  # a suite may have been published since import
    try:
        return SUITE_DIRS[suite]
    except KeyError:
        raise ValueError(
            f"unknown eval suite {suite!r} (have: {sorted(SUITE_DIRS)})"
        ) from None


discover_suites()


def rubric_path(suite: str) -> pathlib.Path:
    return suite_dir(suite) / "rubric.yaml"


def load_rubric(suite: str) -> dict:
    rubric = yaml.safe_load(rubric_path(suite).read_text())
    # The rubric's declared suite MUST equal the registry key it was loaded
    # under: a drift here would silently pin/judge/version one suite's rubric
    # under another's name. Nothing else guards this (SPEC S8/S9).
    if rubric.get("suite") != suite:
        raise ValueError(
            f"rubric.yaml suite {rubric.get('suite')!r} != registry key {suite!r}"
        )
    weights = [d["weight"] for d in rubric["dimensions"].values()]
    total = sum(weights)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"{suite} dimension weights sum to {total}, not 1.0")
    return rubric


def judged_dimensions(rubric: dict) -> list[str]:
    """Dimensions the LLM judge scores (skill-review: all; task-uplift: only
    scorer=judge ones — correctness and turns are rule-based, never opinion)."""
    return [
        name
        for name, d in rubric["dimensions"].items()
        if d.get("scorer", "judge") == "judge"
    ]


def rubric_judge_text(rubric: dict, dimensions: list[str] | None = None) -> str:
    """The system-message rubric body (SPEC S8: rubric instructions come ONLY
    from rubric.yaml, which the submitter cannot touch)."""
    names = dimensions if dimensions is not None else judged_dimensions(rubric)
    lines = [f"Eval suite: {rubric['suite']}. Score each dimension 0.0..1.0:"]
    for name in names:
        criterion = " ".join(rubric["dimensions"][name]["criterion"].split())
        lines.append(f"- {name}: {criterion}")
    return "\n".join(lines)


def composite(rubric: dict, dimension_values: dict[str, float]) -> float:
    """Weighted sum over ALL rubric dimensions; a missing dimension is an
    error, never silently zero-weighted."""
    missing = set(rubric["dimensions"]) - set(dimension_values)
    if missing:
        raise ValueError(f"missing dimension values: {sorted(missing)}")
    return sum(
        d["weight"] * dimension_values[name]
        for name, d in rubric["dimensions"].items()
    )


def turns_efficiency_score(turns: int, max_turns: int) -> float:
    """Rule-based turns dimension: 1.0 at a single turn, linear down to 0.0
    at the turn cap. Identical instrumentation both arms (SPEC S5.2)."""
    if max_turns < 1:
        raise ValueError("max_turns must be >= 1")
    if turns <= 1:
        return 1.0
    return max(0.0, 1.0 - (turns - 1) / max_turns)
