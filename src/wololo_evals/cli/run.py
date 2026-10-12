"""``run``: evaluate a skill from its ``eval.yaml`` in one command.

Runs the no-skill arm and the skill arm over the benchmark, gates the skill arm
against the no-skill arm, and writes ``eval-gate-report.json`` plus
``eval-gate-report.md`` next to the config. Exit codes are the CI contract:
``0`` pass, ``1`` regression, ``2`` refused.

Two modes. **Live** drives the configured engine and spends money, so it
refuses to start without ``max_cost_usd`` (config or ``--max-cost-usd``); the
cap is shared by both arms. **Replay** (``--replay <archive>``) re-scores
recorded runs and spends nothing; the archive holds one replay-engine directory
per arm, ``no-skill/`` and ``skill/``, each laid out
``<case>/seed-<n>/{provenance.json,<engine>.raw.jsonl,workspace/}``.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import sys
from typing import Callable, Optional

from . import gate as _gate
from . import matrix as _matrix
from .config import AssetEntry, MatrixConfig, ModelEntry, PromptEntry
from .eval_config import EvalConfig, EvalConfigError, load_eval_config, resolve_benchmark
from ..core import interfaces as ci

__all__ = ["add_arguments", "run"]

REPORT_JSON = "eval-gate-report.json"
REPORT_MD = "eval-gate-report.md"
ARMS = ("no-skill", "skill")
_REFUSED = 2


def add_arguments(parser) -> None:  # noqa: ANN001 - argparse subparser
    parser.add_argument("--config", required=True, help="path to eval.yaml")
    parser.add_argument(
        "--replay", default=None, metavar="ARCHIVE",
        help="score archived runs (ARCHIVE/no-skill and ARCHIVE/skill) at zero spend",
    )
    parser.add_argument("--engine", default=None, help="override eval.yaml engine")
    parser.add_argument("--model", default=None, help="override eval.yaml model")
    parser.add_argument("--benchmark", default=None, help="override eval.yaml benchmark")
    parser.add_argument(
        "--max-cost-usd", type=float, default=None,
        help="override eval.yaml max_cost_usd; a live run refuses without one",
    )
    parser.add_argument(
        "--max-regression", type=float, default=None,
        help="override eval.yaml thresholds.max_regression",
    )
    parser.add_argument(
        "--out", default=None,
        help="where the per-arm run trees go (default: .wololo-evals next to the config)",
    )
    parser.add_argument(
        "--judge", choices=("on", "off"), default=None,
        help="run the LLM judge for plain-text assertions (spends money; off by default)",
    )


def _refuse(message: str) -> int:
    print(f"run: refused: {message}", file=sys.stderr)
    return _REFUSED


def _apply_overrides(cfg: EvalConfig, args) -> EvalConfig:  # noqa: ANN001
    changes = {}
    if args.engine:
        if args.engine not in ci.ENGINE_IDS:
            raise EvalConfigError(f"--engine must be one of {', '.join(ci.ENGINE_IDS)}")
        changes["engine"] = args.engine
    if args.model:
        changes["model"] = args.model
    if args.benchmark:
        changes["benchmark"] = args.benchmark
    if args.max_cost_usd is not None:
        if args.max_cost_usd <= 0:
            raise EvalConfigError("--max-cost-usd must be greater than 0")
        changes["max_cost_usd"] = args.max_cost_usd
    if args.max_regression is not None:
        if args.max_regression < 0:
            raise EvalConfigError("--max-regression must be at least 0")
        changes["max_regression"] = args.max_regression
    return dataclasses.replace(cfg, **changes)


def _arm_config(
    cfg: EvalConfig, benchmark: pathlib.Path, arm: str, out: pathlib.Path,
    replay_dir: Optional[pathlib.Path],
) -> MatrixConfig:
    """One arm as a matrix config. Both arms share engine, model and prompt id
    (the gate pairs on that triple); the skill arm alone mounts the asset."""
    engine = "replay" if replay_dir else cfg.engine
    raw: dict = {"__config_stem__": arm}
    if replay_dir:
        raw["engines"] = {"replay": {"version_range": "*", "runs_dir": str(replay_dir)}}
    mount = arm == "skill" and replay_dir is None
    return MatrixConfig(
        schema_version="2.0.0",
        benchmark=str(benchmark),
        output_dir=str(out / arm),
        base_dir=cfg.base_dir,
        seeds=1,
        expected_skill=cfg.skill_slug,
        models=(ModelEntry(id=cfg.model, engine=engine, model=cfg.model),),
        prompts=(PromptEntry("arm"),),
        assets=(AssetEntry(id=cfg.skill_slug, kind="agent_skill", path=str(cfg.asset)),) if mount else (),
        raw=raw,
    )


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _spend(matrix_dir: pathlib.Path) -> float:
    summary = json.loads((matrix_dir / "matrix_summary.json").read_text(encoding="utf-8"))
    return float(summary.get("cumulative_spend_usd") or 0.0)


def _mean_score(matrix_dir: pathlib.Path) -> float:
    combos = _gate._load_run(matrix_dir).combos
    scores = [_gate._scorecard_benchmark_score(c.scorecard) for c in combos]
    return sum(scores) / len(scores) if scores else 0.0


def run(
    args,  # noqa: ANN001 - argparse namespace
    *,
    adapter_factory: Optional[Callable[[str], object]] = None,
) -> int:
    """``adapter_factory`` replaces the live engines; tests inject fakes."""
    try:
        cfg = _apply_overrides(load_eval_config(args.config), args)
        benchmark = resolve_benchmark(cfg)
    except EvalConfigError as exc:
        return _refuse(str(exc))

    replay = pathlib.Path(args.replay).resolve() if args.replay else None
    if replay:
        missing = [a for a in ARMS if not (replay / a).is_dir()]
        if missing:
            return _refuse(f"--replay {replay} has no {' or '.join(m + '/' for m in missing)}")
    else:
        if cfg.engine == "replay":
            return _refuse("engine 'replay' scores archives; pass --replay <archive>")
        if cfg.max_cost_usd is None:
            return _refuse(
                "a live run spends money and needs a cap: set `max_cost_usd` in "
                f"{cfg.path.name} or pass --max-cost-usd"
            )

    out = pathlib.Path(args.out).resolve() if args.out else cfg.base_dir / ".wololo-evals"
    stamp = _timestamp()
    remaining = cfg.max_cost_usd
    dirs: dict[str, pathlib.Path] = {}
    spent = 0.0
    for arm in ARMS:
        arm_cfg = _arm_config(cfg, benchmark, arm, out, replay / arm if replay else None)
        from .main import _live_seams  # lazy: main imports this module

        live_factory, normaliser_factory, grader = _live_seams(arm_cfg, args)
        factory = adapter_factory or live_factory
        dirs[arm] = _matrix.run_matrix(
            arm_cfg, factory, grader, timestamp=stamp,
            normaliser_factory=normaliser_factory,
            max_cost_usd=None if replay else remaining,
        )
        arm_spend = _spend(dirs[arm])
        spent += arm_spend
        if remaining is not None:
            remaining = max(remaining - arm_spend, 0.0)

    try:
        verdict = _gate.evaluate(
            dirs["skill"], dirs["no-skill"],
            max_regression=cfg.max_regression, benchmark_dir=benchmark,
        )
    except _gate.GateError as exc:
        return _refuse(str(exc))

    scores = {"no_skill": _mean_score(dirs["no-skill"]), "skill": _mean_score(dirs["skill"])}
    verdict = dataclasses.replace(
        verdict, recorded=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    )
    if verdict.verdict == "pass" and scores["skill"] < cfg.pass_score:
        verdict = dataclasses.replace(
            verdict, verdict="regression",
            reason=(
                f"skill arm scored {scores['skill']:.4f}, below thresholds.pass "
                f"{cfg.pass_score:g}"
            ),
        )

    report = verdict.as_json()
    report.update(
        mode="replay" if replay else "live",
        spend_usd=round(spent, 6),
        thresholds={"pass": cfg.pass_score, "max_regression": cfg.max_regression},
        scores=scores,
    )
    json_path, md_path = cfg.base_dir / REPORT_JSON, cfg.base_dir / REPORT_MD
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(
        verdict.as_markdown()
        + f"\n## Run\n\n- mode: `{report['mode']}`\n- spend: `${spent:.4f}`\n"
        f"- no-skill score: `{scores['no_skill']:.4f}`\n- skill score: `{scores['skill']:.4f}`\n"
        f"- thresholds: pass `{cfg.pass_score:g}`, max_regression `{cfg.max_regression:g}`\n",
        encoding="utf-8",
    )
    print(md_path.read_text(encoding="utf-8"))
    print(f"report written to {json_path}")
    return verdict.exit_code
