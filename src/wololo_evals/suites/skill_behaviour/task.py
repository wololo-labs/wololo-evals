"""skill-behaviour suite (wave-2, SPEC M4): a paired v1-vs-v2 skill-upgrade
A/B. One Inspect sample per (trial_index, arm); baseline installs skill
version 1, treatment installs skill version 2, and each trial i on both arms
sees the identical task, checkout and image. The verdict is the paired uplift
of treatment over baseline (``stats.paired_uplift_pcts`` -> ``verdict_stats``),
exactly like task-uplift, so an upgrade must move the composite to pass.

This is the ``skill_behaviour`` gate family (``core.interfaces``
``GATE_FAMILY_WEIGHTS``). Per-trial composite parts map onto the family slots:

- activation_correct (25): RULE-BASED off the fleet-hooks/trace event stream
  (``used_skill``). A real case demands recall (the skill must fire); a
  distractor demands non-activation (the skill must stay silent). This is a
  fact the trace settles, never a judge opinion -- and is deliberately NOT the
  same thing as skill-review's judge-scored ``activation`` dimension (hence the
  distinct ``activation_correct`` name, avoiding a slot/dimension collision).
- task_outcome (40): ``acceptance-test``, identical contract to task-uplift's
  ``correctness`` -- pass/fail by RUNNING the task's acceptance command
  in-sandbox against the oracle-restored workspace, never opinion.
- trace_conformance (20): a ``trace:`` assertion (rule-based) -- how much of the
  skill's own declared workflow the observed tool-call trace actually followed.
- llm_judge_overall (15): the one judge slot, scored by the hardened judge
  (``judge.run_judge``) through the same nonce fence every other suite uses.

Score composition follows ``docs/plans/wave-contracts.md`` §16.1 (see
:func:`compose`): the weighted slot mean over the participating slots, zeroed if
a hard gate failed, and -- when the judge is disabled -- with the judge slot's
weight dropped from the denominator (``verdict=skipped``, ``judge_disabled``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

SUITE = "skill-behaviour"
# baseline = the incumbent skill (v1); treatment = the proposed upgrade (v2).
# Both arms install a skill -- this is a v1->v2 A/B, not skill-vs-nothing.
ARMS = ("baseline", "treatment")

#: §16.1 skill_behaviour family slot weights (raw points, mirroring
#: ``core.interfaces.GATE_FAMILY_WEIGHTS['skill_behaviour']``). The rubric.yaml
#: dimension names ARE the slot names, so weights there are these over 100.
SLOT_WEIGHTS: dict[str, float] = {
    "activation_correct": 25.0,
    "task_outcome": 40.0,
    "trace_conformance": 20.0,
    "llm_judge_overall": 15.0,
}
JUDGE_SLOT = "llm_judge_overall"

#: fleet-hooks stage/event markers the harness emits when the skill loads
#: (normalises to the trace ``skill_activated`` lifecycle event ->
#: ``trace:used_skill``, per core.interfaces.SKILL_ACTIVATED).
_ACTIVATION_STAGE = "skill-activated"
_ACTIVATION_EVENT = "skill_activated"
_TOOL_CALL_STAGE = "tool-call"


@dataclass(frozen=True)
class SkillBehaviourFixture:
    """A scoped task plus the behavioural expectations the trace is graded on.

    ``expects_activation`` encodes the case role: True on a genuine case (the
    skill SHOULD fire -> activation is a recall check), False on a distractor
    (the skill must NOT fire -> activation is a non-activation check).
    ``declared_workflow`` is the ordered list of trace verbs the skill's own
    SKILL.md declares, which ``trace_conformance`` scores the observed trace
    against.
    """

    fixture_id: str
    repo: str                              # clone source visible inside the sandbox
    ref: str                               # pinned ref (also the correctness oracle)
    task_prompt: str
    acceptance_command: tuple[str, ...]    # exit-0 = correct, run in-sandbox
    expects_activation: bool = True        # False = distractor: skill must stay silent
    declared_workflow: tuple[str, ...] = ()  # ordered trace verbs SKILL.md declares
    # re-materialized from ``ref`` after the harness loop and before acceptance,
    # so a treatment skill that told the harness to weaken the acceptance test
    # cannot fake task_outcome (same defense task-uplift/repo-eval apply).
    oracle_files: tuple[str, ...] = ()
    max_turns: int = 20
    wall_clock_cap_s: int = 900


def used_skill(events: Sequence[Mapping[str, object]]) -> bool:
    """Did the skill activate at all this trial? Reads the trusted, harness-
    emitted event stream (never candidate opinion) for a skill-activation
    lifecycle marker."""
    return any(
        e.get("stage") == _ACTIVATION_STAGE or e.get("event") == _ACTIVATION_EVENT
        for e in events
    )


def activation_correct_score(
    events: Sequence[Mapping[str, object]], *, expects_activation: bool
) -> float:
    """Rule-based activation slot: 1.0 when the observed activation matches what
    the case demands (skill fires on a real case, stays silent on a distractor),
    else 0.0. Recall and non-activation collapse to this single match check."""
    return 1.0 if used_skill(events) == expects_activation else 0.0


def trace_verbs(events: Sequence[Mapping[str, object]]) -> list[str]:
    """Ordered tool-call verbs from the fleet-hooks event stream."""
    verbs: list[str] = []
    for e in events:
        if e.get("stage") == _TOOL_CALL_STAGE:
            verb = e.get("verb")
            if isinstance(verb, str):
                verbs.append(verb)
    return verbs


def trace_conformance_score(
    events: Sequence[Mapping[str, object]], declared_workflow: Sequence[str]
) -> float:
    """``trace:`` assertion (rule-based): the fraction of the skill's declared
    workflow verbs that appear, in order, in the observed tool-call trace. No
    declared workflow means nothing to conform to -> 1.0 (a skill that declares
    no steps cannot be marked as having skipped any)."""
    if not declared_workflow:
        return 1.0
    observed = trace_verbs(events)
    matched = 0
    for verb in observed:
        if matched < len(declared_workflow) and verb == declared_workflow[matched]:
            matched += 1
    return matched / len(declared_workflow)


def compose(
    dimension_values: Mapping[str, float],
    *,
    judge_disabled: bool = False,
    hard_gate_failed: bool = False,
) -> float:
    """Score composition per wave-contracts §16.1.

    Case score = ``sum(slot_weight * slot_score) / sum(slot_weights)`` over the
    participating slots, then zeroed if any hard gate failed. When the judge is
    disabled its slot does not participate: its weight leaves the denominator
    (``verdict=skipped``), so the remaining rule-based slots renormalise to a
    full 0..1 range rather than being silently capped at 0.85.

    With the judge enabled and all four slots present this is identical to
    ``base.composite`` over the rubric (weights 0.25/0.40/0.20/0.15), by
    construction; the divergence is only the judge-disabled denominator.
    """
    if hard_gate_failed:
        return 0.0
    slots = dict(SLOT_WEIGHTS)
    if judge_disabled:
        slots.pop(JUDGE_SLOT)
    missing = set(slots) - set(dimension_values)
    if missing:
        raise ValueError(f"missing slot values: {sorted(missing)}")
    numerator = sum(weight * dimension_values[name] for name, weight in slots.items())
    denominator = sum(slots.values())
    return numerator / denominator


def judge_content(diff: str, untracked: str, events_summary: str) -> str:
    """The attacker-controllable content handed to the hardened judge for the
    llm_judge_overall slot (nonce-fenced by judge.py). Same shape as
    task-uplift's judge_content: the harness's diff, new files, and a transcript
    summary the treatment skill could have influenced."""
    return (
        "## git diff\n" + diff +
        "\n\n## new files (git status --porcelain)\n" + untracked +
        "\n\n## transcript summary\n" + events_summary
    )
