"""Shared suite plumbing: rubric loading, judge-facing rubric text,
weighted composites. Suites are data + pure helpers; ``inspect_adapter``
turns them into Inspect AI Tasks (it alone imports ``inspect_ai``)."""
from __future__ import annotations

import pathlib

import yaml

SUITES_ROOT = pathlib.Path(__file__).resolve().parent

SUITE_DIRS = {
    "skill-review": SUITES_ROOT / "skill_review",
    "task-uplift": SUITES_ROOT / "task_uplift",
    "repo-eval": SUITES_ROOT / "repo_eval",
    "regression-replay": SUITES_ROOT / "regression_replay",
    "skill-behaviour": SUITES_ROOT / "skill_behaviour",
}


def suite_dir(suite: str) -> pathlib.Path:
    try:
        return SUITE_DIRS[suite]
    except KeyError:
        raise ValueError(f"unknown eval suite {suite!r}") from None


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
