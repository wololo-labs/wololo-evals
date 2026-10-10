"""``gate``: candidate-vs-baseline A/B verdict for CI (open, Apache-2.0).

Compares a candidate matrix run (``--run``, a directory holding
``matrix_summary.json``) against a pinned baseline run (``--baseline``) and
returns a significance-gated regression verdict (README §16.4). The statistical
core lives in :mod:`wololo_evals.core.ab`; this module is the file-first reader
and presenter around it.

Gate fails (non-zero exit) when any of:

* the paired-bootstrap 95% CI **upper** bound of the benchmark-score delta is
  below ``-max_regression`` (a significant regression against the
  tolerance-shifted null, ``--max-regression`` default 0);
* any case newly trips a hard gate (§16.1), regardless of significance;
* a signal named by ``--signal-guard`` scored lower than it did on the
  baseline. A composite benchmark score can improve while one measure that
  matters slips underneath it, so a guard lets CI pin an individual signal
  (``--signal-guard qe_flake_resistance``) without turning every signal into a
  gate.

Every signal present in both runs is reported with its baseline mean, candidate
mean and delta, whether or not it is guarded: a gate that only prints one number
cannot be argued with, and the point of the mechanical signals is that they can.

Two refusals guard the inputs (also non-zero exit, but distinct from a
regression): a run flagged ``judge_disabled`` is rejected outright, and a
benchmark whose judge is uncalibrated (no golden-set agreement record at
``κ >= judge.golden_agreement_min``) refuses judge-weighted gating rather than
gating on unvalidated numbers (§16.4 / §17). Quarantined cases are excluded
from the verdict but reported with a ``quarantined`` marker.

Output: a Markdown verdict on stdout plus a machine-readable JSON verdict file.
Pure stdlib + ``core`` — no database, no engine, no network.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import date, datetime, timezone
from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Sequence

from ..core import ab
from ..core import interfaces as ci

__all__ = [
    "GateError",
    "GateVerdict",
    "SignalDelta",
    "evaluate",
    "add_arguments",
    "run",
]

# Exit codes: 0 pass, 1 regression, 2 refused (bad/uncalibrated input).
_EXIT_PASS = 0
_EXIT_REGRESSION = 1
_EXIT_REFUSED = 2

_CALIBRATION_SUBDIR = ("labels", "calibration")

#: Float slack below which a signal delta is noise, not a regression.
_SIGNAL_EPSILON = 1e-9


class GateError(ValueError):
    """A run tree is missing an artefact the gate requires."""


# ---------------------------------------------------------------------------
# Artefact readers (tolerant of both the canonical writer schema and the
# hand-authored baseline-archive schema)
# ---------------------------------------------------------------------------
def _hard_gate_names(evaluation_type: str) -> frozenset[str]:
    gates = set(ci.STANDING_HARD_GATES)
    if evaluation_type == "robustness":
        gates.add("injection_resistance")
    return frozenset(gates)


def _case_hard_failed(signals: Sequence[Mapping[str, object]], hard: frozenset[str]) -> bool:
    return any(
        s.get("verdict") == "fail"
        and (s.get("evaluator_id") in hard or s.get("slot") in hard)
        for s in signals
    )


def _case_has_active_judge(signals: Sequence[Mapping[str, object]]) -> bool:
    return any(s.get("kind") == "judge" and s.get("verdict") != "skipped" for s in signals)


@dataclass(frozen=True)
class SignalDelta:
    """One evaluator's mean score in each run, and the gap between them.

    Means are over every (combo, case, seed) row that emitted the signal, so a
    signal a case did not emit never counts as a zero against it.
    """

    evaluator_id: str
    kind: str
    slot: Optional[str]
    baseline_mean: float
    candidate_mean: float
    baseline_n: int
    candidate_n: int
    baseline_errors: int = 0
    candidate_errors: int = 0
    guarded: bool = False

    @property
    def delta(self) -> float:
        return self.candidate_mean - self.baseline_mean

    @property
    def regressed(self) -> bool:
        return self.delta < -_SIGNAL_EPSILON

    def as_json(self) -> dict:
        return {
            "evaluator_id": self.evaluator_id,
            "kind": self.kind,
            "slot": self.slot,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "delta": self.delta,
            "baseline_n": self.baseline_n,
            "candidate_n": self.candidate_n,
            "baseline_errors": self.baseline_errors,
            "candidate_errors": self.candidate_errors,
            "guarded": self.guarded,
            "regressed": self.regressed,
        }


@dataclass(frozen=True)
class _ComboView:
    combo_id: str
    key: tuple[str, str, str]  # (engine_id, model_id, prompt_id)
    scorecard: Mapping[str, object]


@dataclass(frozen=True)
class _RunView:
    run_dir: pathlib.Path
    evaluation_type: str
    pass_threshold: float
    benchmark_path: Optional[str]
    benchmark_id: str
    benchmark_id_declared: bool
    judge_disabled: bool
    has_active_judge: bool
    combos: Sequence[_ComboView]


def _scorecard_benchmark_score(sc: Mapping[str, object]) -> float:
    summary = sc.get("summary")
    if isinstance(summary, Mapping) and "benchmark_score" in summary:
        return float(summary["benchmark_score"])
    return float(sc.get("benchmark_score", 0.0))


def _load_run(run_dir: str | pathlib.Path) -> _RunView:
    root = pathlib.Path(run_dir)
    summary_path = ci.artefacts.matrix_summary_path(root)
    if not summary_path.is_file():
        raise GateError(f"no matrix_summary.json under {root}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    evaluation_type = str(summary["evaluation_type"])
    pass_threshold = float(summary.get("pass_threshold", 0.7))
    benchmark_path = summary.get("benchmark")
    benchmark_id = str(
        summary.get("benchmark_id")
        or (pathlib.Path(str(benchmark_path)).name if benchmark_path else root.name)
    )

    combos: list[_ComboView] = []
    judge_disabled = False
    has_active_judge = False
    for combo in summary.get("combos", []):
        rel = combo.get("scorecard_path")
        if not rel:
            continue
        sc_path = root / str(rel)
        if not sc_path.is_file():
            raise GateError(f"combo {combo.get('combo_id')!r} scorecard missing: {sc_path}")
        sc = json.loads(sc_path.read_text(encoding="utf-8"))
        judge_disabled = judge_disabled or bool(sc.get("judge_disabled"))
        _check_scorecard(sc, sc_path)
        for case in sc.get("cases", []):
            if _case_has_active_judge(case.get("signals", [])):
                has_active_judge = True
        combos.append(
            _ComboView(
                combo_id=str(combo["combo_id"]),
                key=(
                    str(combo["engine_id"]),
                    str(combo["model_id"]),
                    str(combo["prompt_id"]),
                ),
                scorecard=sc,
            )
        )
    return _RunView(
        run_dir=root,
        evaluation_type=evaluation_type,
        pass_threshold=pass_threshold,
        benchmark_path=str(benchmark_path) if benchmark_path else None,
        benchmark_id=benchmark_id,
        benchmark_id_declared=bool(summary.get("benchmark_id")),
        judge_disabled=judge_disabled,
        has_active_judge=has_active_judge,
        combos=tuple(combos),
    )


def _check_scorecard(sc: Mapping[str, object], path: pathlib.Path) -> None:
    """Refuse a scorecard whose rows cannot be read unambiguously.

    A repeated case_id kept only the last row, so a failing duplicate hid
    behind a passing one; a verdict outside the known set was averaged in with
    a missing score read as 0.0.
    """
    seen: set[str] = set()
    for case in sc.get("cases", []):
        cid = str(case.get("case_id"))
        if cid in seen:
            raise GateError(f"{path}: duplicate case_id {cid!r}")
        seen.add(cid)
        for signal in case.get("signals", []):
            verdict = signal.get("verdict")
            if verdict not in ci.SIGNAL_VERDICTS:
                raise GateError(
                    f"{path}: case {cid!r} signal {signal.get('evaluator_id')!r} "
                    f"has unknown verdict {verdict!r}"
                )


def _case_rows(sc: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    return {str(c["case_id"]): c for c in sc.get("cases", [])}


@dataclass(frozen=True)
class _SignalMean:
    kind: str
    slot: Optional[str]
    mean: float
    n: int
    errors: int


def _signal_means(run: _RunView) -> dict[str, _SignalMean]:
    """``evaluator_id -> mean score`` across a whole run.

    Neither non-verdict enters the mean. A ``skipped`` judge did not run, and
    averaging it in would make the disabled path look like a regression against
    an enabled one; an ``evaluator_error`` observed nothing at all, and
    averaging its 0.0 in would let a mutmut outage on one arm read as the
    candidate getting worse. Errors are counted instead, so a signal that is
    error-only in a run is visible as ``n == 0`` rather than as a zero score.
    """
    totals: dict[str, list[float]] = {}
    errors: dict[str, int] = {}
    meta: dict[str, tuple[str, Optional[str]]] = {}
    for combo in run.combos:
        for case in combo.scorecard.get("cases", []):
            for signal in case.get("signals", []):
                sid = str(signal.get("evaluator_id"))
                verdict = signal.get("verdict")
                meta.setdefault(sid, (str(signal.get("kind", "")), signal.get("slot")))
                if verdict == "evaluator_error":
                    errors[sid] = errors.get(sid, 0) + 1
                    totals.setdefault(sid, [])
                    continue
                if verdict == "skipped":
                    totals.setdefault(sid, [])
                    continue
                totals.setdefault(sid, []).append(float(signal.get("score", 0.0)))
    return {
        sid: _SignalMean(
            kind=meta[sid][0],
            slot=meta[sid][1],
            mean=(sum(scores) / len(scores)) if scores else 0.0,
            n=len(scores),
            errors=errors.get(sid, 0),
        )
        for sid, scores in totals.items()
    }


def _signal_deltas(
    run: _RunView, base: _RunView, guards: Sequence[str]
) -> list[SignalDelta]:
    """Pair every signal both runs actually scored.

    A signal only one run emitted is left out: there is no comparison to make,
    and inventing a zero for the missing side would report a regression that
    did not happen. The same applies to a signal that emitted only
    ``evaluator_error`` or ``skipped`` rows in a run, which is why the pairing
    is on ``n > 0`` rather than on the id being present.
    """
    guarded = set(guards)
    candidate = _signal_means(run)
    baseline = _signal_means(base)
    return [
        SignalDelta(
            evaluator_id=sid,
            kind=candidate[sid].kind,
            slot=candidate[sid].slot,
            baseline_mean=baseline[sid].mean,
            candidate_mean=candidate[sid].mean,
            baseline_n=baseline[sid].n,
            candidate_n=candidate[sid].n,
            baseline_errors=baseline[sid].errors,
            candidate_errors=candidate[sid].errors,
            guarded=sid in guarded,
        )
        for sid in sorted(set(candidate) & set(baseline))
        if candidate[sid].n > 0 and baseline[sid].n > 0
    ]


# ---------------------------------------------------------------------------
# Judge calibration lookup (§17): the newest calibration record for a benchmark
# ---------------------------------------------------------------------------
def _resolve_benchmark_dir(
    run: _RunView, override: Optional[str | pathlib.Path]
) -> Optional[pathlib.Path]:
    if override is not None:
        p = pathlib.Path(override)
        return p if p.is_dir() else None
    if not run.benchmark_path:
        return None
    raw = pathlib.Path(run.benchmark_path)
    candidates = [raw]
    if not raw.is_absolute():
        candidates.extend([pathlib.Path.cwd() / raw, run.run_dir / raw])
    for c in candidates:
        if c.is_dir():
            return c
    return None


def latest_calibration_kappa(benchmark_dir: pathlib.Path) -> Optional[float]:
    """The κ of the newest ``labels/calibration/*.json`` record, or ``None``.

    Records are ordered by their ``ts`` field (falling back to filename), so the
    most recent calibration governs. A record without a numeric ``kappa`` is
    skipped.
    """
    cal_dir = benchmark_dir.joinpath(*_CALIBRATION_SUBDIR)
    if not cal_dir.is_dir():
        return None
    records: list[tuple[str, float]] = []
    for path in cal_dir.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            kappa = rec.get("kappa")
            if kappa is None:
                continue
            records.append((str(rec.get("ts", path.stem)), float(kappa)))
        except (ValueError, OSError):
            continue
    if not records:
        return None
    records.sort(key=lambda r: r[0])
    return records[-1][1]


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GateVerdict:
    """The gate outcome: a rendered verdict plus the exit code CI reads."""

    verdict: str  # "pass" | "regression" | "refused"
    reason: str
    run_dir: str
    baseline_dir: str
    evaluation_type: str
    pass_threshold: float
    max_regression: float
    combos_compared: Sequence[str]
    ab_verdict: Optional[ab.ABVerdict]
    quarantined_cases: Sequence[str]
    unmatched_cases: Sequence[str]
    judge: Mapping[str, object]
    signal_deltas: Sequence[SignalDelta] = ()
    guarded_regressions: Sequence[str] = ()
    #: Which asset this verdict is about: ``{"repo", "path", "commit"}``, all
    #: operator-declared. A verdict that does not name its subject cannot be
    #: published next to an asset, because nothing in it says which asset it
    #: measured or at which commit; a catalogue reading it could only guess.
    subject: Mapping[str, str] = field(default_factory=dict)
    #: Date the gate ran, ``YYYY-MM-DD``.
    recorded: Optional[str] = None
    #: URL of the run, pinned to a wololo-evals commit.
    source: Optional[str] = None
    #: How reachable that URL is for a reader; ``internal`` needs repo access.
    source_visibility: Optional[str] = None

    @property
    def exit_code(self) -> int:
        if self.verdict == "refused":
            return _EXIT_REFUSED
        if self.verdict == "regression":
            return _EXIT_REGRESSION
        return _EXIT_PASS

    def as_json(self) -> dict:
        v = self.ab_verdict
        payload: dict = {
            "schema_version": "1.0.0",
            "verdict": self.verdict,
            "reason": self.reason,
            "run": self.run_dir,
            "baseline": self.baseline_dir,
            "evaluation_type": self.evaluation_type,
            "pass_threshold": self.pass_threshold,
            "max_regression": self.max_regression,
            "combos_compared": list(self.combos_compared),
            "quarantined_cases": list(self.quarantined_cases),
            "unmatched_cases": list(self.unmatched_cases),
            "judge": dict(self.judge),
            "signals": [d.as_json() for d in self.signal_deltas],
            "guarded_regressions": list(self.guarded_regressions),
        }
        # Publication block: what a catalogue needs to show whose score this
        # is, when it was taken and where to read the run. Each key stays
        # absent when undeclared rather than landing as a null to be guessed at.
        if self.subject:
            payload["subject"] = dict(self.subject)
        if self.recorded:
            payload["recorded"] = self.recorded
        if self.source:
            payload["source"] = self.source
        if self.source_visibility:
            payload["sourceVisibility"] = self.source_visibility
        if v is not None:
            payload["stats"] = {
                "mean_delta": v.mean_delta,
                "ci_low": v.ci_low,
                "ci_high": v.ci_high,
                "confidence": v.confidence,
                "significant_regression": v.significant_regression,
                "significant_improvement": v.significant_improvement,
                "newly_failing_hard_gates": list(v.newly_failing_hard_gates),
                "buckets": dict(v.buckets),
                "n_cases": v.n_cases,
                "n_quarantined": v.n_quarantined,
            }
        return payload

    def as_markdown(self) -> str:
        icon = {"pass": "PASS", "regression": "REGRESSION", "refused": "REFUSED"}[
            self.verdict
        ]
        lines = [
            f"# Gate verdict: {icon}",
            "",
            f"- run: `{self.run_dir}`",
            f"- baseline: `{self.baseline_dir}`",
            f"- evaluation_type: `{self.evaluation_type}`",
            f"- max_regression: `{self.max_regression}`",
        ]
        if self.reason:
            lines += ["", f"**{self.reason}**"]
        v = self.ab_verdict
        if v is not None:
            ci_pct = int(round(v.confidence * 100))
            lines += [
                "",
                f"## Benchmark-score delta ({ci_pct}% CI)",
                "",
                f"- mean delta: `{v.mean_delta:+.4f}`",
                f"- {ci_pct}% CI: `[{v.ci_low:+.4f}, {v.ci_high:+.4f}]`",
                f"- significant regression: `{v.significant_regression}`",
                f"- significant improvement: `{v.significant_improvement}`",
                "",
                "## Four-bucket classification (§16.3)",
                "",
                "| bucket | cases |",
                "| --- | --- |",
            ]
            for bucket in ab.BUCKETS:
                lines.append(f"| {bucket} | {v.buckets.get(bucket, 0)} |")
            if v.newly_failing_hard_gates:
                lines += [
                    "",
                    "## Newly failing hard gates",
                    "",
                    *(f"- `{cid}`" for cid in v.newly_failing_hard_gates),
                ]
            if self.quarantined_cases:
                lines += [
                    "",
                    "## Quarantined (excluded from verdict)",
                    "",
                    *(f"- `{cid}`" for cid in self.quarantined_cases),
                ]
        if self.signal_deltas:
            lines += [
                "",
                "## Signals (mean score per evaluator)",
                "",
                "| signal | slot | baseline | candidate | delta | |",
                "| --- | --- | ---: | ---: | ---: | --- |",
            ]
            for d in self.signal_deltas:
                marker = "REGRESSED" if d.regressed else ""
                if d.guarded:
                    marker = ("GUARDED " + marker).strip()
                if d.baseline_errors or d.candidate_errors:
                    marker = (
                        marker + f" ({d.baseline_errors}/{d.candidate_errors} errored)"
                    ).strip()
                lines.append(
                    f"| `{d.evaluator_id}` | {d.slot or '-'} | {d.baseline_mean:.4f} "
                    f"| {d.candidate_mean:.4f} | {d.delta:+.4f} | {marker} |"
                )
        judge = self.judge
        lines += [
            "",
            "## Judge calibration (§17)",
            "",
            f"- participates: `{judge.get('participates')}`",
            f"- agreement (κ): `{judge.get('agreement')}`",
            f"- threshold: `{judge.get('agreement_min')}`",
            f"- calibrated: `{judge.get('calibrated')}`",
            "",
        ]
        return "\n".join(lines)


def _incomparable(run: _RunView, base: _RunView) -> str:
    if run.evaluation_type != base.evaluation_type:
        return (
            f"evaluation_type {run.evaluation_type!r} (run) vs "
            f"{base.evaluation_type!r} (baseline)"
        )
    if run.pass_threshold != base.pass_threshold:
        return (
            f"pass_threshold {run.pass_threshold} (run) vs "
            f"{base.pass_threshold} (baseline)"
        )
    if (
        run.benchmark_id_declared
        and base.benchmark_id_declared
        and run.benchmark_id != base.benchmark_id
    ):
        return f"benchmark {run.benchmark_id!r} (run) vs {base.benchmark_id!r} (baseline)"
    return ""


def _refused(
    run_dir: pathlib.Path,
    baseline_dir: pathlib.Path,
    run: _RunView,
    max_regression: float,
    reason: str,
    judge: Mapping[str, object],
) -> GateVerdict:
    return GateVerdict(
        verdict="refused",
        reason=reason,
        run_dir=str(run_dir),
        baseline_dir=str(baseline_dir),
        evaluation_type=run.evaluation_type,
        pass_threshold=run.pass_threshold,
        max_regression=max_regression,
        combos_compared=(),
        ab_verdict=None,
        quarantined_cases=(),
        unmatched_cases=(),
        judge=judge,
    )


def evaluate(
    run_dir: str | pathlib.Path,
    baseline_dir: str | pathlib.Path,
    *,
    max_regression: float = 0.0,
    confidence: float = 0.95,
    resamples: int = 1000,
    seed: int = 0,
    benchmark_dir: Optional[str | pathlib.Path] = None,
    golden_agreement: Optional[float] = None,
    judge_agreement_min: float = 0.7,
    signal_guards: Sequence[str] = (),
) -> GateVerdict:
    """Produce the §16.4 gate verdict comparing ``run_dir`` against ``baseline_dir``.

    ``golden_agreement`` injects the judge κ directly (bypassing the calibration
    lookup) — useful for CI wiring and tests. Otherwise the newest calibration
    record under the benchmark's ``labels/calibration/`` supplies it.
    """
    run = _load_run(run_dir)
    base = _load_run(baseline_dir)
    run_p, base_p = pathlib.Path(run_dir), pathlib.Path(baseline_dir)

    # Two runs are comparable only on one bar. Each case is classified against
    # its own run's threshold, so a mismatch changes the buckets silently.
    mismatch = _incomparable(run, base)
    if mismatch:
        return _refused(
            run_p, base_p, run, max_regression,
            f"runs are not comparable: {mismatch}",
            {"participates": False, "agreement": None,
             "agreement_min": judge_agreement_min, "calibrated": True},
        )

    # --- judge-input guards (§16.4 / §17) ---
    family = ci.GATE_FAMILY_WEIGHTS.get(run.evaluation_type, {})
    judge_weight = family.get("llm_judge_overall", 0)
    judge_participates = judge_weight > 0 and run.has_active_judge
    judge_info: dict[str, object] = {
        "participates": judge_participates,
        "agreement": None,
        "agreement_min": judge_agreement_min,
        "calibrated": not judge_participates,
    }

    if judge_participates and (run.judge_disabled or base.judge_disabled):
        return _refused(
            run_p, base_p, run, max_regression,
            "judge_disabled runs are rejected as gate inputs (§16.4)", judge_info,
        )

    if judge_participates:
        agreement = golden_agreement
        if agreement is None:
            bdir = _resolve_benchmark_dir(run, benchmark_dir)
            if bdir is not None:
                agreement = latest_calibration_kappa(bdir)
        judge_info["agreement"] = agreement
        calibrated = agreement is not None and agreement >= judge_agreement_min
        judge_info["calibrated"] = calibrated
        if not calibrated:
            got = "none" if agreement is None else f"{agreement:.3f}"
            return _refused(
                run_p, base_p, run, max_regression,
                f"judge uncalibrated: golden-set agreement κ={got} "
                f"< {judge_agreement_min} (§17); refusing judge-weighted gating",
                judge_info,
            )

    # --- pair combos on (engine, model, prompt), cases on case_id ---
    base_by_key = {c.key: c for c in base.combos}
    pairs: list[ab.CasePair] = []
    quarantined: list[str] = []
    unmatched: list[str] = []
    combos_compared: list[str] = []
    hard = _hard_gate_names(run.evaluation_type)

    for cand_combo in run.combos:
        base_combo = base_by_key.get(cand_combo.key)
        if base_combo is None:
            continue
        combos_compared.append(cand_combo.combo_id)
        cand_cases = _case_rows(cand_combo.scorecard)
        base_cases = _case_rows(base_combo.scorecard)
        for case_id, cand_case in cand_cases.items():
            base_case = base_cases.get(case_id)
            if base_case is None:
                unmatched.append(f"{cand_combo.combo_id}:{case_id}")
                continue
            is_quarantined = bool(cand_case.get("quarantined")) or bool(
                base_case.get("quarantined")
            )
            if is_quarantined:
                quarantined.append(f"{cand_combo.combo_id}:{case_id}")
            cand_score = float(cand_case.get("score", 0.0))
            base_score = float(base_case.get("score", 0.0))
            pairs.append(
                ab.CasePair(
                    case_id=f"{cand_combo.combo_id}:{case_id}",
                    baseline_score=base_score,
                    candidate_score=cand_score,
                    baseline_passed=base_score >= base.pass_threshold,
                    candidate_passed=cand_score >= run.pass_threshold,
                    baseline_hard_failed=_case_hard_failed(
                        base_case.get("signals", []), hard
                    ),
                    candidate_hard_failed=_case_hard_failed(
                        cand_case.get("signals", []), hard
                    ),
                    quarantined=is_quarantined,
                    case_weight=float(cand_case.get("case_weight", 1.0)),
                )
            )

    if not any(not p.quarantined for p in pairs):
        return _refused(
            run_p, base_p, run, max_regression,
            "no comparable (non-quarantined) cases between run and baseline",
            judge_info,
        )

    ab_verdict = ab.compare(
        pairs,
        max_regression=max_regression,
        resamples=resamples,
        confidence=confidence,
        seed=seed,
    )
    deltas = _signal_deltas(run, base, signal_guards)
    guarded_regressions = tuple(
        d.evaluator_id for d in deltas if d.guarded and d.regressed
    )
    # A guard the runs cannot support is a refusal (exit 2), never a pass and
    # never a regression. The distinction matters: "this signal did not score
    # in one of the arms" is an input problem for an operator to fix, not
    # evidence about the candidate.
    compared = {d.evaluator_id for d in deltas}
    candidate_means, baseline_means = _signal_means(run), _signal_means(base)
    unusable: list[str] = []
    for guard in signal_guards:
        if guard in compared:
            continue
        arms = [
            arm for arm, means in (("run", candidate_means), ("baseline", baseline_means))
            if means.get(guard) is not None and means[guard].errors and not means[guard].n
        ]
        if arms:
            unusable.append(f"{guard} (error-only in {', '.join(arms)})")
        else:
            unusable.append(f"{guard} (absent)")
    if unusable:
        return _refused(
            run_p, base_p, run, max_regression,
            "guarded signal(s) did not score in both runs: "
            + "; ".join(unusable)
            + "; refusing to report a guard that measured nothing",
            judge_info,
        )

    verdict = "regression" if (ab_verdict.regressed or guarded_regressions) else "pass"
    reason = ""
    if verdict == "regression":
        parts = []
        if ab_verdict.significant_regression:
            parts.append(
                f"CI upper bound {ab_verdict.ci_high:+.4f} < -{max_regression}"
            )
        if ab_verdict.newly_failing_hard_gates:
            parts.append(
                f"{len(ab_verdict.newly_failing_hard_gates)} newly failing hard gate(s)"
            )
        if guarded_regressions:
            parts.append(
                "guarded signal(s) regressed: " + ", ".join(guarded_regressions)
            )
        reason = "regression: " + "; ".join(parts)

    return GateVerdict(
        verdict=verdict,
        reason=reason,
        run_dir=str(run_p),
        baseline_dir=str(base_p),
        evaluation_type=run.evaluation_type,
        pass_threshold=run.pass_threshold,
        max_regression=max_regression,
        combos_compared=tuple(combos_compared),
        ab_verdict=ab_verdict,
        quarantined_cases=tuple(quarantined),
        unmatched_cases=tuple(unmatched),
        judge=judge_info,
        signal_deltas=tuple(deltas),
        guarded_regressions=guarded_regressions,
    )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def add_arguments(parser) -> None:  # noqa: ANN001 - argparse subparser
    parser.add_argument("--run", required=True, help="candidate matrix run dir")
    parser.add_argument("--baseline", required=True, help="baseline matrix run dir")
    parser.add_argument(
        "--max-regression", type=float, default=0.0,
        help="regression tolerance (§16.4, default 0)",
    )
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--resamples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--benchmark", default=None, help="benchmark dir override (calibration lookup)")
    parser.add_argument(
        "--golden-agreement", type=float, default=None,
        help="inject judge κ directly (skip the calibration-record lookup)",
    )
    parser.add_argument("--judge-agreement-min", type=float, default=0.7)
    parser.add_argument(
        "--signal-guard", action="append", default=[], metavar="EVALUATOR_ID",
        help=(
            "fail the gate if this signal's mean score regressed against the "
            "baseline (repeatable). Refuses when the signal is absent from "
            "either run."
        ),
    )
    parser.add_argument("-o", "--out", default=None, help="JSON verdict output path")
    parser.add_argument(
        "--subject-repo", default=None, metavar="URL",
        help="the asset repository this verdict is about (recorded in subject)",
    )
    parser.add_argument(
        "--subject-path", default=None, metavar="PATH",
        help="the asset's path inside that repository, e.g. packages/<pack>",
    )
    parser.add_argument(
        "--subject-commit", default=None, metavar="SHA",
        help="the asset commit measured, so a catalogue can show a stale score",
    )
    parser.add_argument(
        "--source", default=None, metavar="URL",
        help="URL of this run, pinned to a wololo-evals commit",
    )
    parser.add_argument(
        "--source-visibility", default=None, choices=("public", "internal"),
        help="how reachable that URL is for a reader",
    )
    parser.add_argument(
        "--recorded", default=None, metavar="YYYY-MM-DD",
        help="date the gate ran (default: today, UTC)",
    )


def validate_publication(args) -> list[str]:  # noqa: ANN001 - argparse namespace
    """Problems with the publication fields, empty when they are consistent.

    The report is published beside an asset and read as it stands, so a field
    that contradicts another is worse than an absent one: a visibility label
    with no link, a date that is not a date, or a subject naming no commit all
    used to reach disk with exit 0 and read as valid.
    """
    problems: list[str] = []
    if args.source_visibility and not args.source:
        problems.append("--source-visibility needs --source: a label for a link that is not there")
    if args.source and not re.match(r"^https?://\S+$", args.source):
        problems.append(f"--source must be an http(s) URL, got {args.source!r}")
    if args.recorded:
        valid = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", args.recorded))
        if valid:
            try:
                date.fromisoformat(args.recorded)
            except ValueError:
                valid = False
        if not valid:
            problems.append(f"--recorded must be a YYYY-MM-DD date, got {args.recorded!r}")
    named = [n for n in ("subject_repo", "subject_path", "subject_commit") if getattr(args, n)]
    if named:
        for needed in ("subject_repo", "subject_commit"):
            if not getattr(args, needed):
                flag = "--" + needed.replace("_", "-")
                problems.append(
                    f"a subject needs {flag}: without it the score cannot be tied to "
                    f"an asset or shown to have gone stale"
                )
        if args.subject_commit and not re.match(r"^[0-9a-fA-F]{7,40}$", args.subject_commit):
            problems.append(
                f"--subject-commit must be a commit hash (7 to 40 hex digits), "
                f"got {args.subject_commit!r}"
            )
    return problems


def run(args) -> int:  # noqa: ANN001 - argparse namespace
    problems = validate_publication(args)
    if problems:
        for problem in problems:
            print(f"gate: {problem}", file=sys.stderr)
        return _EXIT_REFUSED
    verdict = evaluate(
        args.run,
        args.baseline,
        max_regression=args.max_regression,
        confidence=args.confidence,
        resamples=args.resamples,
        seed=args.seed,
        benchmark_dir=args.benchmark,
        golden_agreement=args.golden_agreement,
        judge_agreement_min=args.judge_agreement_min,
        signal_guards=tuple(args.signal_guard or ()),
    )
    subject = {
        k: v
        for k, v in (
            ("repo", args.subject_repo),
            ("path", args.subject_path),
            ("commit", args.subject_commit),
        )
        if v
    }
    verdict = replace(
        verdict,
        subject=subject,
        recorded=args.recorded or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        source=args.source,
        source_visibility=args.source_visibility,
    )
    out = pathlib.Path(args.out) if args.out else pathlib.Path(args.run) / "gate_verdict.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(verdict.as_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(verdict.as_markdown())
    print(f"verdict written to {out}")
    return verdict.exit_code
