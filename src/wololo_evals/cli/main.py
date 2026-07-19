"""``wololo-evals-cli`` argparse entrypoint (open, Apache-2.0).

Routes the wave's file-first subcommands — ``cases compile``, ``matrix``
(``--dry-run``/``--smoke``/live), ``bench``, ``replay``, ``gate``, ``label``,
``judge calibrate`` — over the core library.
``main`` returns an ``int`` exit code and is invoked in-process by the tests;
the ``wololo-evals-cli`` console script wraps it in ``SystemExit(main())``.

Live ``matrix``/``bench`` lazily wire the real ``engines``/``trace`` seams; the
authoring/replay commands need no engine and no database.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
from typing import Optional, Sequence

from . import bench as _bench
from . import calibrate as _calibrate
from . import cases_compile
from . import gate as _gate
from . import label as _label
from . import matrix as _matrix
from . import replay as _replay
from .config import load_matrix_config

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wololo-evals-cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_cases = sub.add_parser("cases", help="case authoring commands")
    cases_sub = p_cases.add_subparsers(dest="cases_command", required=True)
    p_compile = cases_sub.add_parser("compile", help="cases.toml -> canonical cases.jsonl")
    p_compile.add_argument("cases_toml")
    p_compile.add_argument("--check", action="store_true", help="exit non-zero on drift")
    p_compile.add_argument("-o", "--out", help="output jsonl path (default: sibling cases.jsonl)")

    p_matrix = sub.add_parser("matrix", help="expand + run/dry-run the matrix")
    p_matrix.add_argument("--config", required=True)
    p_matrix.add_argument("--dry-run", action="store_true")
    p_matrix.add_argument("--smoke", action="store_true")
    p_matrix.add_argument("--max-cost-usd", type=float, default=None)
    p_matrix.add_argument("--allow-engine-drift", action="store_true")
    p_matrix.add_argument("--iteration", type=int, default=1)

    p_bench = sub.add_parser("bench", help="run one benchmark as a single combo")
    p_bench.add_argument("--config", required=True)
    p_bench.add_argument("--engine", default=None)
    p_bench.add_argument("--model", default=None)
    p_bench.add_argument("--out", default=None)

    p_replay = sub.add_parser("replay", help="re-score an archived matrix run (zero spend)")
    p_replay.add_argument("archive_dir")
    p_replay.add_argument("--out", default=None)

    p_gate = sub.add_parser("gate", help="candidate-vs-baseline A/B regression verdict")
    _gate.add_arguments(p_gate)

    p_label = sub.add_parser("label", help="record a human verdict into labels.jsonl")
    _label.add_arguments(p_label)

    p_judge = sub.add_parser("judge", help="judge governance commands")
    judge_sub = p_judge.add_subparsers(dest="judge_command", required=True)
    p_calibrate = judge_sub.add_parser(
        "calibrate", help="golden-set judge agreement (Cohen's kappa)"
    )
    _calibrate.add_arguments(p_calibrate)

    return parser


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _empty_grader(spec, result, trace):  # noqa: ANN001 - matches Grader seam
    """Default live grader: emits no signals. A real assertion-engine grader is
    injected by callers (or a future integration); without one, cases score 0."""
    return ()


def _live_seams():
    """Lazily build the (adapter_factory, normaliser_factory, grader) triple
    from the real engine/trace packages. Kept lazy so authoring/replay/dry-run
    never import an engine."""
    from .. import engines as engines_pkg
    from ..trace import get_normaliser

    def adapter_factory(engine_id: str):
        return engines_pkg.get_adapter(engine_id)

    def normaliser_factory(engine_id: str):
        try:
            return get_normaliser(engine_id)
        except Exception:  # noqa: BLE001 - no normaliser is a soft miss
            return None

    return adapter_factory, normaliser_factory, _empty_grader


def _cmd_cases_compile(args: argparse.Namespace) -> int:
    if args.check:
        ok, msg = cases_compile.check(args.cases_toml, args.out)
        print(msg)
        return 0 if ok else 1
    text = cases_compile.compile(args.cases_toml)
    target = (
        pathlib.Path(args.out)
        if args.out
        else pathlib.Path(args.cases_toml).with_suffix(".jsonl")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
    return 0


def _cmd_matrix(args: argparse.Namespace) -> int:
    config = load_matrix_config(args.config)
    if args.dry_run:
        print(_matrix.render_plan(_matrix.plan(config)), end="")
        return 0
    adapter_factory, normaliser_factory, grader = _live_seams()
    matrix_dir = _matrix.run_matrix(
        config,
        adapter_factory,
        grader,
        timestamp=_timestamp(),
        iteration=args.iteration,
        normaliser_factory=normaliser_factory,
        max_cost_usd=args.max_cost_usd,
        smoke=args.smoke,
        allow_engine_drift=args.allow_engine_drift,
    )
    print(f"matrix written to {matrix_dir}")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    config = load_matrix_config(args.config)
    adapter_factory, normaliser_factory, grader = _live_seams()
    scorecard = _bench.run_bench(
        config,
        adapter_factory,
        grader,
        timestamp=_timestamp(),
        engine=args.engine,
        model=args.model,
        out=args.out,
        normaliser_factory=normaliser_factory,
    )
    print(f"scorecard written to {scorecard}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    out = _replay.replay(args.archive_dir, args.out)
    print(f"replayed scorecards under {out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "cases":
        return _cmd_cases_compile(args)
    if args.command == "matrix":
        return _cmd_matrix(args)
    if args.command == "bench":
        return _cmd_bench(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "gate":
        return _gate.run(args)
    if args.command == "label":
        return _label.run(args)
    if args.command == "judge" and args.judge_command == "calibrate":
        return _calibrate.run(args)
    return 2  # unreachable: subparsers are required


if __name__ == "__main__":
    sys.exit(main())
