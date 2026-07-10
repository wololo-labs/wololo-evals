"""repo-eval suite (SPEC S5.3): generated from real seeded commit history,
not hand-written. One Inspect sample per (fixture, trial_index); single-arm
(no candidate skill installed) — repo-eval scores a harness/model config
against real work the repo's own history already did.

Per-trial composite parts:
- correctness (0.50): the repo's real seeded test suite RUN in-sandbox, pass/fail
- churn_penalty (0.25): rule-based off diff line counts vs the historical fix
- abstraction_adherence (0.30->0.25): hardened LLM judge over diff + surrounding

``generate.py`` walks a repo's commit log and turns a qualifying commit into a
:class:`RepoEvalFixture`; the harness is then measured on re-deriving that unit
of work from the commit message alone (the diff is redacted by the checkout).
"""
from __future__ import annotations

from dataclasses import dataclass

SUITE = "repo-eval"
ARM = "single"


@dataclass(frozen=True)
class RepoEvalFixture:
    """A real, isolable unit of work lifted from a repo's seeded history."""

    fixture_id: str
    repo: str                      # clone source visible inside the sandbox
    parent_ref: str                # checkout BEFORE the change (the diff is redacted)
    commit_ref: str                # the historical commit that did the work
    task_prompt: str               # the commit's own message, the only instruction
    acceptance_command: tuple[str, ...]   # exit-0 = correct, run in-sandbox
    historical_lines_changed: int  # added+removed lines in the historical diff
    # the commit's own test paths — materialized from commit_ref onto the
    # workspace BEFORE acceptance so correctness is scored against the fix's own
    # oracle, never whatever test happened to exist at the redacted parent (a
    # new test file is absent there; a modified one holds stale pre-fix content)
    oracle_files: tuple[str, ...] = ()
    max_turns: int = 20
    wall_clock_cap_s: int = 900


def diff_lines_changed(diff: str) -> int:
    """Added + removed lines in a unified git diff, excluding the +++/--- file
    headers and @@ hunk markers, so it is comparable to git's own numstat."""
    n = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def churn_penalty_score(historical_lines: int, candidate_lines: int) -> float:
    """SPEC S5.3 churn_penalty: a diff smaller than or equal to the historical
    fix scores 1.0; only a larger-than-historical diff is penalised
    proportionally. A candidate that changed nothing did no work (0.0)."""
    if candidate_lines <= 0:
        return 0.0
    if historical_lines <= 0:
        # no historical reference to scale against; do not invent a penalty.
        return 1.0
    return min(1.0, historical_lines / candidate_lines)


def judge_content(diff: str, untracked: str) -> str:
    """Attacker-uncontrollable here (the repo is ours), but still nonce-fenced
    by judge.py for a uniform path. The unified diff carries the surrounding
    context lines the abstraction_adherence judge weighs reuse against."""
    return (
        "## candidate git diff (with surrounding context)\n" + diff +
        "\n\n## new files (git status --porcelain)\n" + untracked
    )
