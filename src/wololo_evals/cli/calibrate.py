"""``judge calibrate``: golden-set agreement for the LLM judge (open, Apache-2.0).

Runs the configured judge over a benchmark's golden set — the human-labelled
ground truth built by ``label`` — and reports Cohen's kappa between judge and
human case verdicts (README §17). Below ``judge.golden_agreement_min``
calibration fails (non-zero exit), which is exactly the condition ``gate``
reads before allowing judge-weighted gating (§16.4).

``--dry-run`` validates the judge config against the engine allow-list and the
golden set against its parser, prints the planned sampling matrix, and makes
**zero** model calls — the pre-flight before spending on a real calibration.

The calibration math (:func:`cohens_kappa`, :func:`run_calibration`) is a pure
function of an injected judge backend (the same ``call_model`` seam
``judge.run_judge`` takes), so it is fully testable with a deterministic judge
and no network. Timestamped records are written under
``benchmarks/<id>/labels/calibration/`` for drift tracking.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import statistics
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from ..core import interfaces as ci
from ..judge import run_judge
from .config import load_benchmark, load_matrix_config

__all__ = [
    "CalibrationError",
    "CalibrationItem",
    "CalibrationResult",
    "cohens_kappa",
    "run_calibration",
    "load_golden_verdicts",
    "add_arguments",
    "run",
]

_VERDICTS = ("pass", "fail")
_DEFAULT_RUBRIC = (
    "Judge whether the candidate correctly and completely accomplishes the "
    "task under evaluation. Score 'overall' in [0,1]: 1.0 for a fully correct "
    "outcome, 0.0 for a clear failure."
)


class CalibrationError(ValueError):
    """Config or golden-set validation failed (a fail-closed pre-flight error)."""


# ---------------------------------------------------------------------------
# Cohen's kappa (binary pass/fail agreement)
# ---------------------------------------------------------------------------
def cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa between two aligned verdict sequences.

    ``a``/``b`` are equal-length sequences of ``"pass"``/``"fail"``. Returns 1.0
    on perfect agreement even in the degenerate single-category case (where the
    expected-agreement correction is undefined), 0.0 when observed agreement
    collapses to chance.
    """
    n = len(a)
    if n == 0 or n != len(b):
        raise CalibrationError(f"cannot compute kappa over {n} vs {len(b)} verdicts")
    categories = set(a) | set(b)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = 0.0
    for cat in categories:
        pa = sum(1 for x in a if x == cat) / n
        pb = sum(1 for y in b if y == cat) / n
        pe += pa * pb
    if pe >= 1.0:
        # One category on both raters: agreement is trivial; perfect iff observed.
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1.0 - pe)


# ---------------------------------------------------------------------------
# Calibration run over prepared golden items with an injected judge
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationItem:
    """One golden case: the labelled ``content`` plus the human ``verdict``."""

    case_id: str
    content: str
    human_verdict: str
    rubric_text: str = ""
    dimensions: Sequence[str] = ("overall",)

    def __post_init__(self) -> None:
        if self.human_verdict not in _VERDICTS:
            raise CalibrationError(
                f"golden verdict {self.human_verdict!r} not in {_VERDICTS}"
            )


@dataclass(frozen=True)
class CalibrationResult:
    """Judge-vs-human agreement over the golden set."""

    kappa: float
    n: int
    observed_agreement: float
    judge_verdicts: Sequence[str]
    human_verdicts: Sequence[str]
    injection_flags: int
    threshold: float
    passed: bool
    per_item: Sequence[Mapping[str, object]] = field(default_factory=tuple)

    def as_record(self, *, ts: str, judge_config: Mapping[str, object]) -> dict:
        return {
            "schema_version": "1.0.0",
            "ts": ts,
            "kappa": self.kappa,
            "n": self.n,
            "observed_agreement": self.observed_agreement,
            "threshold": self.threshold,
            "passed": self.passed,
            "injection_flags": self.injection_flags,
            "judge_config": dict(judge_config),
            "per_item": [dict(x) for x in self.per_item],
        }


def run_calibration(
    items: Sequence[CalibrationItem],
    call_model: Callable[[str, str], str],
    *,
    k: int = 5,
    pass_threshold: float = 0.7,
    golden_agreement_min: float = 0.7,
) -> CalibrationResult:
    """Score each golden item through the judge and report agreement.

    The judge scores every item's ``dimensions`` (default the single
    ``overall`` dimension) k times through the injection-hardened
    :func:`judge.run_judge`; the judge case verdict is ``pass`` iff the mean of
    the k-median dimension scores clears ``pass_threshold``. Cohen's kappa is
    then taken against the human verdicts. An item the judge flags as
    injection-suspected is recorded and scored ``fail`` (a poisoned golden case
    never silently counts as agreement).
    """
    if not items:
        raise CalibrationError("golden set is empty; nothing to calibrate")
    judge_verdicts: list[str] = []
    human_verdicts: list[str] = []
    per_item: list[dict] = []
    injection_flags = 0
    for item in items:
        rubric = item.rubric_text or _DEFAULT_RUBRIC
        result = run_judge(call_model, rubric, item.content, item.dimensions, k=k)
        composite = statistics.fmean(result.dimension_medians.values())
        if result.injection_suspected:
            injection_flags += 1
            jv = "fail"
        else:
            jv = "pass" if composite >= pass_threshold else "fail"
        judge_verdicts.append(jv)
        human_verdicts.append(item.human_verdict)
        per_item.append(
            {
                "case_id": item.case_id,
                "human_verdict": item.human_verdict,
                "judge_verdict": jv,
                "judge_score": composite,
                "injection_suspected": result.injection_suspected,
                "agree": jv == item.human_verdict,
            }
        )
    kappa = cohens_kappa(judge_verdicts, human_verdicts)
    observed = sum(1 for j, h in zip(judge_verdicts, human_verdicts) if j == h) / len(items)
    return CalibrationResult(
        kappa=kappa,
        n=len(items),
        observed_agreement=observed,
        judge_verdicts=tuple(judge_verdicts),
        human_verdicts=tuple(human_verdicts),
        injection_flags=injection_flags,
        threshold=golden_agreement_min,
        passed=kappa >= golden_agreement_min,
        per_item=tuple(per_item),
    )


# ---------------------------------------------------------------------------
# Golden-set / artefact loading (CLI side)
# ---------------------------------------------------------------------------
def load_golden_verdicts(golden_path: str | pathlib.Path) -> dict[str, str]:
    """Latest **case-level** human verdict per case (``assertion_id`` null).

    Rows are ordered by ``ts`` so a later relabel wins. Assertion-scoped rows
    are ignored here — calibration is a case-level agreement measure.
    """
    p = pathlib.Path(golden_path)
    if not p.is_file():
        raise CalibrationError(f"golden set not found: {p}")
    latest: dict[str, tuple[str, str]] = {}  # case_id -> (ts, verdict)
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CalibrationError(f"{p}:{lineno} is not valid JSON: {exc}") from exc
        if row.get("assertion_id") is not None:
            continue
        cid = str(row.get("case_id", ""))
        verdict = row.get("verdict")
        if not cid or verdict not in _VERDICTS:
            raise CalibrationError(f"{p}:{lineno} missing case_id/verdict")
        ts = str(row.get("ts", ""))
        if cid not in latest or ts >= latest[cid][0]:
            latest[cid] = (ts, verdict)
    return {cid: v for cid, (_, v) in latest.items()}


def _load_candidate_content(artefact_ref: str, roots: Sequence[pathlib.Path]) -> Optional[str]:
    """Read the judged candidate content for a labelled artefact, if resolvable.

    Tries each root as a base for ``artefact_ref`` and pulls the first of
    ``candidate_content`` / ``judge_content`` present on the case JSON.
    """
    for root in roots:
        path = (root / artefact_ref).resolve()
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for key in ("candidate_content", "judge_content"):
            if payload.get(key):
                return str(payload[key])
    return None


def _judge_config(config_path: Optional[str]) -> dict:
    """The ``[judge]`` table from a matrix config, or an empty dict."""
    if not config_path:
        return {}
    cfg = load_matrix_config(config_path)
    judge = cfg.raw.get("judge")
    return dict(judge) if isinstance(judge, Mapping) else {}


def _validate_judge_config(judge: Mapping[str, object]) -> list[str]:
    """Allow-list checks used by ``--dry-run`` (returns human-readable errors)."""
    errors: list[str] = []
    engine = judge.get("engine")
    if engine is not None and engine not in ci.ENGINE_IDS:
        errors.append(f"judge engine {engine!r} not in {ci.ENGINE_IDS}")
    samples = judge.get("samples", judge.get("k"))
    if samples is not None and (not isinstance(samples, int) or samples < 1 or samples % 2 == 0):
        errors.append(f"judge samples must be a positive odd int, got {samples!r}")
    return errors


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def add_arguments(parser) -> None:  # noqa: ANN001 - argparse subparser
    parser.add_argument("benchmark", help="benchmark directory (holds benchmark.json)")
    parser.add_argument("--config", default=None, help="matrix config for the [judge] table")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate config + golden set, print the sampling matrix, no model calls",
    )
    parser.add_argument("-k", "--samples", type=int, default=None, help="judge samples (odd)")
    parser.add_argument(
        "--runs-root", default=None,
        help="base dir for resolving golden artefact_refs (default: benchmark dir + cwd)",
    )
    parser.add_argument("-o", "--out", default=None, help="calibration record output path")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dry_run(benchmark, golden_path: pathlib.Path, judge: Mapping[str, object]) -> int:  # noqa: ANN001
    errors = _validate_judge_config(judge)
    try:
        verdicts = load_golden_verdicts(golden_path)
    except CalibrationError as exc:
        errors.append(str(exc))
        verdicts = {}
    samples = judge.get("samples", judge.get("k", 5))
    print("# judge calibrate --dry-run")
    print(f"- benchmark: {benchmark.id}")
    print(f"- golden set: {golden_path} ({len(verdicts)} case-level verdicts)")
    print(f"- judge engine/model: {judge.get('engine')}/{judge.get('model')}")
    print(f"- planned sampling: {len(verdicts)} cases x {samples} samples")
    print(f"- agreement threshold: {judge.get('golden_agreement_min', 0.7)}")
    if errors:
        print("\n## config/golden-set errors")
        for e in errors:
            print(f"- {e}")
        return 2
    print("\nOK: config + golden set valid; no model calls made")
    return 0


def run(args, call_model: Optional[Callable[[str, str], str]] = None) -> int:  # noqa: ANN001
    """Execute ``judge calibrate``. ``call_model`` may be injected (tests); when
    ``None`` a real backend is built from the judge config (non-dry-run only)."""
    benchmark = load_benchmark(args.benchmark)
    bench_dir = pathlib.Path(args.benchmark).resolve()
    if not benchmark.golden_set:
        raise CalibrationError(f"benchmark {benchmark.id!r} declares no golden_set")
    golden_path = bench_dir / benchmark.golden_set
    judge = _judge_config(args.config)
    threshold = float(judge.get("golden_agreement_min", 0.7))
    k = args.samples or int(judge.get("samples", judge.get("k", 5)))

    if args.dry_run:
        return _dry_run(benchmark, golden_path, judge)

    verdicts = load_golden_verdicts(golden_path)
    roots = [bench_dir, pathlib.Path.cwd()]
    if args.runs_root:
        roots.insert(0, pathlib.Path(args.runs_root).resolve())

    # Re-read rows to recover each case's artefact_ref for content resolution.
    ref_by_case: dict[str, str] = {}
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("assertion_id") is None and row.get("case_id") in verdicts:
            ref_by_case.setdefault(str(row["case_id"]), str(row.get("artefact_ref", "")))

    items: list[CalibrationItem] = []
    missing: list[str] = []
    for cid, human in verdicts.items():
        content = _load_candidate_content(ref_by_case.get(cid, ""), roots)
        if content is None:
            missing.append(cid)
            continue
        items.append(CalibrationItem(case_id=cid, content=content, human_verdict=human))
    if missing:
        raise CalibrationError(
            "could not resolve candidate content for golden cases: "
            + ", ".join(sorted(missing))
        )

    if call_model is None:
        call_model = _build_backend(judge)

    result = run_calibration(
        items, call_model, k=k,
        pass_threshold=float(benchmark.pass_threshold),
        golden_agreement_min=threshold,
    )

    ts = _now_iso()
    record = result.as_record(ts=ts, judge_config=judge)
    out = (
        pathlib.Path(args.out)
        if args.out
        else bench_dir / "labels" / "calibration" / f"{ts.replace(':', '')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"# judge calibration: {benchmark.id}")
    print(f"- kappa: {result.kappa:.4f} (threshold {threshold})")
    print(f"- observed agreement: {result.observed_agreement:.4f} over {result.n} cases")
    print(f"- injection flags: {result.injection_flags}")
    print(f"- verdict: {'PASS' if result.passed else 'FAIL'}")
    print(f"- record: {out}")
    return 0 if result.passed else 2


def _build_backend(judge: Mapping[str, object]) -> Callable[[str, str], str]:
    """Construct a real judge backend from config (HTTP if a key is present,
    else the claude CLI bridge). Kept lazy so dry-run/tests never build it."""
    import os

    from ..judge import cli_call_model, http_call_model

    model = str(judge.get("model", ""))
    if not model:
        raise CalibrationError("judge config has no model; cannot build backend")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    if api_key:
        return http_call_model(model, base_url=base_url, api_key=api_key)
    return cli_call_model(model)
