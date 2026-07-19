"""``judge calibrate`` golden-set agreement + ``label`` ledger (README §17).

Proves: Cohen's kappa math; calibration over an injected deterministic judge
(perfect agreement passes, disagreement fails, an injection-flagged golden case
never counts as agreement); golden-set parsing (case-level only, latest-ts
wins); ``--dry-run`` validates config + golden set with **zero** model calls;
a full run writes a drift-tracked calibration record ``gate`` can then read; and
``label`` appends canonical golden rows.

Pure: a synthetic benchmark on disk + a canned judge backend; no Postgres,
no network, no model spend.
"""
import json
import pathlib
import types

import pytest

from wololo_evals.cli import calibrate as cal
from wololo_evals.cli import gate as gate_mod
from wololo_evals.cli import label as label_mod
from wololo_evals.cli.main import main


def _judge(system, user):
    """Deterministic judge: 'overall'=1.0 when the fenced content says GOOD."""
    score = 1.0 if "GOOD" in user else 0.0
    return json.dumps(
        {"dimension_scores": {"overall": score}, "rationale": "x", "injection_suspected": False}
    )


def _injection_judge(system, user):
    return json.dumps(
        {"dimension_scores": {"overall": 1.0}, "rationale": "x", "injection_suspected": True}
    )


# --------------------------------------------------------------------------
# Cohen's kappa
# --------------------------------------------------------------------------
def test_kappa_perfect_and_chance():
    assert cal.cohens_kappa(["pass", "fail", "pass"], ["pass", "fail", "pass"]) == 1.0
    # symmetric disagreement about a balanced marginal -> kappa 0
    assert cal.cohens_kappa(
        ["pass", "pass", "fail", "fail"], ["pass", "fail", "pass", "fail"]
    ) == pytest.approx(0.0)


def test_kappa_known_value():
    # 8 agree (4 pass/4 fail), 2 disagree -> po=0.8; balanced marginals -> pe=0.5
    judge = ["pass"] * 5 + ["fail"] * 5
    human = ["pass"] * 4 + ["fail"] + ["pass"] + ["fail"] * 4
    assert cal.cohens_kappa(judge, human) == pytest.approx(0.6, abs=1e-9)


def test_kappa_all_same_category_is_perfect_when_observed():
    assert cal.cohens_kappa(["pass", "pass"], ["pass", "pass"]) == 1.0


# --------------------------------------------------------------------------
# run_calibration over an injected judge
# --------------------------------------------------------------------------
def test_calibration_perfect_agreement_passes():
    items = [
        cal.CalibrationItem("a", "GOOD one", "pass"),
        cal.CalibrationItem("b", "BAD one", "fail"),
        cal.CalibrationItem("c", "GOOD two", "pass"),
    ]
    r = cal.run_calibration(items, _judge, k=1, pass_threshold=0.7, golden_agreement_min=0.7)
    assert r.kappa == 1.0 and r.passed and r.observed_agreement == 1.0


def test_calibration_disagreement_fails_threshold():
    # judge says pass on both, humans split -> low agreement
    items = [
        cal.CalibrationItem("a", "GOOD", "pass"),
        cal.CalibrationItem("b", "GOOD", "fail"),
    ]
    r = cal.run_calibration(items, _judge, k=1, golden_agreement_min=0.7)
    assert not r.passed


def test_injection_flag_forces_fail_verdict():
    items = [cal.CalibrationItem("a", "GOOD", "pass")]
    r = cal.run_calibration(items, _injection_judge, k=1)
    assert r.injection_flags == 1
    assert r.judge_verdicts[0] == "fail"  # never silently agrees


def test_calibration_empty_raises():
    with pytest.raises(cal.CalibrationError):
        cal.run_calibration([], _judge)


# --------------------------------------------------------------------------
# golden-set parsing
# --------------------------------------------------------------------------
def test_load_golden_case_level_only_latest_wins(tmp_path):
    p = tmp_path / "golden.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"case_id": "a", "verdict": "fail", "assertion_id": None, "ts": "2026-01-01"},
                {"case_id": "a", "verdict": "pass", "assertion_id": None, "ts": "2026-02-01"},
                {"case_id": "a", "verdict": "fail", "assertion_id": "x", "ts": "2026-03-01"},
                {"case_id": "b", "verdict": "pass", "assertion_id": None, "ts": "2026-01-01"},
            ]
        )
    )
    assert cal.load_golden_verdicts(p) == {"a": "pass", "b": "pass"}


def test_load_golden_rejects_malformed(tmp_path):
    p = tmp_path / "golden.jsonl"
    p.write_text('{"case_id":"a","assertion_id":null}\n')  # no verdict
    with pytest.raises(cal.CalibrationError):
        cal.load_golden_verdicts(p)


# --------------------------------------------------------------------------
# CLI: --dry-run (no model calls) and full run (record written)
# --------------------------------------------------------------------------
def _bench(tmp_path, verdicts, *, contents=None, judge_engine="claude"):
    """Scaffold a benchmark dir + matrix config; return (bench_dir, config_path)."""
    contents = contents or {}
    bench = tmp_path / "benchmarks" / "demo"
    (bench / "labels").mkdir(parents=True)
    (bench / "artefacts").mkdir()
    bench.joinpath("benchmark.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "demo", "evaluation_type": "skill_behaviour",
        "pass_threshold": 0.7, "golden_set": "labels/golden.jsonl",
    }))
    rows = []
    for cid, v in verdicts.items():
        ref = f"artefacts/{cid}.json"
        rows.append({"case_id": cid, "artefact_ref": ref, "verdict": v,
                     "assertion_id": None, "labeller": "human", "ts": "2026-01-01T00:00:00Z"})
        if cid in contents:
            (bench / ref).write_text(json.dumps({"case_id": cid, "candidate_content": contents[cid]}))
    (bench / "labels" / "golden.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

    cfg = tmp_path / "matrix.toml"
    cfg.write_text(
        'schema_version="2.0.0"\nbenchmark="benchmarks/demo"\noutput_dir="out"\n'
        f'[judge]\nengine="{judge_engine}"\nmodel="claude-sonnet-4-5"\n'
        'samples=3\ngolden_agreement_min=0.7\n'
        '[[models]]\nid="m"\nengine="claude"\nmodel="claude-sonnet-4-5"\n'
    )
    return bench, cfg


def _ns(bench, cfg, **over):
    base = {"benchmark": str(bench), "config": str(cfg), "dry_run": False,
            "samples": None, "runs_root": None, "out": None}
    base.update(over)
    return types.SimpleNamespace(**base)


def test_dry_run_validates_without_model_calls(tmp_path, capsys):
    bench, cfg = _bench(tmp_path, {"a": "pass", "b": "fail"})
    code = cal.run(_ns(bench, cfg, dry_run=True))  # no call_model -> would crash if used
    assert code == 0
    out = capsys.readouterr().out
    assert "2 cases x 3 samples" in out
    # no calibration record produced by a dry run
    assert not (bench / "labels" / "calibration").exists()


def test_dry_run_rejects_bad_judge_engine(tmp_path):
    bench, cfg = _bench(tmp_path, {"a": "pass"}, judge_engine="gpt-9000")
    assert cal.run(_ns(bench, cfg, dry_run=True)) == 2


def test_dry_run_flags_missing_golden(tmp_path):
    bench, cfg = _bench(tmp_path, {"a": "pass"})
    (bench / "labels" / "golden.jsonl").unlink()
    assert cal.run(_ns(bench, cfg, dry_run=True)) == 2


def test_full_run_writes_calibration_record_and_passes(tmp_path):
    bench, cfg = _bench(
        tmp_path,
        {"a": "pass", "b": "fail", "c": "pass"},
        contents={"a": "GOOD a", "b": "BAD b", "c": "GOOD c"},
    )
    code = cal.run(_ns(bench, cfg, samples=1), call_model=_judge)
    assert code == 0
    records = list((bench / "labels" / "calibration").glob("*.json"))
    assert len(records) == 1
    rec = json.loads(records[0].read_text())
    assert rec["kappa"] == 1.0 and rec["passed"] is True and rec["n"] == 3
    # the record is exactly what gate's calibration lookup reads
    assert gate_mod.latest_calibration_kappa(bench) == 1.0


def test_full_run_below_threshold_exits_nonzero(tmp_path):
    bench, cfg = _bench(
        tmp_path, {"a": "pass", "b": "fail"}, contents={"a": "GOOD", "b": "GOOD"},
    )
    code = cal.run(_ns(bench, cfg, samples=1), call_model=_judge)
    assert code == 2  # judge passes both, human split -> κ below 0.7


def test_full_run_missing_content_fails_loud(tmp_path):
    bench, cfg = _bench(tmp_path, {"a": "pass"}, contents={})  # no artefact content
    with pytest.raises(cal.CalibrationError):
        cal.run(_ns(bench, cfg, samples=1), call_model=_judge)


# --------------------------------------------------------------------------
# label command
# --------------------------------------------------------------------------
def test_label_appends_canonical_row(tmp_path):
    artefact = tmp_path / "case.json"
    artefact.write_text(json.dumps({"case_id": "case-001"}))
    out = tmp_path / "labels.jsonl"
    code = main(["label", str(artefact), "--verdict", "pass",
                 "--assertion", "risk_explained", "--note", "ok", "-o", str(out)])
    assert code == 0
    row = json.loads(out.read_text().splitlines()[0])
    assert row["case_id"] == "case-001"
    assert row["verdict"] == "pass" and row["assertion_id"] == "risk_explained"
    assert list(row.keys()) == list(label_mod.LABEL_KEYS)


def test_label_case_level_verdict_null_assertion(tmp_path):
    artefact = tmp_path / "case.json"
    artefact.write_text(json.dumps({"case_id": "case-002"}))
    out = tmp_path / "labels.jsonl"
    main(["label", str(artefact), "--verdict", "fail", "-o", str(out)])
    main(["label", str(artefact), "--verdict", "pass", "-o", str(out)])  # appends, ledger grows
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["assertion_id"] is None


def test_label_rejects_unreadable_case_id(tmp_path):
    with pytest.raises(label_mod.LabelError):
        main(["label", str(tmp_path / "missing.json"), "--verdict", "pass",
              "-o", str(tmp_path / "l.jsonl")])


# --------------------------------------------------------------------------
# GAP 3: the shipped dropdowns-skill golden set is non-degenerate
# --------------------------------------------------------------------------
# A golden set that is all "pass" gives every judge kappa = 1.0 for free
# (single-category collapse: expected agreement pe = 1.0), so calibration can
# never discriminate a good judge from a lazy one. These prove the real golden
# set now carries both classes and that kappa moves with judge quality.
_GOLDEN = (
    pathlib.Path(__file__).resolve().parents[1]
    / "benchmarks" / "dropdowns-skill" / "labels" / "golden.jsonl"
)


def _all_pass_judge(system, user):
    return json.dumps(
        {"dimension_scores": {"overall": 1.0}, "rationale": "x", "injection_suspected": False}
    )


def test_shipped_golden_has_both_verdict_classes():
    verdicts = cal.load_golden_verdicts(_GOLDEN)
    assert set(verdicts.values()) == {"pass", "fail"}, verdicts
    assert list(verdicts.values()).count("fail") >= 1


def test_shipped_golden_kappa_is_meaningful_not_degenerate():
    verdicts = cal.load_golden_verdicts(_GOLDEN)
    # Content encodes each human verdict so the deterministic judge can mirror
    # (pass) or diverge (all-pass) from the labels.
    items = [
        cal.CalibrationItem(cid, "GOOD" if v == "pass" else "BAD", v)
        for cid, v in verdicts.items()
    ]

    # A judge that reproduces the human labels: perfect kappa. Because both
    # classes are present, pe < 1.0, so this 1.0 is genuine agreement, not the
    # degenerate single-category 1.0.
    agree = cal.run_calibration(items, _judge, k=1, golden_agreement_min=0.7)
    assert agree.kappa == 1.0 and agree.passed
    assert agree.n == len(verdicts)

    # A lazy judge that calls everything pass now FAILS calibration — the exact
    # discrimination an all-pass golden set could never provide.
    lazy = cal.run_calibration(items, _all_pass_judge, k=1, golden_agreement_min=0.7)
    assert lazy.kappa < 0.7 and not lazy.passed
