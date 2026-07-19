"""End-to-end eval-gate proof — the wave success metric in one test.

With **zero model spend**, drive the whole open producer→gate spine over the
committed ``examples/runs/dropdowns-synthetic`` A/B archive:

1. **compile** ``benchmarks/dropdowns-skill/cases.toml`` to canonical
   ``cases.jsonl`` (§11.1) and confirm the committed file is in sync;
2. **matrix** the synthetic ``v1`` and ``v2-regressed`` skill through the
   offline :class:`~wololo_evals.engines.ReplayAdapter` (re-emits archived raw
   logs, ``cost_usd == 0``, §14.4) + the real claude trace normaliser, scoring
   each trial from the archive's recorded ``grading.json`` signals under the
   ``skill_behaviour`` §16.1 family;
3. assert the full §15 artefact tree lands — ``matrix_summary.json`` +
   ``scorecard.json`` (2.1.0) whose ``signals[]`` carry the ``activation_correct``
   and ``trace_conformance`` slots — and that spend is exactly ``0``;
4. **gate**: ``v1``-vs-``v1`` passes (exit 0); ``v2``-vs-``v1`` fails (exit 1)
   as a *significant* regression whose ``regression`` bucket names the planted
   cases (``s-react``, ``s-portal``);
5. **replay** the ``v2`` tree twice and assert the scorecards are byte-stable
   (a scorecard that moves is a broken evaluator, not model noise).

Pure: file-scoped ``tmp_path``, module-level fakes, no Postgres, no engine
subprocess, no network. The only inputs are the committed fixtures under
``benchmarks/`` and ``examples/runs/``.

Two committed fixtures diverge from their normaliser's expected wire format —
the codex raw log uses the ``item.completed`` shape while ``CodexNormaliser``
expects ``{"msg":{...}}``, and the claude activation marker is a ``system`` line
``ClaudeNormaliser`` does not lift to a ``skill_activated`` lifecycle event.
Neither is this test's concern: the ``activation_correct``/``trace_conformance``
signals are supplied by the recorded ``grading.json``, so the scoring→gate→replay
spine is proven regardless. The claude stream is still normalised end-to-end to
exercise the trace pillar inside the live matrix runner.
"""
from __future__ import annotations

import json
import pathlib
import shutil

from wololo_evals.cli import cases_compile
from wololo_evals.cli import gate as gate_mod
from wololo_evals.cli import matrix as matrix_mod
from wololo_evals.cli import replay as replay_mod
from wololo_evals.cli.config import load_matrix_config
from wololo_evals.cli.main import main
from wololo_evals.core import interfaces as ci
from wololo_evals.engines import ReplayAdapter
from wololo_evals.trace import get_normaliser

# --------------------------------------------------------------------------
# committed fixtures (repo-relative, read-only)
# --------------------------------------------------------------------------
_REPO = pathlib.Path(__file__).resolve().parents[1]
_DOGFOOD_TOML = _REPO / "benchmarks" / "dropdowns-skill" / "cases.toml"
_SYNTH = _REPO / "examples" / "runs" / "dropdowns-synthetic"
_SYNTH_BENCH = _SYNTH / "benchmark"

#: version → committed matrix archive under the synthetic corpus.
_ARCHIVES = {"v1": _SYNTH / "baseline-v1", "v2": _SYNTH / "candidate-v2"}
#: the two recorded engines and their model ids (drives combo pairing in gate).
_MODELS = {"claude": "claude-sonnet-4-5", "codex": "gpt-5-codex"}
#: the planted regression: these two cases flip pass→fail from v1 to v2.
_PLANTED = {"s-react", "s-portal"}

_TS = "20260719T000000Z"


# --------------------------------------------------------------------------
# stage the committed archive into the ReplayAdapter's resolution layout and
# drive one zero-spend matrix run per skill version
# --------------------------------------------------------------------------
def _stage_replay_sources(version: str, dest: pathlib.Path) -> None:
    """Lay the archive's per-(case, seed) raw logs + ``grading.json`` out where
    ``ReplayAdapter`` resolves them: ``dest/<engine>/<case>/seed-<n>/``."""
    archive = _ARCHIVES[version]
    for engine, model in _MODELS.items():
        run_dir = (
            archive / "combos" / f"{engine}__{model}__default__{version}"
            / "runs" / f"combo-run-{engine}-{version}"
        )
        for seed_dir in sorted((run_dir / "seeds").iterdir()):
            for workspace in sorted((seed_dir / "workspace").iterdir()):
                adv = workspace / ci.ADVISORY_DIR
                out = dest / engine / workspace.name / f"seed-{seed_dir.name}"
                out.mkdir(parents=True, exist_ok=True)
                shutil.copy(adv / f"{engine}.raw.jsonl", out / f"{engine}.raw.jsonl")
                shutil.copy(adv / "grading.json", out / "grading.json")


def _grader(stage: pathlib.Path) -> matrix_mod.Grader:
    """Replay the archive's recorded signals: read the staged ``grading.json``
    for this (engine, case, seed) and return it as ``ScoredSignal[]``. This is
    the zero-spend re-scoring substrate — no model, no judge call."""

    def grade(spec, result, trace):  # noqa: ANN001 - matches Grader seam
        recorded = json.loads(
            (stage / spec.engine_id / spec.case.case_id / f"seed-{spec.seed}"
             / "grading.json").read_text(encoding="utf-8")
        )
        return [
            ci.ScoredSignal(
                evaluator_id=str(s["evaluator_id"]),
                kind=str(s["kind"]),
                score=float(s["score"]),
                weight=float(s["weight"]),
                verdict=str(s["verdict"]),
                slot=s.get("slot"),
                evidence=s.get("evidence"),
            )
            for s in recorded["signals"]
        ]

    return grade


def _normaliser_factory(engine_id: str):
    """Normalise the claude stream end-to-end (exercises the trace pillar in the
    live runner); skip codex whose committed fixture predates its normaliser's
    wire format (integration debt, tracked elsewhere)."""
    return get_normaliser("claude") if engine_id == "claude" else None


def _write_matrix_config(tmp: pathlib.Path, version: str) -> pathlib.Path:
    config = tmp / f"matrix-{version}.toml"
    config.write_text(
        f"""
schema_version = "2.0.0"
benchmark = "{_SYNTH_BENCH}"
output_dir = "out-{version}"
seeds = 2
[engines.claude]
version_range = "*"
[engines.codex]
version_range = "*"
[[models]]
id = "claude-sonnet-4-5"
engine = "claude"
model = "claude-sonnet-4-5"
[[models]]
id = "gpt-5-codex"
engine = "codex"
model = "gpt-5-codex"
[[prompts]]
id = "default"
""",
        encoding="utf-8",
    )
    return config


def _run_version(tmp: pathlib.Path, version: str) -> pathlib.Path:
    """Stage sources, build the config, and run the replay-engine matrix for one
    skill version; returns its ``matrix-<ts>`` directory."""
    stage = tmp / f"stage-{version}"
    _stage_replay_sources(version, stage)
    config = load_matrix_config(_write_matrix_config(tmp, version))

    def adapter_factory(engine_id: str) -> ci.EngineAdapter:
        return ReplayAdapter(runs_dir=str(stage / engine_id))

    return matrix_mod.run_matrix(
        config,
        adapter_factory,
        _grader(stage),
        timestamp=_TS,
        normaliser_factory=_normaliser_factory,
    )


def _cases_by_key(matrix_dir: pathlib.Path) -> tuple[dict, float]:
    """Read a matrix run into ``{(engine, model, prompt): {case_id: case_row}}``
    plus its pass threshold — the shape gate pairs on."""
    summary = json.loads((matrix_dir / "matrix_summary.json").read_text())
    out: dict[tuple[str, str, str], dict] = {}
    for combo in summary["combos"]:
        scorecard = json.loads((matrix_dir / combo["scorecard_path"]).read_text())
        key = (combo["engine_id"], combo["model_id"], combo["prompt_id"])
        out[key] = {c["case_id"]: c for c in scorecard["cases"]}
    return out, float(summary.get("pass_threshold", 0.7))


# --------------------------------------------------------------------------
# THE proof
# --------------------------------------------------------------------------
def test_e2e_eval_gate_proof(tmp_path, capsys):
    # --- (1) cases compile: canonical + committed file in sync (§11.1) -----
    compiled = cases_compile.compile(_DOGFOOD_TOML)
    assert compiled.splitlines()[0] == '{"schema_version":"2.0.0"}'  # header row
    assert compiled.endswith("\n")
    assert '", "' not in compiled.splitlines()[1]  # compact structural separators
    # the committed cases.jsonl round-trips with no drift, via the CLI surface
    assert main(["cases", "compile", str(_DOGFOOD_TOML), "--check"]) == 0

    # --- (2)+(3) matrix over v1 & v2 via the zero-spend replay engine ------
    v1_dir = _run_version(tmp_path, "v1")
    v2_dir = _run_version(tmp_path, "v2")

    for matrix_dir in (v1_dir, v2_dir):
        summary = json.loads((matrix_dir / "matrix_summary.json").read_text())
        assert summary["schema_version"] == "2.0.0"
        # both recorded engines produced a combo (needed for a paired A/B)
        engines = {c["engine_id"] for c in summary["combos"]}
        assert engines == {"claude", "codex"}
        # zero model spend — the whole run is replayed archive bytes (§14.4)
        assert summary["cumulative_spend_usd"] == 0.0

        # §15.3 scorecard with signals[] carrying the trace + activation slots
        claude_combo = next(c for c in summary["combos"] if c["engine_id"] == "claude")
        scorecard = json.loads((matrix_dir / claude_combo["scorecard_path"]).read_text())
        assert scorecard["schema_version"] == "2.1.0"
        react = next(c for c in scorecard["cases"] if c["case_id"] == "s-react")
        assert react["signals"], "signals[] must be populated"
        assert {"evaluator_id", "kind", "score", "weight", "verdict", "slot"} <= (
            react["signals"][0].keys()
        )
        slots = {s.get("slot") for s in react["signals"]}
        assert {"activation_correct", "trace_conformance", "task_outcome"} <= slots

    # trace pillar ran live in the matrix: the claude stream normalised to a
    # non-empty trace.jsonl of tool events (activation comes from the signal).
    claude_traces = [
        p for p in v2_dir.rglob("trace.jsonl")
        if "claude-sonnet" in str(p) and "s-react" in str(p) and p.read_text().strip()
    ]
    assert claude_traces, "claude trace.jsonl should be normalised and non-empty"
    trace_rows = [json.loads(line) for line in claude_traces[0].read_text().splitlines()]
    assert any(r["kind"] == "tool_call" for r in trace_rows)

    # --- (4) gate: v1-vs-v1 passes; v2-vs-v1 fails on the planted regression -
    same = gate_mod.evaluate(v1_dir, v1_dir, golden_agreement=1.0)
    assert same.verdict == "pass" and same.exit_code == 0
    assert same.ab_verdict.buckets == {"working": 8, "fixed": 0, "regression": 0, "still_gap": 0}

    # drive the real CLI so the *process exit code* is what CI would read
    verdict_path = tmp_path / "verdict.json"
    exit_code = main([
        "gate", "--run", str(v2_dir), "--baseline", str(v1_dir),
        "--golden-agreement", "1.0", "-o", str(verdict_path),
    ])
    assert exit_code == 1  # non-zero: a regression
    payload = json.loads(verdict_path.read_text())
    assert payload["verdict"] == "regression"
    # significant against the tolerance-shifted null, not just a mean shift
    assert payload["stats"]["significant_regression"] is True
    assert payload["stats"]["ci_high"] < 0
    # 2 planted cases x 2 engines land in the regression bucket
    assert payload["stats"]["buckets"]["regression"] == 4
    assert "Gate verdict: REGRESSION" in capsys.readouterr().out

    # the regression bucket *names* the planted cases and nothing else
    v2_cases, threshold = _cases_by_key(v2_dir)
    v1_cases, _ = _cases_by_key(v1_dir)
    regressed = {
        case_id
        for key, cases in v2_cases.items()
        for case_id, row in cases.items()
        if float(v1_cases[key][case_id]["score"]) >= threshold
        and float(row["score"]) < threshold
    }
    assert regressed == _PLANTED  # exactly s-react + s-portal, on both engines

    # --- (5) replay is byte-stable across invocations (§14.4) --------------
    scorecards = sorted(v2_dir.rglob("scorecard.json"))
    assert scorecards, "matrix must have written scorecards"
    before = {p: p.read_bytes() for p in scorecards}
    replay_mod.replay(v2_dir)
    after_first = {p: p.read_bytes() for p in scorecards}
    replay_mod.replay(v2_dir)
    after_second = {p: p.read_bytes() for p in scorecards}
    assert before == after_first == after_second
