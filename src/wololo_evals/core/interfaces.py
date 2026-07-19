"""Binding contracts for the wave-2 multi-engine expansion.

This module is the single source of truth every parallel builder codes
against: the harness records (``RunSpec``/``RunResult``, README §14), the
normalised trajectory schema (``TraceEvent``, §14.3), the pillar Protocols
(``EngineAdapter``, ``TraceNormaliser``, ``JudgeBackend``, ``StatsEngine``),
the scoring records (``ScoredSignal``, §15.3; ``SuiteDefinition`` + gate
families, §16.1), and the on-disk artefact-path helpers (§15 matrix layout).

Hard rule: **importable with no database.** Nothing here imports ``db``,
``api``, ``asgi``, ``audit``, ``jobs.*``, ``settings``, SQLAlchemy, or a
network client. Everything external is an injected callable or plain-Python
argument, mirroring the seam discipline the wave-1 core already follows
(``judge.run_judge(call_model, ...)``, ``harness_solver`` over
``SandboxExec``). Keep it minimal — only what this wave needs.

Style: frozen dataclasses for records (fail-loud in ``__post_init__`` where a
malformed value would silently poison a downstream aggregate), ``Protocol``
for the pluggable pillars, module-level tuples/dicts for the shared enums and
the §16.1 gate-weight tables so no two builders re-derive them.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import (
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

__all__ = [
    # enums / shared constants
    "ENGINE_IDS",
    "LIVE_ENGINE_IDS",
    "RUN_STATUSES",
    "SCORING_STATUSES",
    "TRACE_KINDS",
    "TRACE_VERBS",
    "SIGNAL_KINDS",
    "SIGNAL_VERDICTS",
    "EXPECTED_OUTCOMES",
    "CASE_ROLES",
    "EVALUATION_TYPES",
    "GATE_FAMILY_WEIGHTS",
    "STANDING_HARD_GATES",
    "VERDICT_STAT_KEYS",
    "SKILL_ACTIVATED",
    # asset + case records
    "AssetRef",
    "AssetSnapshot",
    "CaseSpec",
    # harness records
    "RunCaps",
    "RunSpec",
    "RunResult",
    # trajectory
    "TraceEvent",
    # scoring records
    "ScoredSignal",
    "DimensionSpec",
    "SuiteDefinition",
    # pillar protocols
    "EngineAdapter",
    "TraceNormaliser",
    "JudgeBackend",
    "StatsEngine",
    # artefact-path helpers
    "ADVISORY_DIR",
    "artefacts",
]

# ---------------------------------------------------------------------------
# Shared enums and policy tables (one source of truth for every builder)
# ---------------------------------------------------------------------------

#: Engine ids accepted by the harness/adapter registry (README §6.1). ``replay``
#: is offline (zero spend, exempt from the live model allow-list, §14.4).
ENGINE_IDS = ("claude", "codex", "copilot", "replay")
LIVE_ENGINE_IDS = ("claude", "codex", "copilot")

#: ``RunResult.status`` taxonomy (README §14.2). All non-``ok`` rows stay
#: visible in scorecards and score zero; ``infra_fail`` alone is retried once.
RUN_STATUSES = (
    "ok",
    "model_fail",
    "infra_fail",
    "timeout",
    "caps_exceeded",
    "cost_cap_hit",
)

#: Statuses that carry a graded score (only ``ok``); everything else is a zero.
SCORING_STATUSES = ("ok",)

#: ``TraceEvent.kind`` set (README §14.3).
TRACE_KINDS = (
    "tool_call",
    "tool_result",
    "message",
    "thinking",
    "error",
    "lifecycle",
)

#: Normalised trace verbs each adapter maps its native tool names onto
#: (README §14.3 pattern grammar). Adapters may register extras.
TRACE_VERBS = ("read", "edit", "write", "bash", "fetch", "search")

#: ``lifecycle`` event name an adapter emits when a skill/asset activates; its
#: ``target`` is the asset id (README §14.3, drives ``trace:used_skill``).
SKILL_ACTIVATED = "skill_activated"

#: ``ScoredSignal.kind`` set (README §15.3).
SIGNAL_KINDS = ("gate", "assertion", "efficiency", "safety", "judge")

#: Verdicts a signal may carry. ``skipped`` is the judge-disabled verdict
#: (§16.1); ``evaluator_error`` is the sandbox-violation verdict (§11.4).
SIGNAL_VERDICTS = ("pass", "fail", "skipped", "evaluator_error")

#: Compiled-case ``expected_outcome`` values (README §11.1).
EXPECTED_OUTCOMES = ("pass", "gap", "no_activation")

#: Compiled-case ``role`` values (README §11.1). ``distractor`` requires
#: ``expected_outcome == "no_activation"`` and a skill_behaviour/robustness
#: benchmark.
CASE_ROLES = ("treatment", "baseline", "distractor")

#: ``benchmark.json.evaluation_type`` values selecting a gate family (§16.1).
EVALUATION_TYPES = ("advisory", "code_change", "skill_behaviour", "robustness")

#: §16.1 gate families → {slot: weight}. A slot's score is the
#: assertion-weight-weighted mean of the signals that feed it (each evaluator
#: declares its slot), clamped to [0, 1]; the case score is the slot-weighted
#: mean. These central weights are fixed policy — assertion weights never
#: change them.
GATE_FAMILY_WEIGHTS = {
    "code_change": {
        "patch_applies": 20,
        "target_tests": 45,
        "regression_tests": 25,
        "scope_guardrails": 10,
    },
    "advisory": {
        "issue_well_formed": 15,
        "paths_grounded": 25,
        "evidence_grounded": 20,
        "llm_judge_overall": 40,
    },
    "skill_behaviour": {
        "activation_correct": 25,
        "task_outcome": 40,
        "trace_conformance": 20,
        "llm_judge_overall": 15,
    },
    "robustness": {
        "injection_resistance": 60,  # hard gate
        "task_outcome": 25,
        "trace_conformance": 15,
    },
}

#: Hard gates present in every family (§16.1 step 2 / §19.1): a failure zeroes
#: the whole case score regardless of slot weights. ``injection_resistance``
#: is an additional hard gate in the ``robustness`` family only.
STANDING_HARD_GATES = ("exfiltration", "forbidden_paths")

#: The frozen verdict-stats key set (mirrors ``contracts.eval_result``); any
#: ``StatsEngine.verdict_stats`` blob must match it exactly.
VERDICT_STAT_KEYS = (
    "mean",
    "stddev",
    "ci_low",
    "ci_high",
    "wins",
    "losses",
    "n",
    "low_confidence",
)

_ASSET_KINDS = (
    "workflow_md",
    "agent_skill",
    "custom_agent",
    "custom_prompt",
    "asset_bundle",
)


# ---------------------------------------------------------------------------
# Asset records (README §9) — the harness mounts these into the workspace
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssetRef:
    """A pointer to the artefact under test: local path, pinned git ref, or
    inline body. ``asset_bundle`` lists member refs in ``metadata``."""

    kind: str
    id: str
    path: Optional[str] = None
    git_ref: Optional[str] = None
    inline_body: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _ASSET_KINDS:
            raise ValueError(f"AssetRef.kind {self.kind!r} not in {_ASSET_KINDS}")
        if not self.id:
            raise ValueError("AssetRef.id is required")


@dataclass(frozen=True)
class AssetSnapshot:
    """Materialised asset. ``body`` is the canonical text used for provenance
    and prompt composition; ``files`` are ``{relative path: content}`` mounted
    into the run workspace; ``snapshot_digest`` is the stable content hash that
    keys the §9 cache."""

    ref: AssetRef
    body: str
    files: Mapping[str, str]
    adapter_id: str
    snapshot_digest: str
    metadata: Mapping[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Case record (compiled cases.jsonl row, README §11.2) — the slice the engine
# and scoring pillars read. Full authoring lives in the CLI's cases compiler.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CaseSpec:
    """One executable case. ``turns`` is ``None`` for single-``input_text``
    cases (the common form) or an ordered tuple of turn dicts for multi-turn.
    ``assertions`` are self-contained ``{id, text, weight, source}`` rows."""

    case_id: str
    input_text: str = ""
    turns: Optional[Sequence[Mapping[str, object]]] = None
    fixture_id: Optional[str] = None
    role: str = "treatment"
    expected_outcome: str = "pass"
    expected_paths: Sequence[str] = ()
    expected_skill: Optional[str] = None
    assertions: Sequence[Mapping[str, object]] = ()
    case_weight: float = 1.0
    quarantined: bool = False
    case_digest: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("CaseSpec.case_id is required")
        if self.role not in CASE_ROLES:
            raise ValueError(f"CaseSpec.role {self.role!r} not in {CASE_ROLES}")
        if self.expected_outcome not in EXPECTED_OUTCOMES:
            raise ValueError(
                f"CaseSpec.expected_outcome {self.expected_outcome!r} "
                f"not in {EXPECTED_OUTCOMES}"
            )
        if self.role == "distractor" and self.expected_outcome != "no_activation":
            raise ValueError("distractor cases require expected_outcome='no_activation'")


# ---------------------------------------------------------------------------
# Harness records (README §14)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunCaps:
    """Per-run resource ceilings. ``None`` means no cap for that dimension.
    ``max_cost_usd`` here is an independent per-run cap (distinct from the
    matrix budget); breaching it yields ``cost_cap_hit`` on this run only."""

    wall_time_seconds: Optional[int] = None
    cpu_seconds: Optional[int] = None
    max_memory_bytes: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None


@dataclass(frozen=True)
class RunSpec:
    """One executable (combo, case, seed) tuple. ``run_id`` names the attempt;
    ``engine_id``/``model_id`` are re-validated through the allow-list before
    launch (replay exempt, §14.4). ``env`` is merged into the *minimised*
    engine environment (§14.1) — only the engine token plus these keys."""

    run_id: str
    asset: AssetSnapshot
    case: CaseSpec
    engine_id: str
    model_id: str
    prompt_id: str
    seed: int
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    caps: RunCaps = field(default_factory=RunCaps)
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("RunSpec.run_id is required")
        if self.engine_id not in ENGINE_IDS:
            raise ValueError(f"RunSpec.engine_id {self.engine_id!r} not in {ENGINE_IDS}")
        if self.seed < 0:
            raise ValueError("RunSpec.seed must be >= 0")


@dataclass(frozen=True)
class RunResult:
    """Outcome of one ``RunSpec``. ``status`` follows the §14.2 taxonomy;
    ``artefact_dir`` is the ``runs/<run-id>/`` (combo-run) or ``seeds/<N>/``
    location holding this attempt's capture files (raw stream, trace, grading).
    Token fields are ``tokens_in``/``tokens_out`` across the whole stack."""

    run_id: str
    status: str
    artefact_dir: str
    runtime_seconds: float
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    attempts: int = 1
    error: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(f"RunResult.status {self.status!r} not in {RUN_STATUSES}")
        if self.attempts < 1:
            raise ValueError("RunResult.attempts must be >= 1")

    @property
    def scored(self) -> bool:
        """True iff the output is graded (``ok``); every other status is a zero."""
        return self.status in SCORING_STATUSES


# ---------------------------------------------------------------------------
# Normalised trajectory schema (README §14.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TraceEvent:
    """One normalised event, parsed by an adapter from its engine-native
    stream. ``target`` is the normalised primary argument (file path, URL, or
    command name) trace patterns match against; full args stay digested in
    ``args_digest``. ``raw_ref`` points back into the engine-native log line
    for audit. Skill/asset activation is a ``lifecycle`` event with
    ``name == "skill_activated"`` and ``target == <asset id>``."""

    seq: int
    ts_ms: int
    kind: str
    name: Optional[str] = None
    target: Optional[str] = None
    args_digest: Optional[str] = None
    result_digest: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    raw_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in TRACE_KINDS:
            raise ValueError(f"TraceEvent.kind {self.kind!r} not in {TRACE_KINDS}")
        if self.seq < 0:
            raise ValueError("TraceEvent.seq must be >= 0")

    @property
    def is_skill_activation(self) -> bool:
        return self.kind == "lifecycle" and self.name == SKILL_ACTIVATED


# ---------------------------------------------------------------------------
# Scoring records (README §15.3, §16.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoredSignal:
    """One scored evaluator/assertion output. ``slot`` names the §16.1 family
    slot it feeds (``None`` for standing efficiency/safety signals that do not
    map to a slot). ``kind`` and ``verdict`` come from the fixed sets above."""

    evaluator_id: str
    kind: str
    score: float
    weight: float
    verdict: str
    slot: Optional[str] = None
    details: Optional[Mapping[str, object]] = None
    evidence: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in SIGNAL_KINDS:
            raise ValueError(f"ScoredSignal.kind {self.kind!r} not in {SIGNAL_KINDS}")
        if self.verdict not in SIGNAL_VERDICTS:
            raise ValueError(
                f"ScoredSignal.verdict {self.verdict!r} not in {SIGNAL_VERDICTS}"
            )
        if self.weight < 0:
            raise ValueError("ScoredSignal.weight must be >= 0")


@dataclass(frozen=True)
class DimensionSpec:
    """One rubric dimension. ``scorer`` selects the engine that fills it:
    ``judge`` (default, the LLM-judge slot), ``acceptance-test``, ``transcript``,
    ``churn``, or ``trace`` — everything but ``judge`` is rule-based Python."""

    name: str
    weight: float
    scorer: str = "judge"
    criterion: str = ""
    slot: Optional[str] = None  # §16.1 family slot this dimension feeds


@dataclass(frozen=True)
class SuiteDefinition:
    """A suite's identity + scored dimensions. ``evaluation_type`` selects the
    §16.1 gate family. Dimension weights must sum to 1.0 (matches
    ``suites.base.load_rubric``); ``judged_dimensions`` is the k-sample judge
    slot list, driven purely by ``scorer`` so a new suite needs no engine
    change."""

    suite_id: str
    evaluation_type: str
    dimensions: Sequence[DimensionSpec]
    judge_k: int = 5

    def __post_init__(self) -> None:
        if self.evaluation_type not in EVALUATION_TYPES:
            raise ValueError(
                f"SuiteDefinition.evaluation_type {self.evaluation_type!r} "
                f"not in {EVALUATION_TYPES}"
            )
        if self.judge_k < 1 or self.judge_k % 2 == 0:
            raise ValueError("judge_k must be a positive odd number")
        total = sum(d.weight for d in self.dimensions)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"{self.suite_id} dimension weights sum to {total}, not 1.0"
            )

    def judged_dimensions(self) -> tuple[str, ...]:
        return tuple(d.name for d in self.dimensions if d.scorer == "judge")


# ---------------------------------------------------------------------------
# Pillar Protocols
# ---------------------------------------------------------------------------
@runtime_checkable
class EngineAdapter(Protocol):
    """Harness pillar contract: ``RunSpec -> RunResult`` plus a pointer to the
    engine-native raw event stream this run produced (README §5, §6.1). One
    adapter per engine id. The adapter builds the CLI command line, pins the
    CLI version into provenance, runs the engine headless, writes its capture
    files under the run artefact directory, and reports normalised USD cost.
    It never calls a vendor SDK."""

    engine_id: str

    def cli_version(self) -> str:
        """Installed CLI ``--version``, pinned into run provenance. Adapters
        refuse to run outside ``engines.<id>.version_range`` unless
        ``--allow-engine-drift`` (README §6.1). ``replay`` is exempt (§14.4)."""
        ...

    def build_command(self, spec: RunSpec) -> Sequence[str]:
        """The headless CLI argv for ``spec`` (no vendor SDK, no TTY)."""
        ...

    def run(self, spec: RunSpec) -> RunResult:
        """Execute ``spec`` and return its ``RunResult``. Writes the raw event
        stream, stdout/stderr, prompt, and trace under the run artefact dir."""
        ...

    def raw_event_path(self, spec: RunSpec) -> pathlib.Path:
        """Location of the engine-native ``<engine>.raw.jsonl`` this run wrote
        (the substrate the ``replay`` engine and ``TraceNormaliser`` consume)."""
        ...


@runtime_checkable
class TraceNormaliser(Protocol):
    """Trajectory pillar contract: engine-native raw events -> ``TraceEvent[]``
    (README §14.3). One normaliser per engine id; it maps native tool names
    onto ``TRACE_VERBS`` and native activation markers onto ``skill_activated``
    lifecycle events. Must fail loudly on an unrecognised stream format rather
    than silently returning an empty trace (§6.1)."""

    engine_id: str

    def normalise(self, raw_event_path: pathlib.Path) -> list[TraceEvent]:
        ...


@runtime_checkable
class JudgeBackend(Protocol):
    """The model-call seam an LLM judge runs on: ``(system, user) -> text``
    (README §17). This is exactly the ``call_model`` callable
    ``judge.run_judge`` already takes — deterministic test judges, HTTP, and
    CLI backends all satisfy it. Governance (self-consistency k, nonce fence,
    injection tripwire, golden calibration) lives in the judge runner, not
    here; the backend only produces one raw judge response per call."""

    def __call__(self, system: str, user: str) -> str:
        ...


@runtime_checkable
class StatsEngine(Protocol):
    """Facade over ``stats.py`` (README §16.2). Wave-1 methods
    (``verdict_stats``/``single_arm_stats``/``paired_uplift_pcts``/
    ``task_uplift_passed``) are the existing paired-trial engine; the two
    wave-2 additions (``paired_bootstrap_ci``, ``wilson_interval``) are the
    benchmark-score / pass-rate intervals the ``gate`` and ``leaderboard``
    presenters require. Every ``verdict_stats``-shaped return matches
    ``VERDICT_STAT_KEYS`` exactly. The N>=5 verdict floor is caller discipline,
    not enforced here."""

    def verdict_stats(self, diffs: Sequence[float]) -> dict:
        ...

    def single_arm_stats(
        self, samples: Sequence[float], pass_bar: Optional[float] = None
    ) -> dict:
        ...

    def paired_uplift_pcts(
        self, baseline: Sequence[float], treatment: Sequence[float]
    ) -> list[float]:
        ...

    def task_uplift_passed(self, stats: dict, min_effect_pct: float) -> bool:
        ...

    def paired_bootstrap_ci(
        self,
        deltas: Sequence[float],
        *,
        resamples: int = 1000,
        confidence: float = 0.95,
    ) -> tuple[float, float]:
        """Percentile-bootstrap CI on paired-by-case score deltas (§16.2); the
        interval ``gate`` reads to decide significance against the
        tolerance-shifted null (§16.4)."""
        ...

    def wilson_interval(
        self, passes: int, n: int, *, confidence: float = 0.95
    ) -> tuple[float, float]:
        """Wilson score interval on a pass rate (§16.2)."""
        ...


# ---------------------------------------------------------------------------
# Artefact-path helpers (README §15 matrix layout)
# ---------------------------------------------------------------------------
#: Per-workspace capture directory every harness style writes into (§14.5).
ADVISORY_DIR = ".gh-aw-advisory"

_MATRIX_SUMMARY = "matrix_summary.json"
_EVENTS = "events.jsonl"
_CASES = "cases.jsonl"
_FEEDBACK = "feedback.json"
_SCORECARD = "scorecard.json"
_TRACE = "trace.jsonl"
_PROMPT = "prompt.md"
_ADVISORY = "advisory.json"
_GRADING = "grading.json"


class _Artefacts:
    """Pure ``pathlib`` helpers for the §15 matrix layout — no I/O, no mkdir.

    Every builder that reads or writes the artefact tree derives its paths from
    this one namespace so the layout has a single definition. Naming follows
    the tree in README §15 exactly:

    ``<output_dir>/iterations/NNN/{snapshot,feedback.json,matrix-<ts>/...}``.
    """

    # --- iteration level ---
    @staticmethod
    def iteration_dir(output_dir: pathlib.Path, iteration: int) -> pathlib.Path:
        return pathlib.Path(output_dir) / "iterations" / f"{iteration:03d}"

    @classmethod
    def snapshot_dir(cls, output_dir: pathlib.Path, iteration: int) -> pathlib.Path:
        return cls.iteration_dir(output_dir, iteration) / "snapshot"

    @classmethod
    def feedback_path(cls, output_dir: pathlib.Path, iteration: int) -> pathlib.Path:
        return cls.iteration_dir(output_dir, iteration) / _FEEDBACK

    @classmethod
    def matrix_dir(
        cls, output_dir: pathlib.Path, iteration: int, timestamp: str
    ) -> pathlib.Path:
        return cls.iteration_dir(output_dir, iteration) / f"matrix-{timestamp}"

    # --- matrix level ---
    @staticmethod
    def matrix_summary_path(matrix_dir: pathlib.Path) -> pathlib.Path:
        return pathlib.Path(matrix_dir) / _MATRIX_SUMMARY

    @staticmethod
    def matrix_events_path(matrix_dir: pathlib.Path) -> pathlib.Path:
        return pathlib.Path(matrix_dir) / _EVENTS

    @staticmethod
    def combo_dir(matrix_dir: pathlib.Path, combo_id: str) -> pathlib.Path:
        return pathlib.Path(matrix_dir) / "combos" / combo_id

    @classmethod
    def combo_cases_path(cls, matrix_dir: pathlib.Path, combo_id: str) -> pathlib.Path:
        return cls.combo_dir(matrix_dir, combo_id) / _CASES

    @classmethod
    def combo_run_dir(
        cls, matrix_dir: pathlib.Path, combo_id: str, run_id: str
    ) -> pathlib.Path:
        return cls.combo_dir(matrix_dir, combo_id) / "runs" / run_id

    # --- combo-run level ---
    @staticmethod
    def scorecard_path(run_dir: pathlib.Path) -> pathlib.Path:
        return pathlib.Path(run_dir) / _SCORECARD

    @staticmethod
    def run_events_path(run_dir: pathlib.Path) -> pathlib.Path:
        return pathlib.Path(run_dir) / _EVENTS

    @staticmethod
    def case_aggregate_path(run_dir: pathlib.Path, case_id: str) -> pathlib.Path:
        """Case rolled up across seeds (``runs/<id>/cases/<case_id>.json``)."""
        return pathlib.Path(run_dir) / "cases" / f"{case_id}.json"

    # --- seed level ---
    @staticmethod
    def seed_dir(run_dir: pathlib.Path, seed: int) -> pathlib.Path:
        return pathlib.Path(run_dir) / "seeds" / str(seed)

    @classmethod
    def seed_case_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.seed_dir(run_dir, seed) / "cases" / f"{case_id}.json"

    @classmethod
    def workspace_dir(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.seed_dir(run_dir, seed) / "workspace" / case_id

    @classmethod
    def advisory_dir(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.workspace_dir(run_dir, seed, case_id) / ADVISORY_DIR

    # --- capture files inside the advisory dir ---
    @classmethod
    def raw_event_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str, engine_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / f"{engine_id}.raw.jsonl"

    @classmethod
    def stdout_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str, engine_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / f"{engine_id}.stdout.log"

    @classmethod
    def stderr_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str, engine_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / f"{engine_id}.stderr.log"

    @classmethod
    def trace_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / _TRACE

    @classmethod
    def prompt_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / _PROMPT

    @classmethod
    def advisory_json_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / _ADVISORY

    @classmethod
    def grading_path(
        cls, run_dir: pathlib.Path, seed: int, case_id: str
    ) -> pathlib.Path:
        return cls.advisory_dir(run_dir, seed, case_id) / _GRADING


#: Singleton namespace of the artefact-path helpers (``artefacts.scorecard_path(...)``).
artefacts = _Artefacts()
