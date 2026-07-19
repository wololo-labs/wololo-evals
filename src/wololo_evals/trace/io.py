"""``trace.jsonl`` writer/reader (README §14.3, §15.2 canonical serialisation).

Every live run writes a ``trace.jsonl``: a header row followed by one
normalised ``TraceEvent`` per line. Serialisation matches the repo-wide
canonical form (``ensure_ascii=False``, tight separators, trailing newline,
fixed field order) so a re-serialised trace is byte-stable — the property the
``replay`` engine's regression tests depend on.

The header row (``{"schema_version": "...", "kind": "trace", ...}``) mirrors
``cases.jsonl`` (README §11.2): the reader tolerates a file with no header
(treated as schema 1.x) so older archives still load.

Imports: ``core.interfaces`` + stdlib only.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Iterable, Optional

from wololo_evals.core.interfaces import TraceEvent

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "TRACE_FIELD_ORDER",
    "write_trace",
    "read_trace",
    "dumps_event",
]

#: current trace schema, carried in the header row.
TRACE_SCHEMA_VERSION = "1.0.0"

#: canonical field order for a serialised ``TraceEvent`` line.
TRACE_FIELD_ORDER = tuple(f.name for f in dataclasses.fields(TraceEvent))


def _canonical(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def dumps_event(event: TraceEvent) -> str:
    """Serialise one ``TraceEvent`` to its canonical single-line form."""
    d = dataclasses.asdict(event)
    return _canonical({k: d[k] for k in TRACE_FIELD_ORDER})


def write_trace(
    path: pathlib.Path,
    events: Iterable[TraceEvent],
    *,
    engine_id: Optional[str] = None,
) -> pathlib.Path:
    """Write ``events`` to ``path`` as a header row + one event per line.

    Parent directories are created. Returns ``path`` for chaining.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header: dict[str, object] = {"schema_version": TRACE_SCHEMA_VERSION, "kind": "trace"}
    if engine_id is not None:
        header["engine_id"] = engine_id
    lines = [_canonical(header)]
    lines.extend(dumps_event(e) for e in events)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_trace(path: pathlib.Path) -> list[TraceEvent]:
    """Read a ``trace.jsonl`` back into ``TraceEvent[]``.

    The header row (an object with ``schema_version`` and no ``seq``) is
    skipped; a file without one is read as schema 1.x. Each event row is
    re-validated through ``TraceEvent.__post_init__`` (fail-loud), and only the
    known fields are passed so extra columns from a newer schema are ignored.
    """
    path = pathlib.Path(path)
    events: list[TraceEvent] = []
    field_set = set(TRACE_FIELD_ORDER)
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: malformed trace.jsonl line: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{lineno}: trace.jsonl line is not an object")
        if "schema_version" in obj and "seq" not in obj:
            continue  # header row
        events.append(TraceEvent(**{k: v for k, v in obj.items() if k in field_set}))
    return events
