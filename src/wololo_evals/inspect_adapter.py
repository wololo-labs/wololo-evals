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
import contextlib
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

from inspect_ai import Task, eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import sandbox

from . import harnesses as harnesses_mod
from . import judge as judge_mod
from .engines import sandbox_compose
from .engines.container_auth import (
    BindMount,
    ContainerAuthPlan,
    build_plan,
    resolve_exec_env,
)
from .harness_solver import (
    HarnessTrialConfig,
    TrialErrored,
    materialize_oracle_files,
    run_acceptance_test,
    run_harness_trial,
)
from .harnesses import HarnessRunner
from .suites import base
from .suites.skill_review import task as skill_review_task
from .suites.task_uplift import task as task_uplift_task
from .suites.repo_eval import task as repo_eval_task
from .suites.regression_replay import task as regression_replay_task
from .suites.skill_behaviour import task as skill_behaviour_task

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


# ---------------------------------------------------------------------------
# live docker-sandbox wiring (SPEC I3): per-engine image selection + threading
# container_auth env/mounts + the reachable fixture URL through the exec seam.
# Additive: nothing here runs unless a caller explicitly wires a container
# trial; the existing suites keep ``sandbox_type`` / ``exec_factory`` behaviour.
# ---------------------------------------------------------------------------
#: The container reaches the host over the Docker gateway alias, attached at run
#: time via ``--add-host=host.docker.internal:host-gateway`` (built-in on Docker
#: Desktop; ``host-gateway`` on Linux Docker >= 20.10).
HOST_DOCKER_INTERNAL = "host.docker.internal"
ADD_HOST = f"{HOST_DOCKER_INTERNAL}:host-gateway"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "0.0.0.0", "::1", "localhost"})


def container_fixture_url(host_base_url: str, page: str = "") -> str:
    """Rewrite a host loopback fixture base URL to the URL a *container* reaches
    it by, then append ``page``.

    ``BrowserFixtureServer`` binds ``127.0.0.1:<ephemeral>`` on the host; inside
    a container ``127.0.0.1`` is the container itself, so the loopback host is
    rewritten to :data:`HOST_DOCKER_INTERNAL` (reachable via :data:`ADD_HOST`)
    while the kernel-assigned port is preserved. A non-loopback host is left as
    given (already routable)."""
    parts = urlsplit(host_base_url)
    if (parts.hostname or "").lower() in _LOOPBACK_HOSTS:
        netloc = HOST_DOCKER_INTERNAL
        if parts.port is not None:
            netloc = f"{HOST_DOCKER_INTERNAL}:{parts.port}"
        parts = parts._replace(netloc=netloc)
    url = urlunsplit(parts)
    if page:
        url = url.rstrip("/") + "/" + page.lstrip("/")
    return url


@dataclass(frozen=True)
class ContainerTrialWiring:
    """Everything a live per-engine trial needs, resolved from the harness name.

    Every field is secret-free and safe to log EXCEPT the seam built by
    :attr:`exec_factory`, which injects auth VALUES (from
    :func:`container_auth.resolve_exec_env`) that must never be logged. Log
    :attr:`plan` (KEY names + mount paths + ``satisfied``), never the resolved
    exec env.
    """

    harness: str
    engine_id: str
    image: Optional[str]              # None for a harness with no runner image
    plan: ContainerAuthPlan          # loggable: env_keys + mounts + satisfied
    fixture_url: str                 # host.docker.internal, container-reachable
    add_host: str                    # docker --add-host value
    exec_factory: Callable[[], object]

    @property
    def mounts(self) -> tuple[BindMount, ...]:
        """Read-only credential bind-mounts to attach to the sandbox
        (host_path -> container_path, ``read_only=True``); may be empty."""
        return self.plan.mounts


def wire_container_trial(
    harness: str,
    host_fixture_base_url: str,
    fixture_page: str = "",
    *,
    source_env: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
    base_exec_factory: Optional[Callable[[], object]] = None,
) -> ContainerTrialWiring:
    """Resolve a harness's runner image + container-auth plan + reachable fixture
    URL, and build the exec seam that threads the engine's auth env and the
    fixture URL into every ``sandbox().exec`` call — without ever logging a value.

    The returned :class:`ContainerTrialWiring` is loggable except its
    ``exec_factory``. The caller should log ``plan`` and, when
    ``plan.satisfied`` is False, treat ``plan.operator_preconditions`` as an
    operator gate and NOT attempt a live run (and in this wave, never a live run
    regardless). ``plan.mounts`` attach as read-only bind-mounts; the sandbox is
    run with ``add_host`` so the container reaches the host fixture server.

    Secrets are read only inside the built seam (via ``resolve_exec_env``), at
    exec time, straight into ``sandbox().exec(env=...)``. ``base_exec_factory``
    defaults to the production sandbox seam; tests inject a recording fake so the
    whole path is driven with no docker and no model."""
    engine = harnesses_mod.engine_id(harness)
    if engine is None:
        raise ValueError(
            f"harness {harness!r} has no container-auth engine mapping"
        )
    image = harnesses_mod.runner_image(harness)
    plan = build_plan(engine, source_env=source_env, home=home)
    fixture_url = container_fixture_url(host_fixture_base_url, fixture_page)
    base_factory = base_exec_factory or _sandbox_exec

    def exec_factory():
        inner = base_factory()
        # resolve_exec_env is the ONLY read of secret VALUES; the result is
        # merged into the exec env and NEVER logged (kept out of ``plan``).
        extra_env = {
            **resolve_exec_env(engine, source_env=source_env, home=home),
            "WOLOLO_FIXTURE_URL": fixture_url,
        }

        async def exec_fn(cmd, env, timeout):
            # run_harness_trial's per-call env ({profile_env: /profile,
            # WOLOLO_TRANSCRIPT_DIR: /transcript}) layers ON TOP of the auth +
            # fixture base, yielding the SPEC exec env shape.
            merged = {**extra_env, **(dict(env) if env else {})}
            return await inner(cmd, merged, timeout)

        return exec_fn

    return ContainerTrialWiring(
        harness=harness,
        engine_id=engine,
        image=image,
        plan=plan,
        fixture_url=fixture_url,
        add_host=ADD_HOST,
        exec_factory=exec_factory,
    )


def _resolve_execution(
    stack: contextlib.ExitStack,
    *,
    container_wiring: Optional["ContainerTrialWiring"],
    exec_factory: Optional[Callable[[], object]],
    sandbox_type: str,
):
    """Resolve the ``(make_exec, sandbox_spec)`` a harness suite runs with, and
    register any provisioning teardown on ``stack``.

    Three mutually-exclusive modes cleanly resolve the historical
    ``exec_factory``-vs-``sandbox`` exclusivity:

    - ``container_wiring`` set -> the REAL run: PROVISION the per-engine runner
      image via a rendered compose spec (image + read-only cred mounts +
      ``host.docker.internal`` add-host + the non-secret fixture URL), and use
      the wiring's ``exec_factory`` so auth VALUES + the fixture URL layer onto
      every ``sandbox().exec`` at exec time (never on disk, never logged). This
      is the fix for the wave-1 gap where ``sandbox_type="docker"`` provisioned
      Inspect's GENERIC image with no image selection / mounts / add-host.
    - ``exec_factory`` set -> the fake-exec unit path: NO Inspect sandbox is
      provisioned; the injected seam stands in for ``sandbox().exec`` so the
      whole bridge runs with no docker and no model.
    - neither -> the pre-existing default: a bare ``sandbox_type`` sandbox,
      behaviour unchanged for every existing caller/test.
    """
    if container_wiring is not None:
        if exec_factory is not None:
            raise ValueError(
                "pass either container_wiring (real provisioned run) or "
                "exec_factory (fake-exec unit path), not both"
            )
        compose_path = stack.enter_context(
            sandbox_compose.sandbox_compose_file(container_wiring)
        )
        # ("docker", <compose path>) is Inspect's shorthand for
        # SandboxEnvironmentSpec("docker", <path>); the rendered compose file
        # selects the runner image and attaches the mounts + add-host.
        return container_wiring.exec_factory, (sandbox_type, compose_path)
    make_exec = exec_factory or _sandbox_exec
    effective_sandbox = None if exec_factory is not None else sandbox_type
    return make_exec, effective_sandbox


def probe_container(
    container_wiring: "ContainerTrialWiring",
    cmd: Sequence[str],
    *,
    sandbox_type: str = "docker",
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Provision ``container_wiring``'s runner-image sandbox through Inspect and
    run ONE non-model command inside it, returning ``(returncode, stdout,
    stderr)``.

    This is the offline proof that the real production provisioning path works:
    Inspect accepts the rendered compose spec, brings up the pinned runner image
    with the read-only credential mounts and ``host.docker.internal`` add-host,
    and the command runs through the SAME ``sandbox().exec`` seam production
    uses. Intended for ``['claude', '--version']`` / ``['codex', '--version']``
    and other auth-free, model-free commands — it NEVER invokes a model. Raises
    ``RuntimeError`` if provisioning/eval fails (e.g. docker unavailable).
    """
    @solver
    def _probe_bridge():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            # The wiring's exec_factory layers auth + fixture env onto the real
            # sandbox().exec; for a --version probe that env is simply unused.
            exec_fn = container_wiring.exec_factory()
            code, out, err = await exec_fn(list(cmd), {}, timeout)
            state.metadata["probe"] = {"code": code, "out": out, "err": err}
            return state

        return solve

    @scorer(metrics=[accuracy()])
    def _probe_scorer():
        async def score(state: TaskState, target: Target) -> Score:
            probe = state.metadata["probe"]
            return Score(value=1.0, metadata={"probe": probe})

        return score

    with contextlib.ExitStack() as stack:
        _make_exec, effective_sandbox = _resolve_execution(
            stack,
            container_wiring=container_wiring,
            exec_factory=None,
            sandbox_type=sandbox_type,
        )
        log_dir = stack.enter_context(tempfile.TemporaryDirectory())
        task = Task(
            dataset=[Sample(input="probe", id="probe-0")],
            solver=_probe_bridge(),
            scorer=_probe_scorer(),
            sandbox=effective_sandbox,
        )
        log = inspect_eval(
            task, model=_NO_MODEL, log_dir=log_dir, display="none",
            fail_on_error=False,
        )[0]
        # The eval log is loaded lazily from ``log_dir``; read everything BEFORE
        # the ExitStack tears the temp dir (and the compose file) down.
        if log.status != "success" or not log.samples:
            raise RuntimeError(
                f"container probe failed: {log.error or 'no samples'}"
            )
        sample = log.samples[0]
        if sample.error is not None or not sample.scores:
            raise RuntimeError(f"container probe errored: {sample.error}")
        probe = next(iter(sample.scores.values())).metadata["probe"]
        return probe["code"], probe["out"], probe["err"]


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
    container_wiring: Optional["ContainerTrialWiring"] = None,
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

    ``container_wiring`` is the REAL production path: a
    :class:`ContainerTrialWiring` (from :func:`wire_container_trial`) whose
    rendered compose spec PROVISIONS the per-engine runner image with the
    read-only credential mounts and the ``host.docker.internal`` add-host, and
    whose ``exec_factory`` layers the auth env + fixture URL onto every
    ``sandbox().exec`` at exec time (never on disk, never logged). It is
    mutually exclusive with ``exec_factory`` (the fake-exec unit path).
    """
    rubric = base.load_rubric("task-uplift")
    judged = base.judged_dimensions(rubric)
    rubric_text = base.rubric_judge_text(rubric)
    # The exec seam is resolved eagerly (no disk state needed): the wiring's
    # exec_factory for a real provisioned run, else the injected fake or the
    # production ``sandbox().exec``. The Inspect sandbox spec (which needs the
    # compose file to persist through the eval) is resolved in the eval block.
    if container_wiring is not None and exec_factory is not None:
        raise ValueError(
            "pass either container_wiring (real provisioned run) or "
            "exec_factory (fake-exec unit path), not both"
        )
    make_exec = (
        container_wiring.exec_factory
        if container_wiring is not None
        else (exec_factory or _sandbox_exec)
    )

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
            # Restore the correctness oracle from the trusted pinned ref before
            # running acceptance: the treatment arm installs an attacker-
            # controllable skill and the harness can write anywhere in /workspace
            # (including the acceptance test). Materializing the oracle paths from
            # fixture.ref defeats a harness that weakened/stubbed the test to fake
            # correctness (same defense as repo-eval's oracle_files).
            exec_fn = make_exec()
            await materialize_oracle_files(
                exec_fn, fixture.ref, list(fixture.oracle_files)
            )
            correct = await run_acceptance_test(
                exec_fn, list(fixture.acceptance_command)
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

    with contextlib.ExitStack() as stack:
        # Resolve the Inspect sandbox spec: for a real wired run this renders +
        # materialises the compose file that selects the runner image and
        # attaches the ro cred mounts + add-host (registered on ``stack`` so it
        # outlives the eval); the fake-exec path gets None; otherwise the bare
        # ``sandbox_type``. ``make_exec`` was already bound eagerly above.
        _, effective_sandbox = _resolve_execution(
            stack,
            container_wiring=container_wiring,
            exec_factory=exec_factory,
            sandbox_type=sandbox_type,
        )
        task = Task(
            dataset=samples,
            solver=_harness_bridge(),
            scorer=_uplift_scorer(),
            sandbox=effective_sandbox,
        )
        # An injected exec seam bypasses the Inspect sandbox and may keep
        # per-trial state (e.g. a fake executor); force sequential samples so
        # that state is never corrupted by Inspect's default concurrency.
        log_dir = stack.enter_context(tempfile.TemporaryDirectory())
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


# ---------------------------------------------------------------------------
# skill-behaviour: paired v1-vs-v2 skill-upgrade A/B, harness-under-eval
# ---------------------------------------------------------------------------
def run_skill_behaviour(
    fixture: skill_behaviour_task.SkillBehaviourFixture,
    runner: HarnessRunner,
    n_trials: int,
    skill_slug: str,
    skill_files_baseline: dict,
    skill_files_treatment: dict,
    call_model: Callable[[str, str], str],
    *,
    k: int = 5,
    sandbox_type: str = "docker",
    arms: Sequence[str] = skill_behaviour_task.ARMS,
    exec_factory: Optional[Callable[[], "object"]] = None,
    judge_enabled: bool = True,
    container_wiring: Optional["ContainerTrialWiring"] = None,
) -> SuiteOutcome:
    """One sample per (trial_index, arm); each sample runs the REAL harness CLI
    inside its own Inspect sandbox via the injected exec seam. Unlike task-uplift
    BOTH arms install a skill -- baseline the incumbent (v1), treatment the
    proposed upgrade (v2) -- so both go through the harness bridge's treatment
    install path; the logical arm (from sample metadata) is what pairs and
    persists. Per-trial composite maps onto the skill_behaviour family slots
    (activation_correct/task_outcome/trace_conformance rule-based; the one judge
    slot llm_judge_overall, skipped when ``judge_enabled`` is False).

    ``container_wiring`` is the REAL production path (see :func:`run_task_uplift`):
    its rendered compose spec provisions the per-engine runner image with the
    read-only credential mounts + ``host.docker.internal`` add-host, and its
    ``exec_factory`` layers auth env + fixture URL at exec time. Mutually
    exclusive with the fake-exec ``exec_factory``.
    """
    rubric = base.load_rubric("skill-behaviour")
    judged = base.judged_dimensions(rubric)  # ["llm_judge_overall"]
    rubric_text = base.rubric_judge_text(rubric)
    if container_wiring is not None and exec_factory is not None:
        raise ValueError(
            "pass either container_wiring (real provisioned run) or "
            "exec_factory (fake-exec unit path), not both"
        )
    make_exec = (
        container_wiring.exec_factory
        if container_wiring is not None
        else (exec_factory or _sandbox_exec)
    )
    skill_by_arm = {"baseline": skill_files_baseline, "treatment": skill_files_treatment}

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
    def _skill_bridge():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            arm = state.metadata["arm"]
            # Both arms install a skill (v1 vs v2), so both use the bridge's
            # treatment install path; the logical arm lives in sample metadata.
            cfg = HarnessTrialConfig(
                runner=runner,
                task_prompt=fixture.task_prompt,
                fixture_repo=fixture.repo,
                fixture_ref=fixture.ref,
                arm="treatment",
                max_turns=fixture.max_turns,
                wall_clock_cap_s=fixture.wall_clock_cap_s,
                skill_slug=skill_slug,
                skill_files=skill_by_arm[arm],
            )
            state.metadata["trial_result"] = await run_harness_trial(make_exec(), cfg)
            return state

        return solve

    @scorer(metrics=[accuracy()])
    def _skill_scorer():
        async def score(state: TaskState, target: Target) -> Score:
            result = state.metadata["trial_result"]
            # Restore the correctness oracle from the trusted pinned ref before
            # running acceptance (same tamper defense as task-uplift/repo-eval).
            exec_fn = make_exec()
            await materialize_oracle_files(
                exec_fn, fixture.ref, list(fixture.oracle_files)
            )
            correct = await run_acceptance_test(
                exec_fn, list(fixture.acceptance_command)
            )
            activation = skill_behaviour_task.activation_correct_score(
                result.events, expects_activation=fixture.expects_activation
            )
            conformance = skill_behaviour_task.trace_conformance_score(
                result.events, fixture.declared_workflow
            )
            values = {
                "activation_correct": activation,
                "task_outcome": 1.0 if correct else 0.0,
                "trace_conformance": conformance,
            }
            judge_samples: list[dict] = []
            injection = False
            prescan: list[str] = []
            if judge_enabled:
                events_summary = (
                    f"turns={result.turns} wall_clock_s={result.wall_clock_s:.1f} "
                    f"events={len(result.events)}"
                )
                judged_result = await asyncio.to_thread(
                    judge_mod.run_judge,
                    call_model,
                    rubric_text,
                    skill_behaviour_task.judge_content(
                        result.diff, result.untracked, events_summary
                    ),
                    judged,
                    k,
                )
                values.update(judged_result.dimension_medians)
                judge_samples = judged_result.samples
                injection = judged_result.injection_suspected
                prescan = judged_result.prescan
            comp = skill_behaviour_task.compose(values, judge_disabled=not judge_enabled)
            return Score(
                value=comp,
                metadata={
                    "dimension_values": values,
                    "judge_samples": judge_samples,
                    "injection_suspected": injection,
                    "prescan": prescan,
                    "judge_disabled": not judge_enabled,
                    "used_skill": skill_behaviour_task.used_skill(result.events),
                    "turns": result.turns,
                    "wall_clock_s": result.wall_clock_s,
                },
            )

        return score

    with contextlib.ExitStack() as stack:
        # Same three-mode resolution as run_task_uplift: real compose spec (with
        # the compose file kept alive on ``stack``), fake-exec None, or the bare
        # sandbox_type. ``make_exec`` was already bound eagerly above.
        _, effective_sandbox = _resolve_execution(
            stack,
            container_wiring=container_wiring,
            exec_factory=exec_factory,
            sandbox_type=sandbox_type,
        )
        task = Task(
            dataset=samples,
            solver=_skill_bridge(),
            scorer=_skill_scorer(),
            sandbox=effective_sandbox,
        )
        log_dir = stack.enter_context(tempfile.TemporaryDirectory())
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
                continue  # errored trial: excluded, never scored as zero (S6.8)
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
                        "judge_disabled": meta["judge_disabled"],
                        "used_skill": meta["used_skill"],
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


# ---------------------------------------------------------------------------
# repo-eval: single-arm harness-under-eval over a generated real-history fixture
# ---------------------------------------------------------------------------
def run_repo_eval(
    fixture: repo_eval_task.RepoEvalFixture,
    runner: HarnessRunner,
    n_trials: int,
    call_model: Callable[[str, str], str],
    *,
    k: int = 5,
    sandbox_type: str = "docker",
    exec_factory: Optional[Callable[[], "object"]] = None,
) -> SuiteOutcome:
    """Single-arm harness-under-eval (SPEC S5.3): the harness re-derives an
    isolable unit of work from a commit message alone; scored on correctness
    (real seeded test suite RUN in-sandbox), churn_penalty (rule-based off diff
    line counts vs the historical fix) and abstraction_adherence (hardened
    judge). No skill is installed — repo-eval scores a harness/model config.

    ``exec_factory`` injects the :data:`SandboxExec` seam exactly as
    :func:`run_task_uplift`, so the whole path is exercisable end to end with a
    fake executor where no docker daemon exists; production leaves it None.
    """
    rubric = base.load_rubric("repo-eval")
    judged = base.judged_dimensions(rubric)          # ["abstraction_adherence"]
    rubric_text = base.rubric_judge_text(rubric)
    make_exec = exec_factory or _sandbox_exec
    effective_sandbox = None if exec_factory is not None else sandbox_type

    samples = [
        Sample(
            input=fixture.task_prompt,
            id=f"single-{i}",
            metadata={"arm": "single", "trial_index": i},
        )
        for i in range(n_trials)
    ]

    @solver
    def _repo_bridge():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            cfg = HarnessTrialConfig(
                runner=runner,
                task_prompt=fixture.task_prompt,
                fixture_repo=fixture.repo,
                fixture_ref=fixture.parent_ref,   # checkout BEFORE the change
                arm="baseline",                   # single-arm: no skill installed
                max_turns=fixture.max_turns,
                wall_clock_cap_s=fixture.wall_clock_cap_s,
            )
            state.metadata["trial_result"] = await run_harness_trial(make_exec(), cfg)
            return state

        return solve

    @scorer(metrics=[accuracy()])
    def _repo_scorer():
        async def score(state: TaskState, target: Target) -> Score:
            result = state.metadata["trial_result"]
            # Correctness is the fix-commit's OWN oracle: pull the commit's test
            # files onto the parent checkout before running acceptance, so a
            # genuinely-correct fix is not failed by a test that was added/
            # changed in the same commit (SPEC S5.3: "the repo's real seeded
            # test suite RUN in-sandbox").
            exec_fn = make_exec()
            await materialize_oracle_files(
                exec_fn, fixture.commit_ref, list(fixture.oracle_files)
            )
            correct = await run_acceptance_test(
                exec_fn, list(fixture.acceptance_command)
            )
            candidate_lines = repo_eval_task.diff_lines_changed(result.diff)
            churn = repo_eval_task.churn_penalty_score(
                fixture.historical_lines_changed, candidate_lines
            )
            judged_result = await asyncio.to_thread(
                judge_mod.run_judge,
                call_model,
                rubric_text,
                repo_eval_task.judge_content(result.diff, result.untracked),
                judged,
                k,
            )
            values = {
                "correctness": 1.0 if correct else 0.0,
                "churn_penalty": churn,
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
                    "candidate_lines": candidate_lines,
                    "historical_lines": fixture.historical_lines_changed,
                },
            )

        return score

    task = Task(
        dataset=samples,
        solver=_repo_bridge(),
        scorer=_repo_scorer(),
        sandbox=effective_sandbox,
    )
    return _collect_trial_outcome(task, exec_factory)


# ---------------------------------------------------------------------------
# regression-replay: single-arm judge over the corrections corpus (no sandbox)
# ---------------------------------------------------------------------------
def run_regression_replay(
    candidate_content: str,
    corpus: Sequence[regression_replay_task.CorrectionSample],
    call_model: Callable[[str, str], str],
    *,
    k: int = 5,
) -> SuiteOutcome:
    """Score a candidate against the replayed corrections corpus (SPEC S5.6).

    One judged sample per correction; the correction's trusted failure
    reference is woven into the system-message rubric, the candidate content
    is nonce-fenced (SPEC S8). Statistical n is the corpus size; the worker's
    single_arm_stats over the per-sample composites is the verdict.
    """
    if not corpus:
        return SuiteOutcome(status="errored", error="empty corrections corpus")
    rubric = base.load_rubric("regression-replay")
    dimensions = list(rubric["dimensions"])
    base_rubric_text = base.rubric_judge_text(rubric)

    trials: list[dict] = []
    for i, sample in enumerate(corpus):
        per_sample_rubric = regression_replay_task.rubric_text_with_reference(
            base_rubric_text, sample
        )
        try:
            result = judge_mod.run_judge(
                call_model, per_sample_rubric, candidate_content, dimensions, k
            )
        except (judge_mod.JudgeIntegrityError, judge_mod.JudgeParseError) as exc:
            return SuiteOutcome(status="errored", error=f"judge: {exc}")
        if result.injection_suspected:
            return SuiteOutcome(
                status="errored",
                error="judge-integrity",
                trials=trials,
            )
        comp = base.composite(rubric, result.dimension_medians)
        trials.append(
            {
                "trial_index": i,
                "arm": "single",
                "dimension_values": result.dimension_medians,
                "composite": comp,
                "judge_samples": result.samples,
                "injection_suspected": result.injection_suspected,
                "prescan": result.prescan,
                "extras": {"correction_id": sample.correction_id},
            }
        )
    return SuiteOutcome(status="completed", trials=trials)


def _collect_trial_outcome(task, exec_factory) -> SuiteOutcome:
    """Shared tail for multi-trial harness suites: run the Inspect eval,
    normalise samples into trials, fail closed on judge integrity. Sequential
    samples when a fake exec seam is injected (it may hold per-trial state)."""
    with tempfile.TemporaryDirectory() as log_dir:
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
                continue  # errored trial: excluded, never scored as zero (S6.8)
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
                        key: meta[key]
                        for key in ("turns", "wall_clock_s", "candidate_lines",
                                    "historical_lines")
                        if key in meta
                    },
                }
            )
        if injection:
            return SuiteOutcome(
                status="errored", error="judge-integrity",
                trials=trials, eval_log_bytes=log_bytes,
            )
        return SuiteOutcome(status="completed", trials=trials, eval_log_bytes=log_bytes)
