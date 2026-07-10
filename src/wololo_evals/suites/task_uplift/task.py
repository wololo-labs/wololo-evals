"""task-uplift suite (SPEC S5.2): baseline-vs-with-skill uplift over N>=5
paired trials. One Inspect sample per (task_fixture, trial_index, arm);
baseline and treatment trial i see the identical task, checkout and image.

Per-trial composite parts:
- correctness (0.5): acceptance test RUN in-sandbox, pass/fail, never opinion
- turns_efficiency (0.2): rule-based off the fleet-hooks event stream
- approach_quality (0.3): hardened LLM judge over diff + transcript
"""
from __future__ import annotations

from dataclasses import dataclass

SUITE = "task-uplift"
ARMS = ("baseline", "treatment")


@dataclass(frozen=True)
class TaskFixture:
    """A real, scoped coding task the harness is measured on."""

    fixture_id: str
    repo: str                      # clone source visible inside the sandbox
    ref: str                       # pinned ref
    task_prompt: str
    acceptance_command: tuple[str, ...]   # exit-0 = correct, run in-sandbox
    max_turns: int = 20
    wall_clock_cap_s: int = 900


def judge_content(diff: str, untracked: str, events_summary: str) -> str:
    """The attacker-controllable content handed to the hardened judge for
    the approach_quality dimension (nonce-fenced by judge.py)."""
    return (
        "## git diff\n" + diff +
        "\n\n## new files (git status --porcelain)\n" + untracked +
        "\n\n## transcript summary\n" + events_summary
    )
