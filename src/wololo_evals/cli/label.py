"""``label``: record human verdicts into ``labels.jsonl`` (open, Apache-2.0).

``gh-aw-evals label <run-artefact> --verdict pass|fail [--assertion <id>]
--note "..."`` appends one row per human verdict (README §17). ``--assertion``
is optional; when omitted the row is a case-level verdict (``assertion_id``
null). Labels are the ground truth ``judge calibrate`` scores the LLM judge
against, and the substrate for validating deterministic scorers — so the file
is the benchmark's ``golden_set``.

Rows are canonical one-object-per-line JSONL, appended (the file is an
accumulating ledger; existing rows are never rewritten). Pure stdlib.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import Optional

__all__ = [
    "LabelError",
    "LABEL_KEYS",
    "build_label_row",
    "append_label",
    "add_arguments",
    "run",
]

#: Row key order (matches the committed golden.jsonl shape).
LABEL_KEYS = (
    "case_id",
    "artefact_ref",
    "verdict",
    "assertion_id",
    "labeller",
    "ts",
    "note",
)

_VERDICTS = ("pass", "fail")


class LabelError(ValueError):
    """The artefact could not be read or a required field is missing."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_label_row(
    *,
    case_id: str,
    artefact_ref: str,
    verdict: str,
    assertion_id: Optional[str] = None,
    labeller: str = "human",
    ts: Optional[str] = None,
    note: str = "",
) -> dict:
    """Build one canonical label row. ``verdict`` must be ``pass`` or ``fail``."""
    if verdict not in _VERDICTS:
        raise LabelError(f"verdict {verdict!r} not in {_VERDICTS}")
    if not case_id:
        raise LabelError("case_id is required")
    return {
        "case_id": case_id,
        "artefact_ref": artefact_ref,
        "verdict": verdict,
        "assertion_id": assertion_id,
        "labeller": labeller,
        "ts": ts or _now_iso(),
        "note": note,
    }


def append_label(labels_path: str | pathlib.Path, row: dict) -> None:
    """Append ``row`` to ``labels_path`` as one compact JSONL line (create if new)."""
    p = pathlib.Path(labels_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _case_id_from_artefact(artefact_path: pathlib.Path) -> Optional[str]:
    if not artefact_path.is_file():
        return None
    try:
        payload = json.loads(artefact_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    cid = payload.get("case_id")
    return str(cid) if cid else None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def add_arguments(parser) -> None:  # noqa: ANN001 - argparse subparser
    parser.add_argument("artefact", help="run artefact being labelled (a case JSON)")
    parser.add_argument("--verdict", required=True, choices=_VERDICTS)
    parser.add_argument("--assertion", default=None, help="assertion id (omit for case-level)")
    parser.add_argument("--note", default="", help="free-text rationale")
    parser.add_argument("--labeller", default="human")
    parser.add_argument("--case-id", default=None, help="override (else read from the artefact)")
    parser.add_argument(
        "--artefact-ref", default=None,
        help="stored artefact_ref (else the artefact path as given)",
    )
    parser.add_argument(
        "-o", "--out", default=None,
        help="labels.jsonl to append to (default: benchmark golden set 'labels/golden.jsonl' "
        "beside the artefact, else ./labels.jsonl)",
    )
    parser.add_argument("--ts", default=None, help="ISO timestamp (default: now, UTC)")


def _default_out(artefact_path: pathlib.Path) -> pathlib.Path:
    """Walk up from the artefact for a benchmark ``labels/golden.jsonl``; else cwd."""
    for parent in artefact_path.resolve().parents:
        golden = parent / "labels" / "golden.jsonl"
        if golden.parent.is_dir() or (parent / "benchmark.json").is_file():
            return golden
    return pathlib.Path.cwd() / "labels.jsonl"


def run(args) -> int:  # noqa: ANN001 - argparse namespace
    artefact_path = pathlib.Path(args.artefact)
    case_id = args.case_id or _case_id_from_artefact(artefact_path)
    if not case_id:
        raise LabelError(
            f"could not read case_id from {artefact_path}; pass --case-id explicitly"
        )
    row = build_label_row(
        case_id=case_id,
        artefact_ref=args.artefact_ref or str(args.artefact),
        verdict=args.verdict,
        assertion_id=args.assertion,
        labeller=args.labeller,
        ts=args.ts,
        note=args.note,
    )
    out = pathlib.Path(args.out) if args.out else _default_out(artefact_path)
    append_label(out, row)
    scope = f"assertion {args.assertion!r}" if args.assertion else "case-level"
    print(f"appended {args.verdict} label ({scope}) for {case_id} -> {out}")
    return 0
