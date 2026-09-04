"""Score composition + artefact-tree writers for the file-first CLI
(open, Apache-2.0).

Two responsibilities:

* **Score composition (§16.1)** — :func:`compose_case` maps ``ScoredSignal``s
  onto family gate slots, takes the assertion-weight-weighted mean per slot,
  the slot-weight-weighted mean across slots, and zeroes on any hard-gate
  failure. Implemented verbatim to the §16.1 / wave-contracts formula, reading
  ``GATE_FAMILY_WEIGHTS`` + ``STANDING_HARD_GATES`` from ``core.interfaces``.
* **Artefact writers (§15)** — ``matrix_summary.json`` (v2.0.0, provenance
  block, §15.1), ``scorecard.json`` (v2.1.0, ``signals[]``, §15.3), and
  ``events.jsonl`` (v1.0.0 header, §15.2). All JSON is written deterministically
  so a re-run over identical inputs is byte-stable (the substrate for ``replay``
  CI self-tests).

Pure stdlib + ``core.interfaces``; no database, no engine SDK.
"""
from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..core import interfaces as ci

__all__ = [
    "ComposedCase",
    "SeedOutcome",
    "ComboRecord",
    "compose_case",
    "pass_at_k",
    "pass_hat_k",
    "build_scorecard",
    "build_matrix_summary",
    "write_json",
    "write_events",
    "EventLog",
    "SCORECARD_SCHEMA_VERSION",
    "MATRIX_SUMMARY_SCHEMA_VERSION",
    "EVENTS_SCHEMA_VERSION",
]

SCORECARD_SCHEMA_VERSION = "2.1.0"

#: Verdicts that carry no observation about the candidate, so contribute to no
#: slot mean: the evaluator was switched off, or it could not run.
_NON_VERDICTS = frozenset({"skipped", "evaluator_error"})
MATRIX_SUMMARY_SCHEMA_VERSION = "2.0.0"
EVENTS_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Score composition (§16.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ComposedCase:
    """Result of composing one (case, seed) attempt's signals into a score.

    ``evaluator_errors`` counts the signals that could not be evaluated at all.
    They are excluded from the slot means rather than folded in as zeroes, so
    the count is the only place they show up in the arithmetic; a non-zero
    value means part of this case went unmeasured and the score covers less
    than it looks like it does.
    """

    score: float
    passed: bool
    hard_gate_failed: bool
    judge_disabled: bool
    slot_scores: Mapping[str, float]
    signals: Sequence[ci.ScoredSignal]
    evaluator_errors: int = 0


def _hard_gates(evaluation_type: str) -> frozenset[str]:
    gates = set(ci.STANDING_HARD_GATES)
    if evaluation_type == "robustness":
        gates.add("injection_resistance")
    return frozenset(gates)


def compose_case(
    signals: Sequence[ci.ScoredSignal],
    evaluation_type: str,
    *,
    pass_threshold: float,
    scored: bool = True,
    judge_disabled: bool = False,
) -> ComposedCase:
    """Compose one attempt's ``signals`` into a case score (§16.1 verbatim).

    ``scored`` is ``RunResult.scored`` — a non-``ok`` run scores 0 but its
    signals are still recorded. ``judge_disabled`` forces the smoke flag on;
    it is also inferred from any ``kind == "judge"`` signal with verdict
    ``skipped`` (whose weight is then excluded from the denominators).
    """
    weights = ci.GATE_FAMILY_WEIGHTS[evaluation_type]
    hard = _hard_gates(evaluation_type)

    hard_failed = any(
        s.verdict == "fail" and (s.evaluator_id in hard or s.slot in hard)
        for s in signals
    )
    judge_off = judge_disabled or any(
        s.kind == "judge" and s.verdict == "skipped" for s in signals
    )

    # Step 1: slot score = assertion-weight-weighted mean of its evaluated
    # signals, clamped to [0, 1]. Both non-verdicts are excluded outright:
    # `skipped` (the evaluator was switched off) and `evaluator_error` (it
    # could not run). Neither is evidence about the candidate.
    evaluator_errors = sum(1 for s in signals if s.verdict == "evaluator_error")
    by_slot: dict[str, list[ci.ScoredSignal]] = {}
    for s in signals:
        if s.verdict in _NON_VERDICTS or s.slot is None or s.slot not in weights:
            continue
        by_slot.setdefault(s.slot, []).append(s)

    slot_scores: dict[str, float] = {}
    for slot, sigs in by_slot.items():
        wsum = sum(s.weight for s in sigs)
        if wsum <= 0:
            continue
        raw = sum(s.weight * s.score for s in sigs) / wsum
        slot_scores[slot] = min(1.0, max(0.0, raw))

    # Step 2: case score = sum(slot_weight * slot_score) / sum(slot_weights),
    # over slots that actually received signals; zeroed on hard-gate failure.
    denom = sum(weights[slot] for slot in slot_scores)
    if not scored or denom <= 0:
        score = 0.0
    else:
        score = sum(weights[slot] * sc for slot, sc in slot_scores.items()) / denom
    if hard_failed:
        score = 0.0

    passed = scored and not hard_failed and score >= pass_threshold
    return ComposedCase(
        score=score,
        passed=passed,
        hard_gate_failed=hard_failed,
        judge_disabled=judge_off,
        slot_scores=slot_scores,
        signals=tuple(signals),
        evaluator_errors=evaluator_errors,
    )


# ---------------------------------------------------------------------------
# pass@k / pass^k (§16.2, standard unbiased combinatorial estimators)
# ---------------------------------------------------------------------------
def pass_at_k(passes: int, n: int, k: int) -> float:
    """Probability at least one of ``k`` drawn seeds passes (non-``ok`` seeds
    already count as failures in ``passes``/``n``)."""
    if n <= 0 or k <= 0 or k > n:
        return 0.0
    fails = n - passes
    if fails < k:
        return 1.0
    return 1.0 - math.comb(fails, k) / math.comb(n, k)


def pass_hat_k(passes: int, n: int, k: int) -> float:
    """Probability all ``k`` drawn seeds pass."""
    if n <= 0 or k <= 0 or k > n or passes < k:
        return 0.0
    return math.comb(passes, k) / math.comb(n, k)


# ---------------------------------------------------------------------------
# Scorecard (§15.3, v2.1.0)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeedOutcome:
    """One (case, seed) attempt: the engine ``RunResult`` + composed score."""

    seed: int
    result: ci.RunResult
    composed: ComposedCase


def _signal_row(s: ci.ScoredSignal) -> dict:
    row: dict = {
        "evaluator_id": s.evaluator_id,
        "kind": s.kind,
        "score": s.score,
        "weight": s.weight,
        "verdict": s.verdict,
        "slot": s.slot,
    }
    if s.details is not None:
        row["details"] = dict(s.details)
    if s.evidence is not None:
        row["evidence"] = s.evidence
    return row


def _assertion_counts(signals: Sequence[ci.ScoredSignal]) -> dict:
    counts = {"pass": 0, "fail": 0, "skipped": 0, "evaluator_error": 0}
    for s in signals:
        if s.kind in ("assertion", "judge", "gate") and s.verdict in counts:
            counts[s.verdict] += 1
    return counts


def _case_row(
    case: ci.CaseSpec,
    seeds: Sequence[SeedOutcome],
    *,
    artefact_ref: str,
    baseline_deltas: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> dict:
    n = len(seeds)
    scores = [so.composed.score for so in seeds]
    passes = sum(1 for so in seeds if so.composed.passed)
    infra = sum(1 for so in seeds if so.result.status == "infra_fail")
    invalid = sum(
        1
        for so in seeds
        if any(
            sig.kind == "safety" and sig.verdict == "evaluator_error"
            for sig in so.composed.signals
        )
    )
    # Latest seed's status is a reasonable case-level status summary; all
    # non-ok statuses stay visible per-seed in the seed case files.
    statuses = [so.result.status for so in seeds]
    status = "ok" if all(s == "ok" for s in statuses) else next(
        (s for s in statuses if s != "ok"), "ok"
    )
    # signals[] flattens every seed's signals so the scorecard is self-contained.
    signals: list[dict] = []
    for so in seeds:
        signals.extend(_signal_row(sig) for sig in so.composed.signals)

    row: dict = {
        "case_id": case.case_id,
        "status": status,
        "score": (sum(scores) / n) if n else 0.0,
        "pass_rate": (passes / n) if n else 0.0,
        "invalid_rate": (invalid / n) if n else 0.0,
        "infra_rate": (infra / n) if n else 0.0,
        "attempts": sum(so.result.attempts for so in seeds),
        # Signals that could not be evaluated. They score nothing either way,
        # so a non-zero count here is the only warning that this case's score
        # covers less than the full set of checks it declared.
        "evaluator_errors": sum(so.composed.evaluator_errors for so in seeds),
        "case_weight": case.case_weight,
        "quarantined": case.quarantined,
        "assertion_counts": _assertion_counts(
            [sig for so in seeds for sig in so.composed.signals]
        ),
        "artefact_ref": artefact_ref,
        "signals": signals,
    }
    if baseline_deltas:
        row["baseline_deltas"] = {k: dict(v) for k, v in baseline_deltas.items()}
    return row


@dataclass(frozen=True)
class ComboRecord:
    """One combo's identity + artefact location, for ``matrix_summary.json``."""

    combo_id: str
    engine_id: str
    model_id: str
    prompt_id: str
    role: str
    scorecard_path: str
    combo_dir: str
    asset_id: Optional[str] = None


def build_scorecard(
    combo: ComboRecord,
    benchmark_id: str,
    *,
    seeds_configured: int,
    case_specs: Sequence[ci.CaseSpec],
    outcomes: Mapping[str, Sequence[SeedOutcome]],
    artefact_refs: Mapping[str, str],
    judge_disabled: bool = False,
    self_judged: bool = False,
) -> dict:
    """Build a §15.3 v2.1.0 scorecard dict for one combo.

    ``outcomes`` maps ``case_id`` to its per-seed :class:`SeedOutcome`s;
    ``case_specs`` is every configured case (a case with no outcomes still
    gets a zero row so the scorecard covers the whole benchmark).
    """
    case_rows: list[dict] = []
    for case in case_specs:
        seeds = list(outcomes.get(case.case_id, ()))
        case_rows.append(
            _case_row(
                case,
                seeds,
                artefact_ref=artefact_refs.get(case.case_id, ""),
            )
        )

    # Aggregate metrics — case-weight-weighted where §16.1/§16.2 require it,
    # excluding quarantined cases from gate-facing aggregates.
    gating = [
        (case, row)
        for case, row in zip(case_specs, case_rows)
        if not case.quarantined
    ]
    wsum = sum(case.case_weight for case, _ in gating)
    if wsum > 0:
        benchmark_score = sum(case.case_weight * row["score"] for case, row in gating) / wsum
        pass_rate = sum(case.case_weight * row["pass_rate"] for case, row in gating) / wsum
        invalid_rate = (
            sum(case.case_weight * row["invalid_rate"] for case, row in gating) / wsum
        )
        infra_rate = sum(case.case_weight * row["infra_rate"] for case, row in gating) / wsum
    else:
        benchmark_score = pass_rate = invalid_rate = infra_rate = 0.0

    all_seeds = [so for seeds in outcomes.values() for so in seeds]
    total_cost = sum(so.result.cost_usd for so in all_seeds)
    total_tokens_in = sum(so.result.tokens_in for so in all_seeds)
    total_tokens_out = sum(so.result.tokens_out for so in all_seeds)
    runtime = sum(so.result.runtime_seconds for so in all_seeds)

    ks = list(range(1, max(1, seeds_configured) + 1))
    pak: dict[str, float] = {}
    phk: dict[str, float] = {}
    for k in ks:
        num_at = num_hat = 0.0
        for case, _row in gating:
            seeds = list(outcomes.get(case.case_id, ()))
            n = seeds_configured
            p = sum(1 for so in seeds if so.composed.passed)
            num_at += case.case_weight * pass_at_k(p, n, k)
            num_hat += case.case_weight * pass_hat_k(p, n, k)
        pak[str(k)] = (num_at / wsum) if wsum > 0 else 0.0
        phk[str(k)] = (num_hat / wsum) if wsum > 0 else 0.0

    scorecard: dict = {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "combo": {
            "combo_id": combo.combo_id,
            "engine_id": combo.engine_id,
            "model_id": combo.model_id,
            "prompt_id": combo.prompt_id,
            "asset_id": combo.asset_id,
            "role": combo.role,
        },
        "summary": {
            "benchmark_score": benchmark_score,
            "pass_rate": pass_rate,
            "invalid_rate": invalid_rate,
            "infra_rate": infra_rate,
            "runtime_seconds": runtime,
            "cost_usd": total_cost,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "pass_at_k": pak,
            "pass_hat_k": phk,
            "seeds": seeds_configured,
        },
        "judge_disabled": judge_disabled,
        "self_judged": self_judged,
        "cases": case_rows,
    }
    return scorecard


# ---------------------------------------------------------------------------
# matrix_summary.json (§15.1, v2.0.0)
# ---------------------------------------------------------------------------
def build_matrix_summary(
    *,
    matrix_id: str,
    generated_at: str,
    benchmark_path: str,
    benchmark_id: str,
    evaluation_type: str,
    pass_threshold: float,
    iteration: int,
    seeds: int,
    concurrency: int,
    cache_mode: str,
    budget_usd: Optional[float],
    cumulative_spend_usd: float,
    engine_versions: Mapping[str, str],
    models: Sequence[Mapping[str, str]],
    prompts: Sequence[str],
    baselines: Sequence[str],
    combos: Sequence[ComboRecord],
    provenance: Mapping[str, object],
    workflow: Optional[Mapping[str, object]] = None,
    snapshot_dir: Optional[str] = None,
) -> dict:
    """Build the §15.1 top-level matrix summary (the entry point downstream
    commands read). ``provenance`` carries the §15.1 provenance block."""
    return {
        "schema_version": MATRIX_SUMMARY_SCHEMA_VERSION,
        "matrix_id": matrix_id,
        "generated_at": generated_at,
        "benchmark": benchmark_path,
        "benchmark_id": benchmark_id,
        "evaluation_type": evaluation_type,
        "pass_threshold": pass_threshold,
        "workflow": dict(workflow or {}),
        "iteration": iteration,
        "snapshot_dir": snapshot_dir,
        "engines": dict(engine_versions),
        "models": [dict(m) for m in models],
        "prompts": list(prompts),
        "baselines": list(baselines),
        "seeds": seeds,
        "concurrency": concurrency,
        "cache_mode": cache_mode,
        "budget_usd": budget_usd,
        "cumulative_spend_usd": cumulative_spend_usd,
        "provenance": dict(provenance),
        "combos": [
            {
                "combo_id": c.combo_id,
                "engine_id": c.engine_id,
                "model_id": c.model_id,
                "prompt_id": c.prompt_id,
                "asset_id": c.asset_id,
                "role": c.role,
                "scorecard_path": c.scorecard_path,
                "combo_dir": c.combo_dir,
            }
            for c in combos
        ],
    }


# ---------------------------------------------------------------------------
# events.jsonl (§15.2, v1.0.0 header)
# ---------------------------------------------------------------------------
@dataclass
class EventLog:
    """Accumulates §15.2 wide events; ``write`` emits header + one row/line."""

    events: list[dict] = field(default_factory=list)

    def record(
        self,
        event: str,
        *,
        ts: str,
        run_id: str = "",
        combo_id: str = "",
        case_id: str = "",
        seed: int = 0,
        status: str = "",
        cost_usd: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        duration_ms: int = 0,
        detail: Optional[Mapping[str, object]] = None,
    ) -> None:
        self.events.append(
            {
                "ts": ts,
                "event": event,
                "run_id": run_id,
                "combo_id": combo_id,
                "case_id": case_id,
                "seed": seed,
                "status": status,
                "cost_usd": cost_usd,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "duration_ms": duration_ms,
                "detail": dict(detail or {}),
            }
        )

    def write(self, path: pathlib.Path, *, filter_combo: Optional[str] = None) -> None:
        rows = [
            e
            for e in self.events
            if filter_combo is None or e["combo_id"] == filter_combo
        ]
        write_events(path, rows)


def write_events(path: str | pathlib.Path, events: Sequence[Mapping[str, object]]) -> None:
    """Write a §15.2 ``events.jsonl``: a header line then one compact row/line."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"schema_version": EVENTS_SCHEMA_VERSION}, ensure_ascii=False)]
    for e in events:
        lines.append(json.dumps(e, ensure_ascii=False, separators=(",", ":")))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path: str | pathlib.Path, obj: object) -> None:
    """Deterministic pretty JSON write (byte-stable for identical input)."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
