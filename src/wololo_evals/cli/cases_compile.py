"""``cases compile``: casebook (``cases.toml``) -> canonical ``cases.jsonl``
(open, Apache-2.0).

Implements the §11.1 authoring semantics — ``*_file`` resolution, assertion
attachment by id, ``[path_sets]`` union expansion, distractor validation — and
the §11.1 canonical serialisation (``json.dumps(..., ensure_ascii=False,
separators=(",", ":"))``, header row first, fields in §11.2 order, trailing
newline). ``--check`` recompiles in memory and diffs byte-for-byte.

Pure stdlib (``tomllib``/``json``); the only package import is
``core.interfaces`` for ``CaseSpec`` (used by :func:`load_cases`).
"""
from __future__ import annotations

import json
import pathlib
from typing import Mapping, Optional, Sequence

from ..core import interfaces as ci

__all__ = [
    "compile",
    "check",
    "load_cases",
    "CasesCompileError",
    "CASES_SCHEMA_VERSION",
]

CASES_SCHEMA_VERSION = "2.0.0"

#: Canonical §11.2 field order. Optional authoring extras (``title``,
#: ``difficulty``, ``tags``) are spliced in at their canonical positions only
#: when the case sets them; every other field is always emitted.
_ALWAYS = (
    "case_id",
    "fixture_id",
    "case_weight",
    "role",
    "input_text",
    "turns",
    "expected_outcome",
    "expected_paths",
    "expected_skill",
    "quarantined",
    "metadata",
    "assertions",
)


class CasesCompileError(ValueError):
    """Fail-loud compile error (bad ref, illegal distractor combo, ...)."""


def _canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _read_text(casebook_dir: pathlib.Path, rel: str) -> str:
    p = casebook_dir / rel
    if not p.is_file():
        raise CasesCompileError(f"referenced file not found: {rel} (resolved {p})")
    return p.read_text(encoding="utf-8")


def _read_json(casebook_dir: pathlib.Path, rel: str) -> Mapping[str, object]:
    return json.loads(_read_text(casebook_dir, rel))


def _deep_merge(base: Mapping[str, object], over: Mapping[str, object]) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def _dedup(seq: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _attach_assertions(
    ids: Sequence[str],
    table: Mapping[str, str],
    weights: Mapping[str, float],
) -> list[dict]:
    rows: list[dict] = []
    for aid in ids:
        if aid not in table:
            raise CasesCompileError(
                f"assertion id {aid!r} attached but not defined in [assertions]"
            )
        rows.append(
            {
                "id": aid,
                "text": table[aid],
                "weight": float(weights.get(aid, 1.0)),
                "source": "assertions",
            }
        )
    return rows


def _turns(raw_turns: Sequence[Mapping[str, object]]) -> list[dict]:
    out: list[dict] = []
    for t in raw_turns:
        row: dict = {"role": str(t.get("role", "user"))}
        if "text" in t:
            row["text"] = t["text"]
        if "script" in t:
            row["script"] = t["script"]
        out.append(row)
    return out


def _build_row(
    case: Mapping[str, object],
    *,
    casebook_dir: pathlib.Path,
    defaults: Mapping[str, object],
    assertion_table: Mapping[str, str],
    path_sets: Mapping[str, Sequence[str]],
    allow_distractor: Optional[bool],
) -> dict:
    case_id = str(case.get("case_id") or "")
    if not case_id:
        raise CasesCompileError("every [[cases]] row requires case_id")

    role = str(case.get("role", "treatment"))
    if role not in ci.CASE_ROLES:
        raise CasesCompileError(f"case {case_id!r} role {role!r} not in {ci.CASE_ROLES}")

    # --- expected_outcome + distractor rules (§11.1) ---
    outcome_set = "expected_outcome" in case
    expected_outcome = str(case.get("expected_outcome", "pass"))
    if role == "distractor":
        if allow_distractor is False:
            raise CasesCompileError(
                f"case {case_id!r}: distractor role only valid for "
                f"skill_behaviour/robustness benchmarks"
            )
        if outcome_set and expected_outcome != "no_activation":
            raise CasesCompileError(
                f"case {case_id!r}: distractor requires expected_outcome="
                f"'no_activation', got {expected_outcome!r}"
            )
        expected_outcome = "no_activation"
    if expected_outcome not in ci.EXPECTED_OUTCOMES:
        raise CasesCompileError(
            f"case {case_id!r} expected_outcome {expected_outcome!r} "
            f"not in {ci.EXPECTED_OUTCOMES}"
        )

    # --- input text / turns ---
    turns_raw = case.get("turns")
    if turns_raw:
        turns: Optional[list[dict]] = _turns(turns_raw)  # type: ignore[arg-type]
        input_text = ""
    else:
        turns = None
        if "input_text" in case:
            input_text = str(case["input_text"])
        elif case.get("input_text_file"):
            input_text = _read_text(casebook_dir, str(case["input_text_file"]))
        elif defaults.get("input_text_file"):
            input_text = _read_text(casebook_dir, str(defaults["input_text_file"]))
        else:
            input_text = ""

    # --- assertion attachment (§11.1) ---
    attached_ids = (
        list(case["assertions"])
        if "assertions" in case
        else list(defaults.get("assertions", []))
    )
    weights: dict[str, float] = {
        str(k): float(v)
        for k, v in dict(defaults.get("assertion_weights", {})).items()
    }
    weights.update(
        {str(k): float(v) for k, v in dict(case.get("assertion_weights", {})).items()}
    )
    assertions = _attach_assertions(attached_ids, assertion_table, weights)

    # --- expected_paths union (§11.1) ---
    refs = list(defaults.get("expected_path_refs", [])) + list(
        case.get("expected_path_refs", [])
    )
    expanded: list[str] = []
    for ref in refs:
        if ref not in path_sets:
            raise CasesCompileError(f"case {case_id!r}: unknown path_set ref {ref!r}")
        expanded.extend(str(x) for x in path_sets[ref])
    expected_paths = _dedup(
        expanded
        + [str(x) for x in defaults.get("expected_paths", [])]
        + [str(x) for x in case.get("expected_paths", [])]
    )

    # --- metadata deep-merge (metadata_file <- inline) ---
    metadata: dict = {}
    if case.get("metadata_file"):
        metadata = dict(_read_json(casebook_dir, str(case["metadata_file"])))
    if isinstance(case.get("metadata"), Mapping):
        metadata = _deep_merge(metadata, case["metadata"])  # type: ignore[arg-type]

    # --- assemble in canonical field order ---
    row: dict = {}
    row["case_id"] = case_id
    row["fixture_id"] = case.get("fixture_id")
    row["case_weight"] = float(case.get("case_weight", 1.0))
    row["role"] = role
    if "title" in case:
        row["title"] = str(case["title"])
    if "difficulty" in case:
        row["difficulty"] = str(case["difficulty"])
    if "tags" in case:
        row["tags"] = [str(t) for t in case["tags"]]
    row["input_text"] = input_text
    row["turns"] = turns
    row["expected_outcome"] = expected_outcome
    row["expected_paths"] = expected_paths
    row["expected_skill"] = case.get("expected_skill")
    row["quarantined"] = bool(case.get("quarantined", False))
    row["metadata"] = metadata
    row["assertions"] = assertions
    return row


def _detect_allow_distractor(casebook_dir: pathlib.Path) -> Optional[bool]:
    """If a sibling ``benchmark.json`` exists, its ``evaluation_type`` decides
    whether distractor cases are legal (§11.1). ``None`` = unknown (no check)."""
    bj = casebook_dir / "benchmark.json"
    if not bj.is_file():
        return None
    try:
        etype = str(json.loads(bj.read_text(encoding="utf-8"))["evaluation_type"])
    except (KeyError, ValueError):
        return None
    return etype in ("skill_behaviour", "robustness")


def compile(cases_toml: str | pathlib.Path) -> str:  # noqa: A001 - matches CLI verb
    """Compile ``cases.toml`` into the canonical ``cases.jsonl`` text (§11.1).

    Returns the full file body: a ``{"schema_version": ...}`` header line, one
    canonical row per ``[[cases]]`` entry in authoring order, trailing newline.
    """
    import tomllib

    path = pathlib.Path(cases_toml)
    casebook_dir = path.parent
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    defaults = dict(data.get("defaults", {}))
    assertion_table = {str(k): str(v) for k, v in dict(data.get("assertions", {})).items()}
    path_sets = {str(k): list(v) for k, v in dict(data.get("path_sets", {})).items()}
    allow_distractor = _detect_allow_distractor(casebook_dir)

    cases = data.get("cases", [])
    if not cases:
        raise CasesCompileError(f"{path} declares no [[cases]]")

    lines = [_canonical({"schema_version": CASES_SCHEMA_VERSION})]
    seen_ids: set[str] = set()
    for case in cases:
        row = _build_row(
            case,
            casebook_dir=casebook_dir,
            defaults=defaults,
            assertion_table=assertion_table,
            path_sets=path_sets,
            allow_distractor=allow_distractor,
        )
        if row["case_id"] in seen_ids:
            raise CasesCompileError(f"duplicate case_id {row['case_id']!r}")
        seen_ids.add(row["case_id"])
        lines.append(_canonical(row))
    return "\n".join(lines) + "\n"


def check(
    cases_toml: str | pathlib.Path, jsonl_path: str | pathlib.Path | None = None
) -> tuple[bool, str]:
    """Recompile in memory and diff byte-for-byte against the checked-in file.

    Returns ``(ok, message)``. ``ok`` is False on drift or a missing target.
    """
    compiled = compile(cases_toml)
    target = (
        pathlib.Path(jsonl_path)
        if jsonl_path is not None
        else pathlib.Path(cases_toml).with_suffix(".jsonl")
    )
    if not target.is_file():
        return False, f"no compiled file at {target}; run `cases compile` first"
    on_disk = target.read_text(encoding="utf-8")
    if on_disk == compiled:
        return True, f"{target} is up to date"
    return False, f"{target} is stale; recompile with `cases compile`"


# --- reader used by matrix/bench/replay to load compiled cases -------------
_ALIASES = {"id": "case_id", "input": "input_text", "prompt": "input_text", "expected": "expected_outcome"}


def load_cases(jsonl_path: str | pathlib.Path) -> list[ci.CaseSpec]:
    """Read a compiled ``cases.jsonl`` into ``CaseSpec`` records.

    Tolerates a missing header row (schema 1.x, ``turns`` absent) and the §11.2
    short aliases (``id``/``input``/``prompt``/``expected``).
    """
    out: list[ci.CaseSpec] = []
    for raw in pathlib.Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if "schema_version" in obj and "case_id" not in obj and "id" not in obj:
            continue  # header row
        for alias, canonical in _ALIASES.items():
            if alias in obj and canonical not in obj:
                obj[canonical] = obj[alias]
        out.append(
            ci.CaseSpec(
                case_id=str(obj["case_id"]),
                input_text=str(obj.get("input_text", "")),
                turns=tuple(obj["turns"]) if obj.get("turns") else None,
                fixture_id=obj.get("fixture_id"),
                role=str(obj.get("role", "treatment")),
                expected_outcome=str(obj.get("expected_outcome", "pass")),
                expected_paths=tuple(obj.get("expected_paths", ())),
                expected_skill=obj.get("expected_skill"),
                assertions=tuple(obj.get("assertions", ())),
                case_weight=float(obj.get("case_weight", 1.0)),
                quarantined=bool(obj.get("quarantined", False)),
                metadata=dict(obj.get("metadata", {})),
            )
        )
    return out
