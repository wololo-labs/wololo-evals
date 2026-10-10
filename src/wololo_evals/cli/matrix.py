"""``matrix``: expand engine x model x prompt/baseline x asset x case x seed,
dry-run the plan or drive the harness, and write the §15 artefact tree
(open, Apache-2.0).

The runner is decoupled from concrete engines: it drives an injected
``EngineAdapter`` factory (README's "injected EngineAdapter") plus an injected
grader seam, so the whole matrix lifecycle is exercised with fakes and no model
spend. ``--dry-run`` builds the full plan and writes nothing. Budget: on cap
the in-flight case finishes and remaining runs are marked ``cost_cap_hit`` with
partial artefacts written (§13).

May import ``core.interfaces``, ``engines``, ``trace``, the scoring module,
stdlib/``tomllib`` — never ``db``/``api``/``jobs``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Optional, Sequence

from ..core import interfaces as ci
from . import artefact_writer as aw
from . import cases_compile
from .config import AssetEntry, BaselineEntry, Benchmark, MatrixConfig, load_benchmark

__all__ = [
    "Combo",
    "MatrixPlan",
    "expand",
    "build_combos",
    "plan",
    "render_plan",
    "run_matrix",
    "AdapterFactory",
    "NormaliserFactory",
    "Grader",
]

#: Injected seams — the runner never constructs concrete engines itself.
AdapterFactory = Callable[[str], ci.EngineAdapter]
NormaliserFactory = Callable[[str], Optional[ci.TraceNormaliser]]
Grader = Callable[[ci.RunSpec, ci.RunResult, Sequence[ci.TraceEvent]], Sequence[ci.ScoredSignal]]


@dataclass(frozen=True)
class Combo:
    """One engine+model+prompt (or +baseline), optionally +asset (§7)."""

    combo_id: str
    engine_id: str
    model_id: str
    prompt_id: str
    role: str
    asset: Optional[AssetEntry] = None
    baseline: Optional[BaselineEntry] = None


def build_combos(config: MatrixConfig) -> list[Combo]:
    """Cross the models with prompts (treatment) and baselines (control)."""
    combos: list[Combo] = []
    assets: Sequence[Optional[AssetEntry]] = config.assets or (None,)
    for model in config.models:
        for prompt in config.prompts or ():
            for asset in assets:
                parts = [model.id, prompt.id] + ([asset.id] if asset else [])
                combos.append(
                    Combo(
                        combo_id="__".join(parts),
                        engine_id=model.engine,
                        model_id=model.id,
                        prompt_id=prompt.id,
                        role="treatment",
                        asset=asset,
                    )
                )
        for baseline in config.baselines or ():
            combos.append(
                Combo(
                    combo_id="__".join([model.id, "baseline", baseline.id]),
                    engine_id=model.engine,
                    model_id=model.id,
                    prompt_id=baseline.id,
                    role="baseline",
                    baseline=baseline,
                )
            )
    return combos


def _snapshot(entry: Optional[AssetEntry], base_dir: pathlib.Path) -> ci.AssetSnapshot:
    """Materialise a lightweight ``AssetSnapshot`` from a config asset entry.

    Reads a local ``path`` (file or directory) into ``files``; ``inline`` bodies
    are used verbatim. Full git-ref materialisation is the engine adapter's job.
    """
    if entry is None:
        ref = ci.AssetRef(kind="custom_prompt", id="__none__", inline_body="")
        digest = hashlib.sha256(b"").hexdigest()
        return ci.AssetSnapshot(ref=ref, body="", files={}, adapter_id="local", snapshot_digest=digest)

    body = entry.inline or ""
    files: dict[str, str] = {}
    if entry.path and not entry.inline:
        p = base_dir / entry.path
        if p.is_file():
            body = p.read_text(encoding="utf-8")
            files[entry.path] = body
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    files[str(f.relative_to(p))] = f.read_text(encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    ref = ci.AssetRef(
        kind=entry.kind,
        id=entry.id,
        path=entry.path,
        git_ref=entry.git_ref,
        inline_body=entry.inline,
    )
    return ci.AssetSnapshot(
        ref=ref, body=body, files=files, adapter_id="local", snapshot_digest=digest
    )


#: Files the scorers read to grade a run. They sit next to the fixture so the
#: manifests stay beside the code they describe, but they must never reach a
#: case workspace: the defect manifest is the answer key, and its rationales
#: name the rules a good suite is meant to discover. The scorers resolve these
#: benchmark-relative, against the committed copy, so withholding them from the
#: workspace costs nothing.
_ORACLE_FILES = ("seeded_defects.json", "changed_lines.json")


def _materialise_fixture(fixtures_dir: Optional[pathlib.Path], workspace: pathlib.Path) -> None:
    """Copy the benchmark's fixture tree into one case workspace.

    A live run has to be able to read the code it is asked to write tests for.
    The replay engine re-materialises a workspace from its recording, so this
    is the live path's equivalent: one pristine copy per case, never shared, so
    a run that edits the code under test cannot affect the next case. The
    scorers still resolve their target benchmark-relative, against the
    committed fixture, so a tampered copy lowers the score rather than moving
    the yardstick.
    """
    if fixtures_dir is None:
        return
    if not fixtures_dir.is_dir():
        raise ValueError(f"fixtures_dir {fixtures_dir} is not a directory")
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        fixtures_dir, workspace, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".pytest_cache", "mutants", *_ORACLE_FILES
        ),
    )


def _prepare_workspace(work: pathlib.Path, fixtures_dir: Optional[pathlib.Path]) -> None:
    """Make ``work`` a pristine trial workspace: the fixture, and its own git.

    Idempotent and destructive on purpose: anything already in ``work`` is
    removed first, so the same call both prepares a workspace and resets one.
    """
    if work.exists():
        for child in work.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    work.mkdir(parents=True, exist_ok=True)
    _materialise_fixture(fixtures_dir, work)
    _seal_workspace_git(work)


def _retain_workspace(work: pathlib.Path, dest: pathlib.Path) -> None:
    """Copy a finished trial's workspace into the artefact tree to be inspected.

    The trial runs somewhere else (see ``_execute_combo``); what stays behind is
    a copy, without the throwaway repository the trial was given.
    """
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(work, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))


def _seal_workspace_git(workspace: pathlib.Path) -> None:
    """Give the workspace its own git repository, so a trial cannot commit here.

    A trial once ran inside this repository's tree, and a run that decided to
    commit its work committed to the branch being used to measure it: one trial
    put generated tests into the fixture's own shipped suite, which is the
    control baseline, and every later case then started from a strong suite.

    Trials now run in a throwaway directory outside the repository, which
    closes that path by location. The repository is kept anyway, so that a
    temp directory that happens to sit inside some other git tree still cannot
    be committed into. Neither is a sandbox, and neither pretends to be.
    """
    try:
        subprocess.run(
            ["git", "init", "-q"], cwd=workspace, check=True,
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            f"could not seal workspace {workspace} from the host repository: {exc}"
        ) from exc


def _baseline_snapshot(baseline: BaselineEntry) -> ci.AssetSnapshot:
    body = baseline.inline or ""
    ref = ci.AssetRef(kind="custom_prompt", id=baseline.id, inline_body=body)
    return ci.AssetSnapshot(
        ref=ref,
        body=body,
        files={},
        adapter_id="local",
        snapshot_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


def _run_id(combo: Combo, case_id: str, seed: int) -> str:
    return f"{combo.combo_id}__{case_id}__s{seed}"


def _validate_engines(config: MatrixConfig, benchmark: Benchmark) -> None:
    if benchmark.engines:
        for model in config.models:
            if model.engine not in benchmark.engines:
                raise ValueError(
                    f"model {model.id!r} engine {model.engine!r} not in benchmark "
                    f"{benchmark.id!r} engines {tuple(benchmark.engines)}"
                )


def _select_cases(config: MatrixConfig, cases):
    """Apply ``only_cases``, so a plan and a run can never disagree.

    The dry run and the real run once counted differently for exactly this
    reason: the filter lived in one and not the other, and a dry run reporting
    six cases would have preceded a run of three.
    """
    if not config.only_cases:
        return list(cases)
    wanted = set(config.only_cases)
    unknown = wanted - {case.case_id for case in cases}
    if unknown:
        raise ValueError(f"only_cases names unknown case ids: {sorted(unknown)}")
    return [case for case in cases if case.case_id in wanted]


def expand(config: MatrixConfig) -> list[ci.RunSpec]:
    """Full ``(engine, model, prompt, asset, case, seed)`` cross-product (§13).

    Reads the benchmark + compiled ``cases.jsonl`` from disk (asset files too),
    but invokes no engine — the return value is exactly the set of runs a live
    ``matrix`` would execute.
    """
    benchmark = load_benchmark(config.benchmark_dir)
    _validate_engines(config, benchmark)
    cases = _select_cases(
        config, cases_compile.load_cases(config.benchmark_dir / "cases.jsonl")
    )
    combos = build_combos(config)

    specs: list[ci.RunSpec] = []
    for combo in combos:
        snapshot = (
            _baseline_snapshot(combo.baseline)
            if combo.role == "baseline" and combo.baseline is not None
            else _snapshot(combo.asset, config.base_dir)
        )
        for case in cases:
            for seed in range(config.seeds):
                specs.append(
                    ci.RunSpec(
                        run_id=_run_id(combo, case.case_id, seed),
                        asset=snapshot,
                        case=case,
                        engine_id=combo.engine_id,
                        model_id=combo.model_id,
                        prompt_id=combo.prompt_id,
                        seed=seed,
                        cwd=str(config.output_path / combo.combo_id / case.case_id / str(seed)),
                        caps=ci.RunCaps(max_cost_usd=config.max_cost_usd),
                    )
                )
    return specs


# ---------------------------------------------------------------------------
# Dry-run plan
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MatrixPlan:
    matrix_id: str
    benchmark_id: str
    evaluation_type: str
    combos: Sequence[Combo]
    n_cases: int
    seeds: int
    specs: Sequence[ci.RunSpec]

    @property
    def total_runs(self) -> int:
        return len(self.specs)


def plan(config: MatrixConfig) -> MatrixPlan:
    """Build the dry-run plan without touching any engine."""
    benchmark = load_benchmark(config.benchmark_dir)
    _validate_engines(config, benchmark)
    cases = _select_cases(
        config, cases_compile.load_cases(config.benchmark_dir / "cases.jsonl")
    )
    combos = build_combos(config)
    specs = expand(config)
    return MatrixPlan(
        matrix_id=config.matrix_id,
        benchmark_id=benchmark.id,
        evaluation_type=benchmark.evaluation_type,
        combos=combos,
        n_cases=len(cases),
        seeds=config.seeds,
        specs=specs,
    )


def render_plan(p: MatrixPlan) -> str:
    lines = [
        f"matrix: {p.matrix_id}",
        f"benchmark: {p.benchmark_id} ({p.evaluation_type})",
        f"combos: {len(p.combos)}  cases: {p.n_cases}  seeds: {p.seeds}",
        f"total runs: {p.total_runs}",
        "",
        "combos:",
    ]
    for c in p.combos:
        asset = f" asset={c.asset.id}" if c.asset else ""
        lines.append(f"  - {c.combo_id} [{c.role}] {c.engine_id}/{c.model_id}{asset}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live / injected execution
# ---------------------------------------------------------------------------
def _spec_extra(case: ci.CaseSpec, prompt_preamble: Optional[str]) -> dict:
    """Per-run extras. The preamble names the asset, so it stays off a case
    whose point is that the asset must stay silent.

    A distractor case checks for a skill that fires where it should not. Told
    to use the skill, the forced arm would fail it by construction, and the
    failure would measure the preamble rather than the skill.
    """
    if prompt_preamble and case.expected_outcome != "no_activation":
        return {"prompt_preamble": prompt_preamble}
    return {}


def _run_with_retry(
    adapter: ci.EngineAdapter,
    spec: ci.RunSpec,
    reset: Optional[Callable[[], None]] = None,
) -> ci.RunResult:
    """Execute a spec, retrying once on ``infra_fail`` (§14.2).

    ``reset`` puts the workspace back as it was before the first attempt. It
    runs before the retry, because a failed attempt may have edited the code
    under test or half-written a suite, and a retry that starts from that is
    scored as though it started clean.
    """
    result = adapter.run(spec)
    if result.status == "infra_fail":
        if reset is not None:
            reset()
        result = adapter.run(spec)
    return result


def _seed_case_payload(
    spec: ci.RunSpec, result: ci.RunResult, composed: aw.ComposedCase
) -> dict:
    return {
        "run_id": spec.run_id,
        "case_id": spec.case.case_id,
        "case_weight": spec.case.case_weight,
        "quarantined": spec.case.quarantined,
        "seed": spec.seed,
        "status": result.status,
        "score": composed.score,
        "passed": composed.passed,
        "hard_gate_failed": composed.hard_gate_failed,
        "evaluator_errors": composed.evaluator_errors,
        "cost_usd": result.cost_usd,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "runtime_seconds": result.runtime_seconds,
        "attempts": result.attempts,
        "signals": [aw._signal_row(s) for s in composed.signals],
    }


def _execute_combo(
    combo: Combo,
    all_cases: Sequence[ci.CaseSpec],
    seeds: int,
    run_dir: pathlib.Path,
    *,
    benchmark: Benchmark,
    base_dir: pathlib.Path,
    adapter_factory: AdapterFactory,
    grader: Grader,
    normaliser_factory: Optional[NormaliserFactory],
    events: aw.EventLog,
    timestamp: str,
    cap: Optional[float],
    cumulative: float,
    smoke: bool,
    engine_versions: dict[str, str],
    fixtures_dir: Optional[pathlib.Path] = None,
    prompt_preamble: Optional[str] = None,
) -> float:
    """Run one combo's cases x seeds, write its seed/case/scorecard artefacts
    under ``run_dir``, append events, and return the new cumulative spend."""
    snapshot = (
        _baseline_snapshot(combo.baseline)
        if combo.role == "baseline" and combo.baseline is not None
        else _snapshot(combo.asset, base_dir)
    )
    outcomes: dict[str, list[aw.SeedOutcome]] = {}
    artefact_refs: dict[str, str] = {}

    for case in all_cases:
        seed_outcomes: list[aw.SeedOutcome] = []
        payloads: list[dict] = []
        for seed in range(seeds):
            # The trial runs in a throwaway directory outside the repository.
            # It used to run in the artefact tree, 16 levels below the repo
            # root, where `../` reached the answer key and the skill file and
            # the trial had --dangerously-skip-permissions. Moving it removes
            # the casual route; it is not a sandbox, and an absolute path still
            # works. What stays in the artefact tree is a copy, taken after
            # grading.
            work = pathlib.Path(tempfile.mkdtemp(prefix="we-trial-")).resolve()
            try:
                spec = ci.RunSpec(
                    run_id=_run_id(combo, case.case_id, seed),
                    asset=snapshot,
                    case=case,
                    engine_id=combo.engine_id,
                    model_id=combo.model_id,
                    prompt_id=combo.prompt_id,
                    seed=seed,
                    cwd=str(work),
                    caps=ci.RunCaps(),
                    extra=_spec_extra(case, prompt_preamble),
                )
                if cap is not None and cumulative >= cap:
                    result = ci.RunResult(
                        run_id=spec.run_id,
                        status="cost_cap_hit",
                        artefact_dir=str(ci.artefacts.seed_dir(run_dir, seed)),
                        runtime_seconds=0.0,
                    )
                    composed = aw.compose_case(
                        [], benchmark.evaluation_type,
                        pass_threshold=benchmark.pass_threshold, scored=False,
                    )
                    events.record(
                        "run_capped", ts=timestamp, run_id=spec.run_id,
                        combo_id=combo.combo_id, case_id=case.case_id, seed=seed,
                        status="cost_cap_hit",
                    )
                else:
                    _prepare_workspace(work, fixtures_dir)
                    kept_advisory = ci.artefacts.advisory_dir(run_dir, seed, case.case_id)
                    kept_advisory.mkdir(parents=True, exist_ok=True)
                    adapter = adapter_factory(combo.engine_id)
                    if combo.engine_id not in engine_versions:
                        try:
                            engine_versions[combo.engine_id] = adapter.cli_version()
                        except Exception:  # noqa: BLE001 - version probe is best-effort
                            engine_versions[combo.engine_id] = "unknown"
                    def _reset(_spec=None, _work=work, _fx=fixtures_dir):  # noqa: ANN001
                        _prepare_workspace(_work, _fx)

                    # The adapter retries on its own as well, so it gets the
                    # same reset hook if it has the slot for one.
                    if hasattr(adapter, "reset_workspace"):
                        adapter.reset_workspace = _reset
                    result = _run_with_retry(adapter, spec, reset=_reset)
                    cumulative += result.cost_usd
                    trace: list[ci.TraceEvent] = []
                    if normaliser_factory is not None:
                        norm = normaliser_factory(combo.engine_id)
                        if norm is not None:
                            trace = list(norm.normalise(adapter.raw_event_path(spec)))
                    _write_trace(run_dir, seed, case.case_id, trace)
                    signals = list(grader(spec, result, trace))
                    composed = aw.compose_case(
                        signals, benchmark.evaluation_type,
                        pass_threshold=benchmark.pass_threshold,
                        scored=result.scored, judge_disabled=smoke,
                    )
                    # Only now is the workspace complete: the run, the trace and
                    # the grading have all written to it.
                    _retain_workspace(
                        work, ci.artefacts.workspace_dir(run_dir, seed, case.case_id)
                    )
                    result = dataclasses.replace(result, artefact_dir=str(kept_advisory))
                    events.record(
                        "run_finished", ts=timestamp, run_id=spec.run_id,
                        combo_id=combo.combo_id, case_id=case.case_id, seed=seed,
                        status=result.status, cost_usd=result.cost_usd,
                        tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                        duration_ms=int(result.runtime_seconds * 1000),
                    )
            finally:
                shutil.rmtree(work, ignore_errors=True)
            payload = _seed_case_payload(spec, result, composed)
            aw.write_json(ci.artefacts.seed_case_path(run_dir, seed, case.case_id), payload)
            payloads.append(payload)
            seed_outcomes.append(aw.SeedOutcome(seed=seed, result=result, composed=composed))

        outcomes[case.case_id] = seed_outcomes
        agg_path = ci.artefacts.case_aggregate_path(run_dir, case.case_id)
        artefact_refs[case.case_id] = str(agg_path.relative_to(run_dir))
        aw.write_json(
            agg_path,
            {
                "case_id": case.case_id,
                "score": sum(so.composed.score for so in seed_outcomes) / len(seed_outcomes),
                "pass_rate": sum(1 for so in seed_outcomes if so.composed.passed)
                / len(seed_outcomes),
                "seeds": payloads,
            },
        )

    scorecard = aw.build_scorecard(
        aw.ComboRecord(
            combo_id=combo.combo_id, engine_id=combo.engine_id,
            model_id=combo.model_id, prompt_id=combo.prompt_id, role=combo.role,
            scorecard_path="", combo_dir="",
            asset_id=(combo.asset.id if combo.asset else None),
        ),
        benchmark.id, seeds_configured=seeds, case_specs=all_cases,
        outcomes=outcomes, artefact_refs=artefact_refs, judge_disabled=smoke,
    )
    aw.write_json(ci.artefacts.scorecard_path(run_dir), scorecard)
    events.write(ci.artefacts.run_events_path(run_dir), filter_combo=combo.combo_id)
    return cumulative


def run_matrix(
    config: MatrixConfig,
    adapter_factory: AdapterFactory,
    grader: Grader,
    *,
    timestamp: str,
    iteration: int = 1,
    normaliser_factory: Optional[NormaliserFactory] = None,
    max_cost_usd: Optional[float] = None,
    smoke: bool = False,
    allow_engine_drift: bool = False,
    cache_mode: str = "off",
    harness_version: str = "0.1.0",
) -> pathlib.Path:
    """Drive the matrix through injected adapters, writing the §15 tree.

    Returns the ``matrix-<timestamp>`` directory holding ``matrix_summary.json``.
    """
    benchmark = load_benchmark(config.benchmark_dir)
    _validate_engines(config, benchmark)
    all_cases = cases_compile.load_cases(config.benchmark_dir / "cases.jsonl")
    all_cases = _select_cases(config, all_cases)
    if config.expected_skill:
        # The benchmark names the skill it was written around; the asset under
        # evaluation may ship the same capability under its own slug, and
        # `trace:used_skill` with no argument reads the case's expectation.
        all_cases = [
            replace(case, expected_skill=config.expected_skill) for case in all_cases
        ]
    combos = build_combos(config)
    seeds = config.seeds

    if smoke:
        treatment = next(
            (c for c in all_cases if not c.quarantined and c.role == "treatment"), None
        )
        if treatment is None:
            raise ValueError("--smoke: no non-quarantined treatment case in cases.jsonl")
        all_cases = [treatment]
        combos = combos[:1]
        seeds = 1

    matrix_dir = ci.artefacts.matrix_dir(config.output_path, iteration, timestamp)
    matrix_dir.mkdir(parents=True, exist_ok=True)

    cap = max_cost_usd if max_cost_usd is not None else config.max_cost_usd
    cumulative = 0.0
    events = aw.EventLog()
    engine_versions: dict[str, str] = {}
    combo_records: list[aw.ComboRecord] = []

    for combo in combos:
        run_dir = ci.artefacts.combo_run_dir(matrix_dir, combo.combo_id, combo.combo_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        cumulative = _execute_combo(
            combo, all_cases, seeds, run_dir,
            benchmark=benchmark, base_dir=config.base_dir,
            adapter_factory=adapter_factory, grader=grader,
            normaliser_factory=normaliser_factory, events=events,
            timestamp=timestamp, cap=cap, cumulative=cumulative, smoke=smoke,
            engine_versions=engine_versions,
            fixtures_dir=(
                (config.base_dir / config.fixtures_dir).resolve()
                if config.fixtures_dir
                else None
            ),
            prompt_preamble=config.prompt_preamble,
        )
        scorecard_path = ci.artefacts.scorecard_path(run_dir)
        combo_records.append(
            aw.ComboRecord(
                combo_id=combo.combo_id, engine_id=combo.engine_id,
                model_id=combo.model_id, prompt_id=combo.prompt_id, role=combo.role,
                asset_id=(combo.asset.id if combo.asset else None),
                scorecard_path=str(scorecard_path.relative_to(matrix_dir)),
                combo_dir=str(run_dir.relative_to(matrix_dir)),
            )
        )

    summary = aw.build_matrix_summary(
        matrix_id=config.matrix_id,
        generated_at=timestamp,
        benchmark_path=str(config.benchmark),
        benchmark_id=benchmark.id,
        evaluation_type=benchmark.evaluation_type,
        pass_threshold=benchmark.pass_threshold,
        iteration=iteration,
        seeds=seeds,
        concurrency=config.concurrency,
        cache_mode=cache_mode,
        budget_usd=cap,
        cumulative_spend_usd=cumulative,
        engine_versions=engine_versions,
        models=[{"id": m.id, "engine": m.engine, "model": m.model} for m in config.models],
        prompts=[pr.id for pr in config.prompts],
        baselines=[b.id for b in config.baselines],
        combos=combo_records,
        provenance=_provenance(
            config, benchmark, engine_versions, harness_version, cache_mode, smoke
        ),
        workflow=config.workflow,
    )
    aw.write_json(ci.artefacts.matrix_summary_path(matrix_dir), summary)
    events.write(ci.artefacts.matrix_events_path(matrix_dir))
    return matrix_dir


def _write_trace(
    run_dir: pathlib.Path, seed: int, case_id: str, trace: Sequence[ci.TraceEvent]
) -> None:
    path = ci.artefacts.trace_path(run_dir, seed, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    rows = []
    for ev in trace:
        rows.append(
            json.dumps(
                {
                    "seq": ev.seq,
                    "ts_ms": ev.ts_ms,
                    "kind": ev.kind,
                    "name": ev.name,
                    "target": ev.target,
                    "args_digest": ev.args_digest,
                    "result_digest": ev.result_digest,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    path.write_text(("\n".join(rows) + "\n") if rows else "", encoding="utf-8")


def _provenance(
    config: MatrixConfig,
    benchmark: Benchmark,
    engine_versions: Mapping[str, str],
    harness_version: str,
    cache_mode: str,
    smoke: bool,
) -> dict:
    return {
        "harness_version": harness_version,
        "engine_cli_versions": dict(engine_versions),
        "judge": {
            "engine": None,
            "model": None,
            "config_digest": None,
            "disabled": smoke,
        },
        "fixture_digests": {},
        "hermetic": True,
        "exfiltration_scan_scope": "advisory",
        "repro_command": f"wololo-evals-cli matrix --config {config.matrix_id}.toml",
    }
