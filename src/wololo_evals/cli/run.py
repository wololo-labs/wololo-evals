"""``run``: evaluate a skill from the ``eval:`` block of its ``apm.yml`` in one command.

Runs the no-skill arm and the skill arm over the benchmark, gates the skill arm
against the no-skill arm, and writes ``eval-gate-report.json`` plus
``eval-gate-report.md`` next to the config (or where its ``report:`` key says). Exit codes are the CI contract:
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
import os
import pathlib
import shutil
import sys
from typing import Callable, Optional

from . import gate as _gate
from . import matrix as _matrix
from .config import AssetEntry, MatrixConfig, ModelEntry, PromptEntry
from .eval_config import EvalConfig, EvalConfigError, load_eval_config, resolve_benchmark
from ..core import interfaces as ci

__all__ = ["add_arguments", "run"]

ARMS = ("no-skill", "skill")
_REFUSED = 2


def add_arguments(parser) -> None:  # noqa: ANN001 - argparse subparser
    parser.add_argument(
        "--config", required=True,
        help="path to apm.yml (its `eval:` block); a legacy eval.yaml still works",
    )
    parser.add_argument(
        "--replay", default=None, metavar="ARCHIVE",
        help="score archived runs (ARCHIVE/no-skill and ARCHIVE/skill) at zero spend",
    )
    parser.add_argument("--engine", default=None, help="override the config's engine")
    parser.add_argument("--model", default=None, help="override the config's model")
    parser.add_argument("--benchmark", default=None, help="override the config's benchmark")
    parser.add_argument(
        "--max-cost-usd", type=float, default=None,
        help="override the config's max_cost_usd; a live run refuses without one",
    )
    parser.add_argument(
        "--max-regression", type=float, default=None,
        help="override the config's thresholds.max_regression",
    )
    parser.add_argument(
        "--out", default=None,
        help="where the per-arm run trees go (default: .wololo-evals next to the config)",
    )
    parser.add_argument(
        "--record", default=None, metavar="DIR",
        help=(
            "after a live run, write both arms as a replay archive (DIR/no-skill, "
            "DIR/skill) that --replay can score later at zero spend"
        ),
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


#: Files the replay engine regenerates itself; an archive keeps only what a run left behind.
_REGENERATED = ("*.raw.jsonl", "*.stdout.log", "*.stderr.log", "trace.jsonl", "prompt.md")
_SECRET_NAME = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _secrets() -> list[str]:
    """Values of environment variables that look like credentials. An archive is
    uploaded, and a run's own stream can echo whatever the agent could read."""
    return sorted(
        {v for k, v in os.environ.items()
         if len(v) >= 8 and any(w in k.upper() for w in _SECRET_NAME)},
        key=len, reverse=True,
    )


def _redact(root: pathlib.Path, secrets: list[str]) -> None:
    """Scrub secret values from every archived file, as bytes so a binary file
    cannot slip through unread. An I/O error propagates: a record that could
    not be scrubbed must fail, not ship."""
    needles = [v.encode("utf-8") for v in secrets]
    for f in root.rglob("*"):
        if f.is_symlink() or not f.is_file():
            continue
        data = f.read_bytes()
        scrubbed = data
        for needle in needles:
            scrubbed = scrubbed.replace(needle, b"[redacted]")
        if scrubbed != data:
            f.write_bytes(scrubbed)


def _skip_links(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if (pathlib.Path(directory) / n).is_symlink()}


def _has_link(root: pathlib.Path) -> Optional[pathlib.Path]:
    return next((p for p in root.rglob("*") if p.is_symlink()), None)


def _record(matrix_dir: pathlib.Path, dest: pathlib.Path, model: str, stamp: str) -> int:
    """Export one arm's retained workspaces as a replay-engine archive."""
    summary = json.loads((matrix_dir / "matrix_summary.json").read_text(encoding="utf-8"))
    count = 0
    for combo in summary["combos"]:
        for ws in sorted((matrix_dir / combo["combo_dir"] / "seeds").glob("*/workspace/*")):
            raw = next((ws / ci.ADVISORY_DIR).glob("*.raw.jsonl"), None)
            if raw is None or raw.is_symlink():
                continue  # the run never started (cost cap hit): nothing to replay
            out = dest / ws.name / f"seed-{ws.parents[1].name}"
            patterns = shutil.ignore_patterns(*_REGENERATED)
            shutil.copytree(
                ws, out / "workspace", symlinks=True,
                ignore=lambda d, names: set(patterns(d, names)) | _skip_links(d, names),
            )
            shutil.copy(raw, out / raw.name)
            (out / "provenance.json").write_text(
                json.dumps(
                    {"engine_id": raw.name.split(".")[0], "model_id": model,
                     "recorded": f"live run {stamp}"},
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            _redact(out, _secrets())
            count += 1
    return count


def _next_iteration(out: pathlib.Path) -> int:
    """One more than the runs already under ``out``: a second run in the same
    second used to collide on the timestamp and refuse."""
    seen = [len(list((out / arm / "iterations").glob("*"))) for arm in ARMS]
    return max(seen) + 1


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _spend(matrix_dir: pathlib.Path) -> float:
    summary = json.loads((matrix_dir / "matrix_summary.json").read_text(encoding="utf-8"))
    return float(summary.get("cumulative_spend_usd") or 0.0)


def _mean_score(matrix_dir: pathlib.Path) -> float:
    combos = _gate._load_run(matrix_dir).combos
    scores = [_gate._scorecard_benchmark_score(c.scorecard) for c in combos]
    return sum(scores) / len(scores) if scores else 0.0


def _subject(cfg: EvalConfig) -> dict:
    """Which asset the verdict is about, from the CI environment.

    Like ``gate``, a subject with no repo or commit cannot tie the score to
    anything, so outside CI the report simply carries none.
    """
    repo, commit = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_SHA")
    if not (repo and commit):
        return {}
    return {
        "repo": f"https://github.com/{repo}",
        "path": os.path.relpath(cfg.asset, cfg.base_dir),
        "commit": commit,
    }


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
        link = _has_link(replay)
        if link:
            return _refuse(
                f"--replay archive contains a symlink ({link.relative_to(replay)}); "
                "an archive is plain files, and a link would be followed out of it"
            )
    else:
        if cfg.engine == "replay":
            return _refuse("engine 'replay' scores archives; pass --replay <archive>")
        if cfg.max_cost_usd is None:
            return _refuse(
                "a live run spends money and needs a cap: set `max_cost_usd` in "
                f"{cfg.path.name} or pass --max-cost-usd"
            )

    if args.record and replay:
        return _refuse("--record saves a live run; there is nothing to record under --replay")

    out = pathlib.Path(args.out).resolve() if args.out else cfg.base_dir / ".wololo-evals"
    stamp = _timestamp()
    iteration = _next_iteration(out)
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
            normaliser_factory=normaliser_factory, iteration=iteration,
            max_cost_usd=None if replay else remaining,
        )
        arm_spend = _spend(dirs[arm])
        spent += arm_spend
        if remaining is not None:
            remaining = max(remaining - arm_spend, 0.0)

    if args.record:
        for arm in ARMS:
            _record(dirs[arm], pathlib.Path(args.record).resolve() / arm, cfg.model, stamp)

    try:
        verdict = _gate.evaluate(
            dirs["skill"], dirs["no-skill"],
            max_regression=cfg.max_regression, benchmark_dir=benchmark,
        )
    except _gate.GateError as exc:
        return _refuse(str(exc))

    scores = {"no_skill": _mean_score(dirs["no-skill"]), "skill": _mean_score(dirs["skill"])}
    verdict = dataclasses.replace(
        verdict,
        # Relative to the config: an absolute path would put the operator's
        # home directory in a report that is uploaded and published.
        run_dir=os.path.relpath(dirs["skill"], cfg.base_dir),
        baseline_dir=os.path.relpath(dirs["no-skill"], cfg.base_dir),
        subject=_subject(cfg),
        recorded=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
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
    json_path = cfg.report_json
    md_path = json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
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
