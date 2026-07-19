"""``replay``: re-score an archived matrix run with zero model spend
(open, Apache-2.0).

The offline ``replay`` engine (§14.4) is the substrate for CI self-tests: it
reads the recorded per-(case, seed) signals out of an archived matrix tree,
re-composes case scores through the same §16.1 path, and re-writes the §15.3
scorecards. Because composition and serialisation are deterministic, replay is
**byte-stable** — a scorecard that changes across replays is a broken evaluator,
not model noise.

Pure stdlib + ``core.interfaces`` + the CLI's own writers; no engine, no DB.
"""
from __future__ import annotations

import json
import pathlib
from typing import Optional

from ..core import interfaces as ci
from . import artefact_writer as aw

__all__ = ["replay", "ReplayError"]


class ReplayError(ValueError):
    """Archive is missing files replay needs (summary, seed case records)."""


def _signal_from_row(row: dict) -> ci.ScoredSignal:
    return ci.ScoredSignal(
        evaluator_id=str(row["evaluator_id"]),
        kind=str(row["kind"]),
        score=float(row["score"]),
        weight=float(row["weight"]),
        verdict=str(row["verdict"]),
        slot=row.get("slot"),
        details=row.get("details"),
        evidence=row.get("evidence"),
    )


def _result_from_payload(payload: dict, artefact_dir: str) -> ci.RunResult:
    return ci.RunResult(
        run_id=str(payload["run_id"]),
        status=str(payload["status"]),
        artefact_dir=artefact_dir,
        runtime_seconds=float(payload.get("runtime_seconds", 0.0)),
        cost_usd=float(payload.get("cost_usd", 0.0)),
        tokens_in=int(payload.get("tokens_in", 0)),
        tokens_out=int(payload.get("tokens_out", 0)),
        attempts=int(payload.get("attempts", 1)),
    )


def replay(
    archive_dir: str | pathlib.Path,
    out_dir: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Re-score an archived ``matrix-<ts>`` tree and re-write its scorecards.

    ``out_dir`` defaults to ``archive_dir`` (in place). Returns the directory
    the re-scored scorecards were written under.
    """
    archive = pathlib.Path(archive_dir)
    summary_path = ci.artefacts.matrix_summary_path(archive)
    if not summary_path.is_file():
        raise ReplayError(f"no matrix_summary.json under {archive}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    evaluation_type = str(summary["evaluation_type"])
    pass_threshold = float(summary["pass_threshold"])
    benchmark_id = str(summary["benchmark_id"])
    seeds_default = int(summary.get("seeds", 1))
    judge_disabled = bool(
        summary.get("provenance", {}).get("judge", {}).get("disabled", False)
    )

    out = pathlib.Path(out_dir) if out_dir is not None else archive

    for combo in summary["combos"]:
        run_dir = archive / combo["combo_dir"]
        seeds_root = run_dir / "seeds"
        if not seeds_root.is_dir():
            raise ReplayError(f"combo {combo['combo_id']!r} has no seeds/ under {run_dir}")

        outcomes: dict[str, list[aw.SeedOutcome]] = {}
        case_specs: dict[str, ci.CaseSpec] = {}
        seeds_seen: set[int] = set()
        for seed_case in sorted(seeds_root.glob("*/cases/*.json")):
            payload = json.loads(seed_case.read_text(encoding="utf-8"))
            cid = str(payload["case_id"])
            seed = int(payload["seed"])
            seeds_seen.add(seed)
            signals = [_signal_from_row(r) for r in payload.get("signals", [])]
            result = _result_from_payload(payload, str(seed_case.parent))
            composed = aw.compose_case(
                signals, evaluation_type,
                pass_threshold=pass_threshold, scored=result.scored,
                judge_disabled=judge_disabled,
            )
            outcomes.setdefault(cid, []).append(
                aw.SeedOutcome(seed=seed, result=result, composed=composed)
            )
            if cid not in case_specs:
                case_specs[cid] = ci.CaseSpec(
                    case_id=cid,
                    case_weight=float(payload.get("case_weight", 1.0)),
                    quarantined=bool(payload.get("quarantined", False)),
                )

        # stable case order (original scorecard order, else sorted)
        ordered_ids = _archived_case_order(run_dir) or sorted(case_specs)
        cases = [case_specs[cid] for cid in ordered_ids if cid in case_specs]
        seeds_configured = seeds_default or (max(seeds_seen) + 1 if seeds_seen else 1)
        artefact_refs = {
            cid: str(ci.artefacts.case_aggregate_path(run_dir, cid).relative_to(run_dir))
            for cid in case_specs
        }

        scorecard = aw.build_scorecard(
            aw.ComboRecord(
                combo_id=str(combo["combo_id"]),
                engine_id=str(combo["engine_id"]),
                model_id=str(combo["model_id"]),
                prompt_id=str(combo["prompt_id"]),
                role=str(combo["role"]),
                scorecard_path="",
                combo_dir="",
                asset_id=combo.get("asset_id"),
            ),
            benchmark_id,
            seeds_configured=seeds_configured,
            case_specs=cases,
            outcomes=outcomes,
            artefact_refs=artefact_refs,
            judge_disabled=judge_disabled,
        )
        aw.write_json(ci.artefacts.scorecard_path(out / combo["combo_dir"]), scorecard)

    return out


def _archived_case_order(run_dir: pathlib.Path) -> Optional[list[str]]:
    """Preserve the original scorecard's case order so a re-score is byte-stable
    against the committed archive."""
    sc = ci.artefacts.scorecard_path(run_dir)
    if not sc.is_file():
        return None
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
        return [str(c["case_id"]) for c in data.get("cases", [])]
    except (ValueError, KeyError):
        return None
