"""``gate``: candidate-vs-baseline A/B verdict for CI (open, Apache-2.0).

Compares a candidate matrix run (``--run``, a directory holding
``matrix_summary.json``) against a pinned baseline run (``--baseline``) and
returns a significance-gated regression verdict (README §16.4). The statistical
core lives in :mod:`wololo_evals.core.ab`; this module is the file-first reader
and presenter around it.

Gate fails (non-zero exit) when either:

* the paired-bootstrap 95% CI **upper** bound of the benchmark-score delta is
  below ``-max_regression`` (a significant regression against the
  tolerance-shifted null, ``--max-regression`` default 0), or
* any case newly trips a hard gate (§16.1) — regardless of significance.

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
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..core import ab
from ..core import interfaces as ci

__all__ = [
    "GateError",
    "GateVerdict",
    "evaluate",
    "add_arguments",
    "run",
]

# Exit codes: 0 pass, 1 regression, 2 refused (bad/uncalibrated input).
_EXIT_PASS = 0
_EXIT_REGRESSION = 1
_EXIT_REFUSED = 2

_CALIBRATION_SUBDIR = ("labels", "calibration")


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
        judge_disabled=judge_disabled,
        has_active_judge=has_active_judge,
        combos=tuple(combos),
    )


def _case_rows(sc: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    return {str(c["case_id"]): c for c in sc.get("cases", [])}


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
        }
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
) -> GateVerdict:
    """Produce the §16.4 gate verdict comparing ``run_dir`` against ``baseline_dir``.

    ``golden_agreement`` injects the judge κ directly (bypassing the calibration
    lookup) — useful for CI wiring and tests. Otherwise the newest calibration
    record under the benchmark's ``labels/calibration/`` supplies it.
    """
    run = _load_run(run_dir)
    base = _load_run(baseline_dir)
    run_p, base_p = pathlib.Path(run_dir), pathlib.Path(baseline_dir)

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
    verdict = "regression" if ab_verdict.regressed else "pass"
    reason = ""
    if ab_verdict.regressed:
        parts = []
        if ab_verdict.significant_regression:
            parts.append(
                f"CI upper bound {ab_verdict.ci_high:+.4f} < -{max_regression}"
            )
        if ab_verdict.newly_failing_hard_gates:
            parts.append(
                f"{len(ab_verdict.newly_failing_hard_gates)} newly failing hard gate(s)"
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
    parser.add_argument("-o", "--out", default=None, help="JSON verdict output path")


def run(args) -> int:  # noqa: ANN001 - argparse namespace
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
