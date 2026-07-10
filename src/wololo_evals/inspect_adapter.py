"""The ONE module that imports ``inspect_ai`` (SPEC S6, hard boundary).

Inspect AI owns execution, sandboxing, transcript logging and the ``eval()``
runner; this adapter turns wololo-evals suite definitions into Inspect
``Task``s and normalizes results for the persistence layer. Every suite
module imports this adapter, never ``inspect_ai`` itself.

Entry points are synchronous (Inspect runs its own event loop); the async
worker calls them via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from inspect_ai import Task, eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import sandbox

from . import judge as judge_mod
from .harness_solver import (
    HarnessTrialConfig,
    TrialErrored,
    run_acceptance_test,
    run_harness_trial,
)
from .harnesses import HarnessRunner
from .suites import base
from .suites.skill_review import task as skill_review_task
from .suites.task_uplift import task as task_uplift_task

# mockllm satisfies Inspect's model requirement for suites whose scoring
# never touches Inspect's model loop (our judge goes through its own
# injected backend so judge calls are attributable + tenant-configurable).
_NO_MODEL = "mockllm/model"


@dataclass
class SuiteOutcome:
    """Normalized result the worker turns into trials + score rows."""

    status: str                     # "completed" | "errored"
    error: str = ""
    # per logical trial: {trial_index, arm, dimension_values, composite,
    #                     judge_samples, injection_suspected, prescan, extras}
    trials: list[dict] = field(default_factory=list)
    eval_log_bytes: bytes = b""


def _read_log(log) -> bytes:
    path = str(log.location).replace("file://", "")
    p = pathlib.Path(path)
    return p.read_bytes() if p.exists() else b""


# ---------------------------------------------------------------------------
# skill-review: static, no sandbox, single sample
# ---------------------------------------------------------------------------
@solver
def _noop_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        return state  # the sample already contains everything to grade

    return solve


def run_skill_review(
    candidate_payload: dict,
    call_model: Callable[[str, str], str],
    *,
    k: int = 5,
) -> SuiteOutcome:
    rubric = base.load_rubric("skill-review")
    rubric_text = base.rubric_judge_text(rubric)
    dimensions = list(rubric["dimensions"])
    content = skill_review_task.render_candidate(candidate_payload)

    @scorer(metrics=[accuracy()])
    def _judge_scorer():
        async def score(state: TaskState, target: Target) -> Score:
            result = await asyncio.to_thread(
                judge_mod.run_judge,
                call_model,
                rubric_text,
                state.input_text,
                dimensions,
                k,
            )
            comp = base.composite(rubric, result.dimension_medians)
            return Score(
                value=comp,
                metadata={
                    "dimension_medians": result.dimension_medians,
                    "samples": result.samples,
                    "injection_suspected": result.injection_suspected,
                    "prescan": result.prescan,
                },
            )

        return score

    task = Task(
        dataset=[Sample(input=content, id="skill-review-0")],
        solver=_noop_solver(),
        scorer=_judge_scorer(),
    )
    with tempfile.TemporaryDirectory() as log_dir:
        try:
            log = inspect_eval(task, model=_NO_MODEL, log_dir=log_dir, display="none")[0]
        except (judge_mod.JudgeIntegrityError, judge_mod.JudgeParseError) as exc:
            return SuiteOutcome(status="errored", error=f"judge: {exc}")
        log_bytes = _read_log(log)
        if log.status != "success":
            err = str(log.error or "inspect eval failed")
            # fail closed: a judge integrity/parse failure surfaced through
            # Inspect's error channel is an errored run, never a default pass
            return SuiteOutcome(status="errored", error=err, eval_log_bytes=log_bytes)
        sample_score = next(iter(log.samples[0].scores.values()))
        meta = sample_score.metadata
        if meta["injection_suspected"]:
            return SuiteOutcome(
                status="errored",
                error="judge-integrity",
                trials=[_skill_trial(sample_score, meta)],
                eval_log_bytes=log_bytes,
            )
        return SuiteOutcome(
            status="completed",
            trials=[_skill_trial(sample_score, meta)],
            eval_log_bytes=log_bytes,
        )


def _skill_trial(sample_score, meta) -> dict:
    return {
        "trial_index": 0,
        "arm": "single",
        "dimension_values": meta["dimension_medians"],
        "composite": float(sample_score.as_float()),
        "judge_samples": meta["samples"],
        "injection_suspected": meta["injection_suspected"],
        "prescan": meta["prescan"],
    }


# ---------------------------------------------------------------------------
# task-uplift: harness-under-eval inside the Inspect sandbox
# ---------------------------------------------------------------------------
def _sandbox_exec():
    async def exec_fn(cmd, env, timeout):
        result = await sandbox().exec(
            list(cmd), env=dict(env or {}), timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr

    return exec_fn


def run_task_uplift(
    fixture: task_uplift_task.TaskFixture,
    runner: HarnessRunner,
    n_trials: int,
    skill_slug: str,
    skill_files: dict,
    call_model: Callable[[str, str], str],
    *,
    k: int = 5,
    sandbox_type: str = "docker",
    arms: Sequence[str] = task_uplift_task.ARMS,
    exec_factory: Optional[Callable[[], "object"]] = None,
) -> SuiteOutcome:
    """One sample per (trial_index, arm); each sample runs the REAL harness
    CLI inside its own Inspect sandbox via the injected exec seam.

    ``sandbox_type="docker"`` is the SPEC posture (network-disabled cell per
    trial). ``"local"`` exists for daemonless environments and is a labelled
    degraded mode: no container isolation, smoke-grade only.

    ``exec_factory`` injects the :data:`SandboxExec` seam directly, bypassing
    the Inspect sandbox entirely. It exists so the harness-under-eval bridge
    (clone -> skill install -> harness loop -> diff/transcript capture ->
    acceptance test -> judge -> composite) is exercisable end to end with a
    fake executor on a machine with no docker daemon; production leaves it None
    and the real ``sandbox().exec`` drives a pinned per-harness container.
    """
    rubric = base.load_rubric("task-uplift")
    judged = base.judged_dimensions(rubric)
    rubric_text = base.rubric_judge_text(rubric)
    # No Inspect sandbox is provisioned when the exec seam is injected.
    make_exec = exec_factory or _sandbox_exec
    effective_sandbox = None if exec_factory is not None else sandbox_type

    samples = [
        Sample(
            input=fixture.task_prompt,
            id=f"{arm}-{i}",
            metadata={"arm": arm, "trial_index": i},
        )
        for i in range(n_trials)
        for arm in arms
    ]

    @solver
    def _harness_bridge():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            arm = state.metadata["arm"]
            cfg = HarnessTrialConfig(
                runner=runner,
                task_prompt=fixture.task_prompt,
                fixture_repo=fixture.repo,
                fixture_ref=fixture.ref,
                arm=arm,
                max_turns=fixture.max_turns,
                wall_clock_cap_s=fixture.wall_clock_cap_s,
                skill_slug=skill_slug if arm == "treatment" else None,
                skill_files=skill_files if arm == "treatment" else None,
            )
            result = await run_harness_trial(make_exec(), cfg)
            state.metadata["trial_result"] = result
            return state

        return solve

    @scorer(metrics=[accuracy()])
    def _uplift_scorer():
        async def score(state: TaskState, target: Target) -> Score:
            result = state.metadata["trial_result"]
            correct = await run_acceptance_test(
                make_exec(), list(fixture.acceptance_command)
            )
            turns_score = base.turns_efficiency_score(
                result.turns or fixture.max_turns, fixture.max_turns
            )
            events_summary = (
                f"turns={result.turns} wall_clock_s={result.wall_clock_s:.1f} "
                f"events={len(result.events)}"
            )
            judged_result = await asyncio.to_thread(
                judge_mod.run_judge,
                call_model,
                rubric_text,
                task_uplift_task.judge_content(
                    result.diff, result.untracked, events_summary
                ),
                judged,
                k,
            )
            values = {
                "correctness": 1.0 if correct else 0.0,
                "turns_efficiency": turns_score,
                **judged_result.dimension_medians,
            }
            comp = base.composite(rubric, values)
            return Score(
                value=comp,
                metadata={
                    "dimension_values": values,
                    "judge_samples": judged_result.samples,
                    "injection_suspected": judged_result.injection_suspected,
                    "prescan": judged_result.prescan,
                    "turns": result.turns,
                    "wall_clock_s": result.wall_clock_s,
                },
            )

        return score

    task = Task(
        dataset=samples,
        solver=_harness_bridge(),
        scorer=_uplift_scorer(),
        sandbox=effective_sandbox,
    )
    with tempfile.TemporaryDirectory() as log_dir:
        # An injected exec seam bypasses the Inspect sandbox and may keep
        # per-trial state (e.g. a fake executor); force sequential samples so
        # that state is never corrupted by Inspect's default concurrency.
        eval_kwargs = {"max_samples": 1} if exec_factory is not None else {}
        log = inspect_eval(
            task, model=_NO_MODEL, log_dir=log_dir, display="none",
            fail_on_error=False, **eval_kwargs,
        )[0]
        log_bytes = _read_log(log)
        if log.status != "success" or not log.samples:
            return SuiteOutcome(
                status="errored",
                error=str(log.error or "inspect eval failed"),
                eval_log_bytes=log_bytes,
            )
        trials: list[dict] = []
        injection = False
        for s in log.samples:
            if s.error is not None or not s.scores:
                # errored trial: excluded, never scored as zero (SPEC S6.8);
                # the worker errors the run when an arm comes up short.
                continue
            sc = next(iter(s.scores.values()))
            meta = sc.metadata
            injection = injection or meta["injection_suspected"]
            trials.append(
                {
                    "trial_index": s.metadata["trial_index"],
                    "arm": s.metadata["arm"],
                    "dimension_values": meta["dimension_values"],
                    "composite": float(sc.as_float()),
                    "judge_samples": meta["judge_samples"],
                    "injection_suspected": meta["injection_suspected"],
                    "prescan": meta["prescan"],
                    "extras": {
                        "turns": meta["turns"],
                        "wall_clock_s": meta["wall_clock_s"],
                    },
                }
            )
        if injection:
            return SuiteOutcome(
                status="errored", error="judge-integrity",
                trials=trials, eval_log_bytes=log_bytes,
            )
        return SuiteOutcome(status="completed", trials=trials, eval_log_bytes=log_bytes)
