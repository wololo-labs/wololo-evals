"""The file-first CLI is the open producer surface: authoring (`cases compile`),
planning (`matrix --dry-run`), injected-adapter execution, and the zero-spend
`replay`. These tests pin the load-bearing invariants:

* **§11.1 canonical compilation** — byte-stable `cases.jsonl` (header row,
  §11.2 field order, path-set union, assertion attachment, distractor rule),
  and `--check` that fails loudly on drift.
* **§13 expansion** — `matrix --dry-run` yields the exact engine x model x
  prompt/baseline x asset x case x seed count and writes nothing live.
* **§16.1 score composition** — slot-weighted family scoring, judge-disabled
  denominator exclusion, hard-gate zeroing.
* **§15 artefact tree** — `matrix_summary.json` (provenance block),
  `scorecard.json` v2.1.0 (`signals[]`), `events.jsonl` (header), driven
  through an injected `EngineAdapter` + grader (no model spend, no DB).
* **§14.4 replay** — re-scoring an archived run is byte-stable.

Pure tests: no Postgres, file-scoped `tmp_path` fixtures, module-level fakes.
"""
from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stdout

import pytest

from wololo_evals.cli import bench as bench_mod
from wololo_evals.cli import cases_compile
from wololo_evals.cli import matrix as matrix_mod
from wololo_evals.cli import replay as replay_mod
from wololo_evals.cli.artefact_writer import compose_case, pass_at_k, pass_hat_k
from wololo_evals.cli.config import load_matrix_config
from wololo_evals.cli.main import main
from wololo_evals.core import interfaces as ci


# --------------------------------------------------------------------------
# module-level fakes (reusable across tests, per test-architecture §5)
# --------------------------------------------------------------------------
class FakeAdapter:
    """Deterministic engine stand-in: fixed cost/tokens, no subprocess."""

    def __init__(self, engine_id: str = "claude", cost: float = 0.01) -> None:
        self.engine_id = engine_id
        self._cost = cost

    def cli_version(self) -> str:
        return "2.2.0"

    def build_command(self, spec):  # noqa: ANN001
        return ["true"]

    def raw_event_path(self, spec) -> pathlib.Path:  # noqa: ANN001
        return pathlib.Path(spec.cwd) / f"{self.engine_id}.raw.jsonl"

    def run(self, spec):  # noqa: ANN001
        return ci.RunResult(
            run_id=spec.run_id, status="ok", artefact_dir=spec.cwd,
            runtime_seconds=1.0, cost_usd=self._cost, tokens_in=10, tokens_out=20,
        )


class RecordingAdapter(FakeAdapter):
    """Records where a trial ran and what it could see from there."""

    seen: list = []

    def run(self, spec):  # noqa: ANN001
        cwd = pathlib.Path(spec.cwd)
        RecordingAdapter.seen.append(
            {
                "cwd": cwd,
                "git": (cwd / ".git").is_dir(),
                "had_fixture": (cwd / "pkg" / "mod.py").is_file(),
            }
        )
        (cwd / "made_by_trial.txt").write_text("x", encoding="utf-8")
        return super().run(spec)


def adapter_factory(cost: float = 0.01):
    return lambda engine_id: FakeAdapter(engine_id, cost)


def make_grader(c1_score: float = 1.0):
    """Grader whose task-outcome signal makes `c1` pass and the distractor fail
    unless told otherwise — deterministic so replay is byte-stable."""

    def grader(spec, result, trace):  # noqa: ANN001
        outcome = c1_score if spec.case.case_id == "c1" else 0.0
        return [
            ci.ScoredSignal("act", "gate", 1.0, 1.0, "pass", slot="activation_correct"),
            ci.ScoredSignal(
                "explains", "assertion", outcome, 1.0,
                "pass" if outcome >= 0.5 else "fail", slot="task_outcome",
            ),
            ci.ScoredSignal("reads", "assertion", 1.0, 1.0, "pass", slot="trace_conformance"),
            ci.ScoredSignal("judge", "judge", 0.8, 1.0, "pass", slot="llm_judge_overall"),
        ]

    return grader


_CASES_TOML = """
version = 2
[defaults]
assertions = ["explains"]
[assertions]
explains = "the answer explains the risk"
reads = "trace:require_order(read:a, edit:a)"
[path_sets]
manifest = ["package.json", "package-lock.json"]
[defaults.assertion_weights]
explains = 1.0
[[cases]]
case_id = "c1"
title = "one"
expected_paths = ["SECURITY.md"]
expected_path_refs = ["manifest"]
assertions = ["explains", "reads"]
[[cases]]
case_id = "c2-distractor"
role = "distractor"
input_text = "unrelated"
"""

_MATRIX_TOML = """
schema_version = "2.0.0"
benchmark = "benchmarks/demo"
output_dir = "out/demo"
seeds = 2
max_cost_usd = {max_cost}
[engines.claude]
version_range = ">=2.1,<3"
[[models]]
id = "claude-sonnet"
engine = "claude"
model = "claude-sonnet-4-6"
[[prompts]]
id = "stock"
[[baselines]]
id = "nofix"
inline = "do nothing"
"""


def _make_project(tmp_path: pathlib.Path, *, evaluation_type: str = "skill_behaviour",
                  max_cost: str = "25.0") -> pathlib.Path:
    """Scaffold a compiled benchmark + matrix config under tmp_path; return the
    matrix config path."""
    bench_dir = tmp_path / "benchmarks" / "demo"
    bench_dir.mkdir(parents=True)
    (bench_dir / "benchmark.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "demo",
        "evaluation_type": evaluation_type, "pass_threshold": 0.7,
        "engines": ["claude", "codex"],
    }))
    (bench_dir / "cases.toml").write_text(_CASES_TOML)
    (bench_dir / "cases.jsonl").write_text(cases_compile.compile(bench_dir / "cases.toml"))
    mc = tmp_path / "matrix.toml"
    mc.write_text(_MATRIX_TOML.format(max_cost=max_cost))
    return mc


# --------------------------------------------------------------------------
# §11.1 cases compile
# --------------------------------------------------------------------------
def test_compile_is_canonical_with_path_union_and_attachment(tmp_path):
    (tmp_path / "cases.toml").write_text(_CASES_TOML)
    text = cases_compile.compile(tmp_path / "cases.toml")
    lines = text.splitlines()
    assert lines[0] == '{"schema_version":"2.0.0"}'  # header first
    assert text.endswith("\n")  # trailing newline
    c1 = json.loads(lines[1])
    # path-set union: manifest expansion then case-level, deduped in order
    assert c1["expected_paths"] == ["package.json", "package-lock.json", "SECURITY.md"]
    # assertion attachment: both attached, weight from defaults, source tagged
    assert [a["id"] for a in c1["assertions"]] == ["explains", "reads"]
    assert c1["assertions"][0] == {
        "id": "explains", "text": "the answer explains the risk",
        "weight": 1.0, "source": "assertions",
    }
    # compact structural separators (§11.1): no space after field-delimiting comma
    assert '","' in lines[1] and '", "' not in lines[1]


def test_compile_distractor_rules(tmp_path):
    # distractor auto-resolves to no_activation
    (tmp_path / "cases.toml").write_text(_CASES_TOML)
    rows = [json.loads(x) for x in cases_compile.compile(tmp_path / "cases.toml").splitlines()[1:]]
    distractor = next(r for r in rows if r["role"] == "distractor")
    assert distractor["expected_outcome"] == "no_activation"

    # an explicit conflicting outcome is rejected
    bad = '[[cases]]\ncase_id="d"\nrole="distractor"\nexpected_outcome="pass"\n'
    (tmp_path / "bad.toml").write_text(bad)
    with pytest.raises(cases_compile.CasesCompileError):
        cases_compile.compile(tmp_path / "bad.toml")


def test_compile_check_detects_drift(tmp_path):
    toml = tmp_path / "cases.toml"
    toml.write_text(_CASES_TOML)
    jsonl = tmp_path / "cases.jsonl"
    jsonl.write_text(cases_compile.compile(toml))
    ok, _ = cases_compile.check(toml)
    assert ok is True
    # mutate on-disk -> drift
    jsonl.write_text(jsonl.read_text() + '{"case_id":"stray"}\n')
    ok, msg = cases_compile.check(toml)
    assert ok is False and "stale" in msg


def test_compile_check_via_main_exit_code(tmp_path):
    toml = tmp_path / "cases.toml"
    toml.write_text(_CASES_TOML)
    (tmp_path / "cases.jsonl").write_text(cases_compile.compile(toml))
    assert main(["cases", "compile", str(toml), "--check"]) == 0
    (tmp_path / "cases.jsonl").write_text("tampered\n")
    assert main(["cases", "compile", str(toml), "--check"]) == 1


def test_compile_writes_jsonl_via_main(tmp_path):
    toml = tmp_path / "cases.toml"
    toml.write_text(_CASES_TOML)
    out = tmp_path / "out.jsonl"
    assert main(["cases", "compile", str(toml), "-o", str(out)]) == 0
    assert out.read_text() == cases_compile.compile(toml)


# --------------------------------------------------------------------------
# §13 matrix expansion / dry-run
# --------------------------------------------------------------------------
def test_dry_run_expansion_count_and_no_writes(tmp_path):
    mc = _make_project(tmp_path)
    cfg = load_matrix_config(mc)
    plan = matrix_mod.plan(cfg)
    # 2 combos (treatment + baseline) x 2 cases x 2 seeds
    assert len(plan.combos) == 2
    assert plan.total_runs == 2 * 2 * 2
    assert len(matrix_mod.expand(cfg)) == plan.total_runs

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["matrix", "--config", str(mc), "--dry-run"])
    assert rc == 0
    assert "total runs: 8" in buf.getvalue()
    # dry-run writes nothing
    assert not (tmp_path / "out").exists()


def test_expand_rejects_engine_not_in_benchmark(tmp_path):
    mc = _make_project(tmp_path)
    bj = tmp_path / "benchmarks" / "demo" / "benchmark.json"
    data = json.loads(bj.read_text())
    data["engines"] = ["codex"]  # claude no longer allowed
    bj.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not in benchmark"):
        matrix_mod.expand(load_matrix_config(mc))


# --------------------------------------------------------------------------
# §16.1 score composition
# --------------------------------------------------------------------------
def test_compose_skill_behaviour_weights_and_judge_exclusion():
    sigs = [
        ci.ScoredSignal("a", "gate", 1.0, 1.0, "pass", slot="activation_correct"),
        ci.ScoredSignal("t", "assertion", 0.5, 1.0, "pass", slot="task_outcome"),
        ci.ScoredSignal("c", "assertion", 1.0, 1.0, "pass", slot="trace_conformance"),
        ci.ScoredSignal("j", "judge", 0.0, 1.0, "skipped", slot="llm_judge_overall"),
    ]
    c = compose_case(sigs, "skill_behaviour", pass_threshold=0.7)
    # judge weight (15) excluded: (25*1 + 40*0.5 + 20*1) / (25+40+20) = 65/85
    assert c.score == pytest.approx(65 / 85)
    assert c.judge_disabled is True
    assert c.passed is True


def test_compose_hard_gate_zeroes_score():
    sigs = [
        ci.ScoredSignal("t", "assertion", 1.0, 1.0, "pass", slot="task_outcome"),
        ci.ScoredSignal("forbidden_paths", "safety", 0.0, 1.0, "fail", slot=None),
    ]
    c = compose_case(sigs, "skill_behaviour", pass_threshold=0.7)
    assert c.hard_gate_failed is True
    assert c.score == 0.0 and c.passed is False


def test_compose_unscored_run_scores_zero():
    sigs = [ci.ScoredSignal("t", "assertion", 1.0, 1.0, "pass", slot="task_outcome")]
    c = compose_case(sigs, "skill_behaviour", pass_threshold=0.7, scored=False)
    assert c.score == 0.0 and c.passed is False


def test_pass_at_k_identities():
    # k=1: pass@1 == pass^1 == observed pass rate; all-pass -> 1 for every k
    assert pass_at_k(2, 3, 1) == pytest.approx(2 / 3)
    assert pass_hat_k(2, 3, 1) == pytest.approx(2 / 3)
    assert pass_at_k(3, 3, 3) == 1.0
    assert pass_hat_k(3, 3, 3) == 1.0
    assert pass_hat_k(2, 3, 3) == 0.0


# --------------------------------------------------------------------------
# §15 artefact tree (injected adapter + grader)
# --------------------------------------------------------------------------
def test_fixtures_dir_lands_one_copy_per_case_workspace(tmp_path):
    # A live run can only write tests for code it can read, so the fixture tree
    # is materialised into every case workspace, one pristine copy each.
    fixtures = tmp_path / "fixtures"
    (fixtures / "quoting").mkdir(parents=True)
    (fixtures / "quoting" / "pricing.py").write_text("RATE = 1\n")
    mc = _make_project(tmp_path)
    # top-level key, so it has to go in before the first table
    mc.write_text('fixtures_dir = "fixtures"\n' + mc.read_text())

    md = matrix_mod.run_matrix(
        load_matrix_config(mc), adapter_factory(), make_grader(),
        timestamp="20260719T000000Z",
    )
    copies = sorted(md.rglob("workspace/*/quoting/pricing.py"))
    assert copies, "fixture never reached a case workspace"
    # one per case x seed x combo, and each one is its own copy
    assert len({p.parent.parent for p in copies}) == len(copies)
    assert all(p.read_text() == "RATE = 1\n" for p in copies)


def test_the_answer_key_never_reaches_a_case_workspace(tmp_path):
    # About half of the first live runs ran `cat seeded_defects.json`: the
    # manifest is copied beside the fixture, and its rationales name the very
    # rules a good suite is supposed to find. Withheld from the workspace, the
    # scorers still read the committed copy benchmark-relative.
    fixtures = tmp_path / "fixtures"
    (fixtures / "pkg").mkdir(parents=True)
    (fixtures / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (fixtures / "seeded_defects.json").write_text("{}")
    (fixtures / "changed_lines.json").write_text("{}")
    mc = _make_project(tmp_path)
    mc.write_text('fixtures_dir = "fixtures"\n' + mc.read_text())

    md = matrix_mod.run_matrix(
        load_matrix_config(mc), adapter_factory(), make_grader(),
        timestamp="20260719T000000Z",
    )
    assert list(md.rglob("workspace/*/pkg/mod.py")), "fixture never reached a workspace"
    assert not list(md.rglob("workspace/*/seeded_defects.json"))
    assert not list(md.rglob("workspace/*/changed_lines.json"))


class FlakyAdapter(FakeAdapter):
    """Fails its first attempt at each case after leaving a mess behind."""

    log: list = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._attempt: dict = {}

    def run(self, spec):  # noqa: ANN001
        cwd = pathlib.Path(spec.cwd)
        n = self._attempt[spec.case.case_id] = self._attempt.get(spec.case.case_id, 0) + 1
        FlakyAdapter.log.append(
            {"attempt": n, "stray": (cwd / "stray.txt").exists(), "fixture": (cwd / "pkg" / "mod.py").is_file()}
        )
        if n == 1:
            (cwd / "stray.txt").write_text("left behind by a failed attempt")
            (cwd / "pkg" / "mod.py").write_text("VALUE = 'corrupted'\n")
            return ci.RunResult(run_id=spec.run_id, status="infra_fail", artefact_dir=spec.cwd, runtime_seconds=0.1)
        return super().run(spec)


def test_a_matrix_retry_starts_from_a_pristine_workspace(tmp_path):
    # An infra_fail retry used to run in the workspace the failed attempt had
    # already written to, so the second attempt could start from edited code
    # or a half-written suite and be scored as if it had started clean.
    fixtures = tmp_path / "fixtures"
    (fixtures / "pkg").mkdir(parents=True)
    (fixtures / "pkg" / "mod.py").write_text("VALUE = 1\n")
    mc = _make_project(tmp_path)
    mc.write_text('fixtures_dir = "fixtures"\n' + mc.read_text())

    FlakyAdapter.log = []
    matrix_mod.run_matrix(
        load_matrix_config(mc), lambda e: FlakyAdapter(e), make_grader(),
        timestamp="20260719T000000Z",
    )
    retries = [r for r in FlakyAdapter.log if r["attempt"] == 2]
    assert retries, "no retry happened"
    for r in retries:
        assert not r["stray"], "the retry saw the failed attempt's leftovers"
        assert r["fixture"], "the retry did not get the fixture back"


def test_trials_run_outside_the_repository_and_leave_a_retained_copy(tmp_path):
    # A trial runs with --dangerously-skip-permissions, and its cwd used to sit
    # inside this repository, 16 directories below the root, so `../` reached
    # the answer key and the skill file. The trial now runs in a throwaway
    # directory elsewhere; the artefact tree still gets a copy to inspect.
    fixtures = tmp_path / "fixtures"
    (fixtures / "pkg").mkdir(parents=True)
    (fixtures / "pkg" / "mod.py").write_text("VALUE = 1\n")
    mc = _make_project(tmp_path)
    mc.write_text('fixtures_dir = "fixtures"\n' + mc.read_text())

    RecordingAdapter.seen = []
    md = matrix_mod.run_matrix(
        load_matrix_config(mc), lambda e: RecordingAdapter(e), make_grader(),
        timestamp="20260719T000000Z",
    )
    assert RecordingAdapter.seen, "the adapter never ran"
    for run in RecordingAdapter.seen:
        cwd = run["cwd"].resolve()
        assert tmp_path.resolve() not in cwd.parents, f"{cwd} is inside the project tree"
        assert md.resolve() not in cwd.parents, f"{cwd} is inside the artefact tree"
        assert run["had_fixture"], "the fixture was not materialised before the run"
        assert run["git"], "the trial had no repository of its own"
        assert not cwd.exists(), f"{cwd} was left behind"

    kept = list(md.rglob("workspace/*/made_by_trial.txt"))
    assert len(kept) == len(RecordingAdapter.seen), "the artefact tree lost a workspace"
    assert not list(md.rglob("workspace/*/.git")), "a nested repository was retained"


def test_only_cases_restricts_the_run_and_refuses_unknown_ids(tmp_path):
    # Calibrating a few cases of a casebook on their own means running them
    # without the rest, and a typo must fail loudly rather than run nothing.
    mc = _make_project(tmp_path)
    mc.write_text('only_cases = ["c1"]\n' + mc.read_text())
    md = matrix_mod.run_matrix(
        load_matrix_config(mc), adapter_factory(), make_grader(),
        timestamp="20260719T000000Z",
    )
    ran = {p.stem for p in md.rglob("seeds/*/cases/*.json")}
    assert ran == {"c1"}
    # the dry run must agree with the run, or it would promise a different spend
    planned = matrix_mod.plan(load_matrix_config(mc))
    assert planned.n_cases == 1
    assert {spec.case.case_id for spec in planned.specs} == {"c1"}

    bad = _make_project(tmp_path / "bad")
    bad.write_text('only_cases = ["no-such-case"]\n' + bad.read_text())
    with pytest.raises(ValueError, match="unknown case ids"):
        matrix_mod.run_matrix(
            load_matrix_config(bad), adapter_factory(), make_grader(),
            timestamp="20260719T000000Z",
        )


def test_the_preamble_is_not_applied_to_a_distractor_case(tmp_path):
    # A distractor case exists to check the skill stays silent. Telling the
    # model to use the skill on it makes the forced arm fail activation there
    # by construction, which measures the preamble and not the skill.
    mc = _make_project(tmp_path)
    mc.write_text('prompt_preamble = "Use the demo skill."\n' + mc.read_text())

    seen = {}

    class Spy(FakeAdapter):
        def run(self, spec):  # noqa: ANN001
            seen[spec.case.case_id] = dict(spec.extra)
            return super().run(spec)

    matrix_mod.run_matrix(
        load_matrix_config(mc), lambda e: Spy(e), make_grader(),
        timestamp="20260719T000000Z",
    )
    roles = {}
    for line in (tmp_path / "benchmarks" / "demo" / "cases.jsonl").read_text().splitlines()[1:]:
        row = json.loads(line); roles[row["case_id"]] = row["expected_outcome"]
    distractors = [cid for cid, out in roles.items() if out == "no_activation"]
    assert distractors, "the fixture casebook has no distractor to check"
    for cid, extra in seen.items():
        if cid in distractors:
            assert "prompt_preamble" not in extra, f"{cid} got the preamble"
        else:
            assert extra.get("prompt_preamble") == "Use the demo skill.", f"{cid} lost it"


def test_expected_skill_override_reaches_every_case(tmp_path):
    # One benchmark, two assets doing the same job under different slugs: the
    # matrix says which slug this arm ships, so activation is scored against
    # the asset under evaluation rather than the benchmark's first author.
    mc = _make_project(tmp_path)
    mc.write_text('expected_skill = "test-writer-fixer"\n' + mc.read_text())
    cfg = load_matrix_config(mc)
    assert cfg.expected_skill == "test-writer-fixer"

    seen: list[object] = []
    factory = adapter_factory()

    def recording_factory(engine_id):
        adapter = factory(engine_id)
        run = adapter.run

        def spy(spec):
            seen.append(spec.case.expected_skill)
            return run(spec)

        adapter.run = spy
        return adapter

    matrix_mod.run_matrix(
        cfg, recording_factory, make_grader(), timestamp="20260719T000000Z"
    )
    assert seen and set(seen) == {"test-writer-fixer"}


def test_run_matrix_writes_artefact_tree(tmp_path):
    cfg = load_matrix_config(_make_project(tmp_path))
    md = matrix_mod.run_matrix(
        cfg, adapter_factory(), make_grader(), timestamp="20260719T000000Z"
    )
    summary = json.loads((md / "matrix_summary.json").read_text())
    assert summary["schema_version"] == "2.0.0"
    # provenance block (§15.1)
    prov = summary["provenance"]
    assert {"harness_version", "engine_cli_versions", "judge", "repro_command"} <= prov.keys()
    assert summary["engines"]["claude"] == "2.2.0"
    assert summary["cumulative_spend_usd"] == pytest.approx(0.08)  # 2 combos*2 cases*2 seeds*0.01

    # events.jsonl header (§15.2)
    events = (md / "events.jsonl").read_text().splitlines()
    assert json.loads(events[0]) == {"schema_version": "1.0.0"}
    assert any(json.loads(e).get("event") == "run_finished" for e in events[1:])

    # scorecard v2.1.0 with signals[] (§15.3)
    sc = json.loads((md / summary["combos"][0]["scorecard_path"]).read_text())
    assert sc["schema_version"] == "2.1.0"
    case = next(c for c in sc["cases"] if c["case_id"] == "c1")
    assert case["signals"], "signals[] must be populated"
    assert {"evaluator_id", "kind", "score", "weight", "verdict", "slot"} <= case["signals"][0].keys()
    assert set(sc["summary"]["pass_at_k"].keys()) == {"1", "2"}


def test_bench_writes_single_scorecard(tmp_path):
    cfg = load_matrix_config(_make_project(tmp_path))
    out = tmp_path / "bench-out"
    sc_path = bench_mod.run_bench(
        cfg, adapter_factory(), make_grader(), timestamp="20260719T000000Z", out=out
    )
    assert sc_path == out / "scorecard.json"
    sc = json.loads(sc_path.read_text())
    assert sc["schema_version"] == "2.1.0"
    assert sc["combo"]["role"] == "treatment"


def test_cost_cap_marks_remaining_runs(tmp_path):
    # cap below one combo's spend -> first run ok, rest cost_cap_hit, partial written
    cfg = load_matrix_config(_make_project(tmp_path, max_cost="0.005"))
    md = matrix_mod.run_matrix(
        cfg, adapter_factory(0.01), make_grader(), timestamp="20260719T000000Z"
    )
    seed_files = list(md.rglob("seeds/*/cases/*.json"))
    statuses = {json.loads(f.read_text())["status"] for f in seed_files}
    assert "cost_cap_hit" in statuses
    # partial artefacts (scorecards) still written
    assert list(md.rglob("scorecard.json"))


# --------------------------------------------------------------------------
# §14.4 replay byte-stability
# --------------------------------------------------------------------------
def test_replay_is_byte_stable(tmp_path):
    cfg = load_matrix_config(_make_project(tmp_path))
    md = matrix_mod.run_matrix(
        cfg, adapter_factory(), make_grader(), timestamp="20260719T000000Z"
    )
    scorecards = sorted(md.rglob("scorecard.json"))
    before = {p: p.read_bytes() for p in scorecards}
    replay_mod.replay(md)
    after = {p: p.read_bytes() for p in scorecards}
    assert before == after
    # idempotent across a second replay
    replay_mod.replay(md)
    assert {p: p.read_bytes() for p in scorecards} == after


def test_replay_missing_summary_fails_loud(tmp_path):
    with pytest.raises(replay_mod.ReplayError):
        replay_mod.replay(tmp_path)
