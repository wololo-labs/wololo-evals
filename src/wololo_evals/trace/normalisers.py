"""Per-engine trace normalisers (README §14.3).

Each engine speaks its own raw event dialect — Claude Code's ``stream-json``
lines, Codex's ``exec`` event messages, Copilot's raw jsonl — and this module
maps every one of them onto the single normalised ``TraceEvent`` schema from
``core.interfaces``. That schema is what the trace-assertion engine
(``trace.assertions``) and the efficiency signals (``trace.efficiency``) read,
so downstream code never sees an engine-native shape.

Two invariants the whole wave depends on:

* **Fail loudly.** A parser that silently returns ``[]`` on an unrecognised
  stream would turn a broken adapter into a green ``trace:`` conformance
  score. Every normaliser raises ``TraceParseError`` on malformed JSON, an
  unknown top-level event discriminator, or a structurally invalid line —
  never an empty trace (README §6.1, §14.3).
* **One activation shape.** Every engine's native "a skill/asset activated"
  marker maps onto ``kind="lifecycle", name="skill_activated",
  target=<asset id>`` so ``trace:used_skill`` is engine-agnostic.

Native tool names map onto the fixed verb set (``read``/``edit``/``write``/
``bash``/``fetch``/``search``); an unrecognised *tool name* is not fatal — it
is carried through as an adapter-registered extra verb (README §14.3) so
``allow_tools``/``deny_tools`` can still reason about it. Only an unrecognised
*stream structure* is fatal.

Imports: ``core.interfaces`` + stdlib only. No database, no vendor SDK.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import pathlib
from typing import Mapping, Optional

from wololo_evals.core.interfaces import (
    SKILL_ACTIVATED,
    TraceEvent,
)

__all__ = [
    "TraceParseError",
    "ClaudeNormaliser",
    "CodexNormaliser",
    "CopilotNormaliser",
    "ReplayNormaliser",
    "NORMALISERS",
    "get_normaliser",
    "normalise_stream",
]


_LOG = logging.getLogger(__name__)


class TraceParseError(ValueError):
    """Raised when a raw engine stream cannot be normalised.

    A dedicated type so callers (the harness retry policy, tests) can tell a
    stream-format failure apart from a generic ``ValueError`` and mark the run
    ``infra_fail`` per README §14.2 rather than scoring a silent zero.
    """


def _digest(value: object) -> Optional[str]:
    """Stable short content hash for full tool args / results (README §14.3:
    full arguments remain digested). ``None`` in → ``None`` out."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_jsonl(raw_event_path: pathlib.Path) -> list[tuple[int, dict]]:
    """Load a raw ``*.jsonl`` stream into ``(line_number, object)`` pairs.

    Blank lines are skipped; a non-object row or a JSON syntax error is fatal
    (``TraceParseError``) — the stream is malformed, not empty.
    """
    path = pathlib.Path(raw_event_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        raise TraceParseError(f"cannot read raw event stream {path}: {exc}") from exc
    rows: list[tuple[int, dict]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceParseError(
                f"{path}:{lineno}: malformed JSON in raw event stream: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise TraceParseError(
                f"{path}:{lineno}: raw event line is {type(obj).__name__}, expected object"
            )
        rows.append((lineno, obj))
    if not rows:
        raise TraceParseError(f"{path}: raw event stream is empty")
    return rows


class _BaseNormaliser:
    """Shared sequencing/token bookkeeping for the concrete normalisers.

    Subclasses only implement ``_emit`` (yield ``(kind, fields)`` per raw row);
    the base assigns monotonic ``seq``, synthesises a millisecond clock when the
    native stream carries none, and attributes engine-provided token totals onto
    the most-recently emitted event so ``efficiency`` can sum tokens off the
    trace regardless of where the engine reports them.
    """

    engine_id: str = ""
    #: recognised top-level discriminators.
    _known: frozenset[str] = frozenset()

    def __init__(self, *, strict: bool = False) -> None:
        #: When True an unrecognised discriminator fails the parse. The default
        #: skips it, because a CLI adds informational lines over time and
        #: throwing away a run that has already been paid for over one is a
        #: poor trade. It is not silent: each new type is logged and every one
        #: is counted in :attr:`skipped`, which the matrix records.
        self.strict = strict
        self.skipped: collections.Counter[str] = collections.Counter()

    def _unknown(self, lineno: int, field: str, value) -> None:
        """Decide what an unrecognised discriminator means. Called from ``_emit``."""
        if self.strict:
            raise TraceParseError(
                f"{self.engine_id}:{lineno}: unknown {field} {value!r} "
                f"(known: {sorted(self._known)})"
            )
        if self.skipped[str(value)] == 0:
            _LOG.warning(
                "%s:%s: skipping unknown %s %r (known: %s)",
                self.engine_id, lineno, field, value, sorted(self._known),
            )
        self.skipped[str(value)] += 1

    def normalise(self, raw_event_path: pathlib.Path) -> list[TraceEvent]:
        rows = _read_jsonl(raw_event_path)
        events: list[TraceEvent] = []
        for seq, (lineno, obj) in enumerate(self._iter_emitted(rows)):
            kind, fields = obj
            events.append(
                TraceEvent(
                    seq=seq,
                    ts_ms=fields.pop("ts_ms", seq),
                    kind=kind,
                    raw_ref=fields.pop("raw_ref", f"{self.engine_id}:{lineno}"),
                    **fields,
                )
            )
        if not events:
            raise TraceParseError(
                f"{raw_event_path}: {self.engine_id} stream yielded no trace events"
            )
        return events

    # -- token attribution ------------------------------------------------
    def _iter_emitted(self, rows):
        """Yield ``(lineno, (kind, fields))`` and fold token totals in.

        Engines that report a single cumulative token total (Codex
        ``token_count``, Copilot ``usage``) attach it to the last real event;
        engines with per-turn usage (Claude) attach inline. Either way the
        per-event ``tokens_in``/``tokens_out`` sum to the true totals.
        """
        pending: list[tuple[int, tuple[str, dict]]] = []
        for lineno, obj in rows:
            for item in self._emit(lineno, obj):
                if item is None:
                    continue
                if item[0] == "__tokens__":
                    tin, tout = item[1]["tokens_in"], item[1]["tokens_out"]
                    if pending:
                        pending[-1][1][1]["tokens_in"] = (
                            pending[-1][1][1].get("tokens_in", 0) + tin
                        )
                        pending[-1][1][1]["tokens_out"] = (
                            pending[-1][1][1].get("tokens_out", 0) + tout
                        )
                    continue
                pending.append((lineno, item))
        return pending

    def _emit(self, lineno: int, obj: dict):  # pragma: no cover - abstract
        raise NotImplementedError


class ClaudeNormaliser(_BaseNormaliser):
    """Claude Code ``--output-format stream-json`` lines → ``TraceEvent[]``.

    Each line is one of ``system`` (session init), ``assistant`` / ``user``
    (message turns whose ``content`` blocks carry ``text``, ``tool_use``, and
    ``tool_result``), or ``result`` (terminal totals). ``tool_use`` with name
    ``Skill`` is the native skill-activation marker.
    """

    engine_id = "claude"
    #: ``rate_limit_event`` is the CLI telling the operator about throttling.
    #: It carries no trajectory, but it is a real line a live stream emits, and
    #: refusing it threw away a whole run that had already been paid for.
    _known = frozenset({"system", "assistant", "user", "result", "rate_limit_event"})
    #: Known types that describe the session rather than the run.
    _ignored = frozenset({"system", "rate_limit_event"})

    #: native Claude Code tool names → normalised verbs.
    _VERBS = {
        "Read": "read",
        "Edit": "edit",
        "MultiEdit": "edit",
        "Write": "write",
        "Bash": "bash",
        "WebFetch": "fetch",
        "Fetch": "fetch",
        "Grep": "search",
        "Glob": "search",
    }

    def _emit(self, lineno, obj):
        etype = obj.get("type")
        if etype not in self._known:
            self._unknown(lineno, "event type", etype)
            return
        if etype in self._ignored:
            return  # session chatter: no trajectory event to record
        if etype == "result":
            usage = obj.get("usage") or {}
            yield ("__tokens__", {
                "tokens_in": int(usage.get("input_tokens", 0)),
                "tokens_out": int(usage.get("output_tokens", 0)),
            })
            return
        message = obj.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            raise TraceParseError(
                f"{self.engine_id}:{lineno}: {etype} message.content is not a list"
            )
        usage = message.get("usage") or {}
        tin = int(usage.get("input_tokens", 0))
        tout = int(usage.get("output_tokens", 0))
        emitted_any = False
        for block in content:
            if not isinstance(block, dict):
                raise TraceParseError(
                    f"{self.engine_id}:{lineno}: content block is not an object"
                )
            btype = block.get("type")
            if btype == "text":
                yield ("message", {"target": None, "tokens_in": tin, "tokens_out": tout})
                emitted_any = True
                tin = tout = 0
            elif btype == "thinking":
                yield ("thinking", {"tokens_in": tin, "tokens_out": tout})
                emitted_any = True
                tin = tout = 0
            elif btype == "tool_use":
                name = block.get("name", "")
                tinput = block.get("input") or {}
                if name == "Skill":
                    skill = (
                        tinput.get("skill")
                        or tinput.get("command")
                        or tinput.get("name")
                        or ""
                    )
                    yield ("lifecycle", {
                        "name": SKILL_ACTIVATED,
                        "target": str(skill),
                        "args_digest": _digest(tinput),
                        "tokens_in": tin,
                        "tokens_out": tout,
                    })
                else:
                    yield ("tool_call", {
                        "name": self._VERBS.get(name, name),
                        "target": _claude_target(name, tinput),
                        "args_digest": _digest(tinput),
                        "tokens_in": tin,
                        "tokens_out": tout,
                    })
                emitted_any = True
                tin = tout = 0
            elif btype == "tool_result":
                yield ("tool_result", {
                    "target": None,
                    "result_digest": _digest(block.get("content")),
                })
                emitted_any = True
            else:
                raise TraceParseError(
                    f"{self.engine_id}:{lineno}: unknown content block type {btype!r}"
                )
        if not emitted_any and tin + tout:
            # message with usage but no renderable blocks: keep the tokens.
            yield ("message", {"target": None, "tokens_in": tin, "tokens_out": tout})


def _claude_target(name: str, tinput: Mapping[str, object]) -> Optional[str]:
    if name in ("Read", "Edit", "MultiEdit", "Write"):
        fp = tinput.get("file_path")
        return str(fp) if fp is not None else None
    if name == "Bash":
        cmd = tinput.get("command")
        return str(cmd) if cmd is not None else None
    if name in ("WebFetch", "Fetch"):
        url = tinput.get("url")
        return str(url) if url is not None else None
    if name in ("Grep", "Glob"):
        pat = tinput.get("pattern") or tinput.get("path")
        return str(pat) if pat is not None else None
    return None


class CodexNormaliser(_BaseNormaliser):
    """Codex ``exec --json`` event messages → ``TraceEvent[]``.

    Each line is ``{"id": ..., "msg": {"type": ...}}``. Codex acts mostly
    through shell exec (``exec_command_begin``/``_end``) and patch application
    (``patch_apply_begin``/``_end``); ``skill_activated`` is the runtime's
    native activation marker and ``token_count`` the cumulative token report.
    """

    engine_id = "codex"
    _known = frozenset({
        "task_started",
        "task_complete",
        "skill_activated",
        "agent_message",
        "agent_reasoning",
        "exec_command_begin",
        "exec_command_end",
        "patch_apply_begin",
        "patch_apply_end",
        "token_count",
        "error",
    })

    def _emit(self, lineno, obj):
        msg = obj.get("msg")
        if not isinstance(msg, dict):
            raise TraceParseError(
                f"{self.engine_id}:{lineno}: line has no object 'msg' field"
            )
        mtype = msg.get("type")
        if mtype not in self._known:
            self._unknown(lineno, "msg.type", mtype)
            return
        if mtype in ("task_started", "task_complete"):
            return
        if mtype == "skill_activated":
            yield ("lifecycle", {
                "name": SKILL_ACTIVATED,
                "target": str(msg.get("skill", "")),
                "args_digest": _digest(msg),
            })
        elif mtype == "agent_message":
            yield ("message", {"target": None})
        elif mtype == "agent_reasoning":
            yield ("thinking", {})
        elif mtype == "exec_command_begin":
            yield ("tool_call", {
                "name": "bash",
                "target": _codex_command(msg.get("command")),
                "args_digest": _digest(msg.get("command")),
            })
        elif mtype == "exec_command_end":
            yield ("tool_result", {
                "target": None,
                "result_digest": _digest(msg.get("stdout")),
            })
        elif mtype == "patch_apply_begin":
            changes = msg.get("changes")
            if not isinstance(changes, dict):
                raise TraceParseError(
                    f"{self.engine_id}:{lineno}: patch_apply_begin.changes not an object"
                )
            for path, change in changes.items():
                yield ("tool_call", {
                    "name": _codex_patch_verb(change),
                    "target": str(path),
                    "args_digest": _digest(change),
                })
        elif mtype == "patch_apply_end":
            yield ("tool_result", {
                "target": None,
                "result_digest": _digest(msg.get("success")),
            })
        elif mtype == "token_count":
            yield ("__tokens__", {
                "tokens_in": int(msg.get("input_tokens", 0)),
                "tokens_out": int(msg.get("output_tokens", 0)),
            })
        elif mtype == "error":
            yield ("error", {"target": str(msg.get("message", "")) or None})


def _codex_command(command: object) -> Optional[str]:
    """Reduce Codex's argv (``["bash","-lc","cat x"]``) to the command string
    trace patterns match against."""
    if command is None:
        return None
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        parts = [str(p) for p in command]
        if len(parts) >= 3 and parts[0] in ("bash", "sh") and parts[1] in ("-lc", "-c"):
            return parts[2]
        return " ".join(parts)
    return str(command)


def _codex_patch_verb(change: object) -> str:
    if isinstance(change, dict):
        if "add" in change:
            return "write"
        if "delete" in change:
            return "edit"
    return "edit"


class CopilotNormaliser(_BaseNormaliser):
    """GitHub Copilot CLI raw jsonl → ``TraceEvent[]``.

    Copilot's agent mode streams flat rows discriminated by ``type``:
    ``assistant_message``, ``tool_call`` / ``tool_result`` (OpenAI-style
    function calls), ``usage`` totals, and a terminal ``done``. An
    ``activate_skill`` tool call is the native activation marker.
    """

    engine_id = "copilot"
    _known = frozenset({
        "assistant_message",
        "reasoning",
        "tool_call",
        "tool_result",
        "usage",
        "error",
        "done",
    })

    #: native Copilot tool names → normalised verbs.
    _VERBS = {
        "view": "read",
        "read_file": "read",
        "str_replace": "edit",
        "edit_file": "edit",
        "create": "write",
        "create_file": "write",
        "run": "bash",
        "shell": "bash",
        "bash": "bash",
        "fetch": "fetch",
        "web_fetch": "fetch",
        "search": "search",
        "grep": "search",
    }

    def _emit(self, lineno, obj):
        etype = obj.get("type")
        if etype not in self._known:
            self._unknown(lineno, "event type", etype)
            return
        if etype == "done":
            return
        if etype == "assistant_message":
            yield ("message", {"target": None})
        elif etype == "reasoning":
            yield ("thinking", {})
        elif etype == "tool_call":
            name = obj.get("name", "")
            args = obj.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError as exc:
                    raise TraceParseError(
                        f"{self.engine_id}:{lineno}: tool_call.arguments is not JSON: {exc}"
                    ) from exc
            args = args if isinstance(args, dict) else {}
            if name in ("activate_skill", "skill"):
                yield ("lifecycle", {
                    "name": SKILL_ACTIVATED,
                    "target": str(args.get("skill") or args.get("id") or ""),
                    "args_digest": _digest(args),
                })
            else:
                yield ("tool_call", {
                    "name": self._VERBS.get(name, name),
                    "target": _copilot_target(name, args),
                    "args_digest": _digest(args),
                })
        elif etype == "tool_result":
            yield ("tool_result", {
                "target": None,
                "result_digest": _digest(obj.get("output")),
            })
        elif etype == "usage":
            yield ("__tokens__", {
                "tokens_in": int(obj.get("input_tokens", 0)),
                "tokens_out": int(obj.get("output_tokens", 0)),
            })
        elif etype == "error":
            yield ("error", {"target": str(obj.get("message", "")) or None})


def _copilot_target(name: str, args: Mapping[str, object]) -> Optional[str]:
    for key in ("path", "file", "file_path", "url", "command", "pattern", "query"):
        val = args.get(key)
        if val is not None:
            return str(val)
    return None


class ReplayNormaliser:
    """Normalise an archived stream with the *recorded* engine's normaliser.

    ``replay`` is not a dialect, it is a re-emission: ``ReplayAdapter`` writes
    the archived log back out under its original name
    (``<engine>.raw.jsonl``), so the engine is read off the filename and the
    real normaliser does the work. Without this a replayed matrix would
    normalise nothing and every ``trace:`` assertion would score zero against a
    perfectly good archive.
    """

    engine_id = "replay"

    def normalise(self, raw_event_path: pathlib.Path) -> list[TraceEvent]:
        path = pathlib.Path(raw_event_path)
        recorded = path.name.split(".")[0]
        if recorded == self.engine_id or recorded not in NORMALISERS:
            raise TraceParseError(
                f"{path}: cannot infer the recorded engine from the archived "
                f"filename (got {recorded!r}; have: {sorted(NORMALISERS)})"
            )
        return NORMALISERS[recorded].normalise(path)


#: engine id → normaliser instance (README §6.1). ``replay`` delegates to the
#: recorded engine's normaliser, so an offline matrix normalises exactly like
#: the live run it replays.
NORMALISERS: Mapping[str, object] = {
    ClaudeNormaliser.engine_id: ClaudeNormaliser(),
    CodexNormaliser.engine_id: CodexNormaliser(),
    CopilotNormaliser.engine_id: CopilotNormaliser(),
    ReplayNormaliser.engine_id: ReplayNormaliser(),
}


def get_normaliser(engine_id: str) -> object:
    """Return the normaliser for ``engine_id`` or fail loudly (README §6.1)."""
    try:
        return NORMALISERS[engine_id]
    except KeyError:
        raise TraceParseError(
            f"no trace normaliser for engine {engine_id!r} "
            f"(have: {sorted(NORMALISERS)})"
        ) from None


def normalise_stream(engine_id: str, raw_event_path: pathlib.Path) -> list[TraceEvent]:
    """Convenience: resolve the normaliser and normalise one raw stream."""
    return get_normaliser(engine_id).normalise(raw_event_path)
