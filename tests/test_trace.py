"""Trajectory pillar tests (README §14.3): per-engine normalisers, the
``trace:`` assertion engine, standing efficiency signals, and ``trace.jsonl``
io.

These are pure, no-DB tests — the trace package imports only
``core.interfaces`` + stdlib, so there is no Postgres fixture in sight. Each
group proves one spec invariant:

* normalisers map every engine's native stream onto the *same* ``TraceEvent``
  schema and **fail loudly** on an unrecognised format (never a silent empty
  trace);
* the built-in ``trace:`` checks implement the §14.3 pattern grammar
  (``verb`` / ``verb:target-glob``) with the documented pass/fail semantics;
* efficiency signals count calls/waste/tokens off the normalised trace;
* ``write_trace``/``read_trace`` round-trip byte-stably with a header row.

Recorded raw streams live in ``tests/fixtures/trace/`` and are hand-authored
from each engine's documented output shape.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from wololo_evals.core.interfaces import TraceEvent
from wololo_evals.trace import (
    ClaudeNormaliser,
    CodexNormaliser,
    CopilotNormaliser,
    TraceAssertionError,
    TraceParseError,
    dumps_event,
    efficiency_signals,
    evaluate_trace_assertion,
    get_normaliser,
    is_trace_assertion,
    normalise_stream,
    parse_trace_assertion,
    read_trace,
    register_check,
    token_totals,
    total_tool_calls,
    wall_clock_ms,
    wasted_calls,
    write_trace,
)
from wololo_evals.trace.assertions import TraceCheckResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "trace"


def _tc(name, target, *, seq=0):
    """A minimal ``tool_call`` event for hand-built assertion/efficiency cases."""
    return TraceEvent(seq=seq, ts_ms=seq, kind="tool_call", name=name, target=target)


def _activation(skill, *, seq=0):
    return TraceEvent(
        seq=seq, ts_ms=seq, kind="lifecycle", name="skill_activated", target=skill
    )


# ---------------------------------------------------------------------------
# Normalisers — happy path over recorded fixtures
# ---------------------------------------------------------------------------
def test_claude_normaliser_maps_native_stream():
    events = normalise_stream("claude", FIXTURES / "claude.raw.jsonl")
    # seq is monotonic from 0; the system init line emits nothing.
    assert [e.seq for e in events] == list(range(len(events)))
    kinds = [e.kind for e in events]
    assert kinds == [
        "message",     # assistant text
        "lifecycle",   # Skill activation
        "tool_call",   # Read
        "tool_result",
        "tool_call",   # Edit
        "tool_result",
    ]
    # native tool names normalised onto the verb set, target = file path.
    calls = [e for e in events if e.kind == "tool_call"]
    assert [(e.name, e.target) for e in calls] == [
        ("read", "src/select.js"),
        ("edit", "src/select.js"),
    ]
    # skill activation surfaced as the engine-agnostic lifecycle marker.
    act = next(e for e in events if e.is_skill_activation)
    assert act.name == "skill_activated" and act.target == "dropdowns"
    # per-turn usage sums to the run totals.
    assert token_totals(events) == (5000, 130)
    # raw_ref points back at the source line.
    assert all(e.raw_ref and e.raw_ref.startswith("claude:") for e in events)


def test_codex_normaliser_maps_exec_and_patch():
    events = normalise_stream("codex", FIXTURES / "codex.raw.jsonl")
    kinds = [e.kind for e in events]
    assert kinds == [
        "lifecycle",    # skill_activated
        "message",      # agent_message
        "tool_call",    # exec_command_begin -> bash
        "tool_result",  # exec_command_end
        "tool_call",    # patch_apply_begin -> edit
        "tool_result",  # patch_apply_end
    ]
    calls = [e for e in events if e.kind == "tool_call"]
    assert [(e.name, e.target) for e in calls] == [
        ("bash", "cat src/select.js"),
        ("edit", "src/select.js"),
    ]
    act = next(e for e in events if e.is_skill_activation)
    assert act.target == "dropdowns"
    # single cumulative token_count attributed onto the trace.
    assert token_totals(events) == (4200, 110)


def test_copilot_normaliser_maps_flat_jsonl():
    events = normalise_stream("copilot", FIXTURES / "copilot.raw.jsonl")
    kinds = [e.kind for e in events]
    assert kinds == [
        "message",
        "lifecycle",
        "tool_call",    # view -> read
        "tool_result",
        "tool_call",    # str_replace -> edit
        "tool_result",
    ]
    calls = [e for e in events if e.kind == "tool_call"]
    assert [(e.name, e.target) for e in calls] == [
        ("read", "src/select.js"),
        ("edit", "src/select.js"),
    ]
    assert next(e for e in events if e.is_skill_activation).target == "dropdowns"
    assert token_totals(events) == (3800, 95)


def test_registry_covers_live_engines_and_rejects_unknown():
    assert set(get_normaliser(e).engine_id for e in ("claude", "codex", "copilot")) == {
        "claude", "codex", "copilot"
    }
    assert isinstance(get_normaliser("claude"), ClaudeNormaliser)
    assert isinstance(get_normaliser("codex"), CodexNormaliser)
    assert isinstance(get_normaliser("copilot"), CopilotNormaliser)
    with pytest.raises(TraceParseError):
        get_normaliser("gemini")


# ---------------------------------------------------------------------------
# Normalisers — fail loudly (never a silent empty trace)
# ---------------------------------------------------------------------------
def test_normaliser_rejects_malformed_json(tmp_path):
    bad = tmp_path / "claude.raw.jsonl"
    bad.write_text('{"type":"system"}\n{not json}\n', encoding="utf-8")
    with pytest.raises(TraceParseError, match="malformed JSON"):
        normalise_stream("claude", bad)


def test_a_stream_of_nothing_but_unknown_events_is_still_an_error(tmp_path):
    # Skipping unknown types must not turn an unreadable stream into an empty
    # trace that scores as a run which did nothing.
    bad = tmp_path / "claude.raw.jsonl"
    bad.write_text('{"type":"telepathy","payload":1}\n', encoding="utf-8")
    with pytest.raises(TraceParseError, match="yielded no trace events"):
        normalise_stream("claude", bad)


def test_an_unknown_event_type_is_skipped_and_counted_not_fatal(tmp_path, caplog):
    # A live stream emitted an informational line the normaliser did not know,
    # and the whole matrix aborted on a run that had already been paid for.
    # Unknown types are skipped with a warning and counted, so a trace can
    # still be rejected by anyone who reads the count, but a new informational
    # event no longer throws a run away.
    import logging

    from wololo_evals.trace.normalisers import ClaudeNormaliser

    raw = tmp_path / "claude.raw.jsonl"
    raw.write_text(
        '{"type":"system","subtype":"init"}\n'
        '{"type":"tool_progress","pct":40}\n'
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}\n'
        '{"type":"tool_progress","pct":90}\n',
        encoding="utf-8",
    )
    norm = ClaudeNormaliser()
    with caplog.at_level(logging.WARNING):
        events = norm.normalise(raw)
    assert [(e.kind, e.name) for e in events] == [("tool_call", "read")]
    assert dict(norm.skipped) == {"tool_progress": 2}
    assert "tool_progress" in caplog.text

    # strict mode keeps the old behaviour for anyone who wants a parse to fail
    strict = ClaudeNormaliser(strict=True)
    with pytest.raises(TraceParseError, match="unknown event type"):
        strict.normalise(raw)


def test_claude_session_chatter_is_ignored_not_rejected(tmp_path):
    # A throttling notice is a real line a live stream emits. It carries no
    # trajectory, and refusing it discards a run that has already been paid for.
    raw = tmp_path / "claude.raw.jsonl"
    raw.write_text(
        '{"type":"system","subtype":"init"}\n'
        '{"type":"rate_limit_event","status":"throttled"}\n'
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}\n',
        encoding="utf-8",
    )
    events = normalise_stream("claude", raw)
    assert [(e.kind, e.name) for e in events] == [("tool_call", "read")]


def test_codex_rejects_line_without_msg(tmp_path):
    bad = tmp_path / "codex.raw.jsonl"
    bad.write_text('{"id":"0"}\n', encoding="utf-8")
    with pytest.raises(TraceParseError, match="no object 'msg'"):
        normalise_stream("codex", bad)


def test_normaliser_rejects_empty_stream(tmp_path):
    empty = tmp_path / "copilot.raw.jsonl"
    empty.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(TraceParseError, match="empty"):
        normalise_stream("copilot", empty)


def test_copilot_rejects_non_json_tool_arguments(tmp_path):
    bad = tmp_path / "copilot.raw.jsonl"
    bad.write_text('{"type":"tool_call","name":"view","arguments":"{oops"}\n', encoding="utf-8")
    with pytest.raises(TraceParseError, match="not JSON"):
        normalise_stream("copilot", bad)


# ---------------------------------------------------------------------------
# Assertion engine — parsing + grammar
# ---------------------------------------------------------------------------
def test_is_trace_assertion_and_parse():
    assert is_trace_assertion("trace:allow_tools(read, edit)")
    assert not is_trace_assertion("the answer explains the risk")
    check, args = parse_trace_assertion("trace:require_order(read:package.json, edit:package.json)")
    assert check == "require_order"
    assert args == ["read:package.json", "edit:package.json"]
    assert parse_trace_assertion("trace:no_repeat_loops(3)") == ("no_repeat_loops", ["3"])


def test_parse_rejects_malformed_and_unknown():
    with pytest.raises(TraceAssertionError, match="malformed"):
        parse_trace_assertion("trace:allow_tools[read]")
    with pytest.raises(TraceAssertionError, match="unknown trace check"):
        evaluate_trace_assertion("trace:teleport(x)", [])


CLAUDE_EVENTS = None  # populated lazily per-test to avoid module-load I/O


def _claude_events():
    return normalise_stream("claude", FIXTURES / "claude.raw.jsonl")


def test_allow_tools_pass_and_fail():
    events = _claude_events()
    assert evaluate_trace_assertion("trace:allow_tools(read, edit)", events).passed
    res = evaluate_trace_assertion("trace:allow_tools(read)", events)
    assert not res.passed
    assert "edit" in res.evidence


def test_deny_tools_pass_and_fail():
    events = _claude_events()
    assert evaluate_trace_assertion("trace:deny_tools(bash, fetch)", events).passed
    assert not evaluate_trace_assertion("trace:deny_tools(edit)", events).passed


def test_require_order_semantics():
    events = _claude_events()  # read src/select.js then edit src/select.js
    assert evaluate_trace_assertion(
        "trace:require_order(read:src/select.js, edit:src/select.js)", events
    ).passed
    # reversed requirement fails: no edit precedes the read.
    res = evaluate_trace_assertion(
        "trace:require_order(edit:src/select.js, read:src/select.js)", events
    )
    assert not res.passed
    assert res.details["violations"] == 1
    # vacuously true when the 'after' pattern never occurs.
    assert evaluate_trace_assertion(
        "trace:require_order(read:src/select.js, bash:*)", events
    ).passed


def test_require_order_glob_matches_target():
    events = [_tc("read", "package.json", seq=0), _tc("edit", "package.json", seq=1)]
    assert evaluate_trace_assertion(
        "trace:require_order(read:pack*.json, edit:package.json)", events
    ).passed
    # case-sensitive glob: uppercase does not match.
    res = evaluate_trace_assertion(
        "trace:require_order(read:PACK*, edit:package.json)", events
    )
    assert not res.passed


def test_max_calls():
    events = _claude_events()
    assert evaluate_trace_assertion("trace:max_calls(read, 1)", events).passed
    assert not evaluate_trace_assertion("trace:max_calls(read, 0)", events).passed
    with pytest.raises(TraceAssertionError):
        evaluate_trace_assertion("trace:max_calls(read, notanint)", events)


def test_no_repeat_loops():
    ok = [_tc("bash", "ls", seq=0), _tc("bash", "pwd", seq=1)]
    assert evaluate_trace_assertion("trace:no_repeat_loops(2)", ok).passed
    looped = [_tc("bash", "ls", seq=i) for i in range(3)]
    res = evaluate_trace_assertion("trace:no_repeat_loops(3)", looped)
    assert not res.passed
    assert res.details["longest_run"] == 3
    # non-consecutive repeats do not trip the loop check.
    spaced = [_tc("bash", "ls", seq=0), _tc("read", "a", seq=1), _tc("bash", "ls", seq=2)]
    assert evaluate_trace_assertion("trace:no_repeat_loops(2)", spaced).passed
    with pytest.raises(TraceAssertionError):
        evaluate_trace_assertion("trace:no_repeat_loops(1)", ok)


def test_used_skill_explicit_and_default():
    events = _claude_events()
    assert evaluate_trace_assertion("trace:used_skill(dropdowns)", events).passed
    assert not evaluate_trace_assertion("trace:used_skill(tabs)", events).passed
    # empty arg falls back to the case's expected_skill (README §14.3).
    assert evaluate_trace_assertion(
        "trace:used_skill()", events, default_skill="dropdowns"
    ).passed
    with pytest.raises(TraceAssertionError, match="needs a skill id"):
        evaluate_trace_assertion("trace:used_skill()", events)


def test_result_adapts_to_scored_signal():
    events = _claude_events()
    res = evaluate_trace_assertion("trace:used_skill(dropdowns)", events)
    signal = res.to_signal(evaluator_id="reads_before_edit", weight=2.0)
    assert signal.kind == "assertion"
    assert signal.slot == "trace_conformance"
    assert signal.verdict == "pass" and signal.score == 1.0 and signal.weight == 2.0


def test_register_custom_check():
    def _always_pass(args, events, default_skill):
        return TraceCheckResult("thinks", True, "ok", {})

    register_check("thinks", _always_pass)
    assert evaluate_trace_assertion("trace:thinks()", []).passed


# ---------------------------------------------------------------------------
# Efficiency signals
# ---------------------------------------------------------------------------
def test_efficiency_counts_off_trace():
    events = _claude_events()
    assert total_tool_calls(events) == 2  # lifecycle activation excluded
    assert wasted_calls(events) == 0      # read target reused by the edit
    assert token_totals(events) == (5000, 130)


def test_wasted_calls_detects_unused_read():
    # read a file that is never edited/used -> wasted; read+edit pair -> not.
    events = [
        _tc("read", "unused.py", seq=0),
        _tc("read", "src/app.py", seq=1),
        _tc("edit", "src/app.py", seq=2),
    ]
    assert wasted_calls(events) == 1
    # a read whose target is substring-referenced by a later bash counts used.
    events2 = [_tc("read", "app.py", seq=0), _tc("bash", "python app.py", seq=1)]
    assert wasted_calls(events2) == 0


def test_wall_clock_span():
    events = [
        TraceEvent(seq=0, ts_ms=1000, kind="message"),
        TraceEvent(seq=1, ts_ms=1750, kind="message"),
    ]
    assert wall_clock_ms(events) == 750
    assert wall_clock_ms(events[:1]) == 0


def test_efficiency_signals_are_non_gating():
    signals = efficiency_signals(_claude_events())
    ids = {s.evaluator_id: s for s in signals}
    assert set(ids) == {
        "efficiency.total_tool_calls",
        "efficiency.wasted_calls",
        "efficiency.tokens_in",
        "efficiency.tokens_out",
        "efficiency.wall_clock_ms",
    }
    for s in signals:
        assert s.kind == "efficiency"
        assert s.weight == 0.0  # do not gate by default
        assert s.verdict == "pass"
        assert s.slot is None
    assert ids["efficiency.total_tool_calls"].details["value"] == 2
    assert ids["efficiency.tokens_in"].details["value"] == 5000


# ---------------------------------------------------------------------------
# trace.jsonl io
# ---------------------------------------------------------------------------
def test_write_read_round_trip(tmp_path):
    events = _claude_events()
    path = write_trace(tmp_path / "sub" / "trace.jsonl", events, engine_id="claude")
    assert path.exists()  # parents created
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header == {"schema_version": "1.0.0", "kind": "trace", "engine_id": "claude"}
    assert len(lines) == len(events) + 1  # header + one row per event
    assert read_trace(path) == events


def test_trace_write_is_byte_stable(tmp_path):
    events = normalise_stream("codex", FIXTURES / "codex.raw.jsonl")
    p1 = write_trace(tmp_path / "a.jsonl", events, engine_id="codex")
    reparsed = read_trace(p1)
    p2 = write_trace(tmp_path / "b.jsonl", reparsed, engine_id="codex")
    assert p1.read_bytes() == p2.read_bytes()  # replay-grade stability


def test_read_tolerates_missing_header(tmp_path):
    path = tmp_path / "legacy.jsonl"
    row = dumps_event(TraceEvent(seq=0, ts_ms=0, kind="tool_call", name="read", target="x"))
    path.write_text(row + "\n", encoding="utf-8")  # no header row
    events = read_trace(path)
    assert len(events) == 1 and events[0].name == "read"


def test_read_revalidates_events(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"schema_version":"1.0.0","kind":"trace"}\n'
        '{"seq":0,"ts_ms":0,"kind":"not_a_kind"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not in"):
        read_trace(path)


def test_skip_counts_do_not_carry_over_between_streams(tmp_path):
    # NORMALISERS holds one instance per engine, and the counter was never
    # cleared, so a later run's artefact reported skips from an earlier one.
    from wololo_evals.trace.normalisers import ClaudeNormaliser

    noisy = tmp_path / "noisy.raw.jsonl"
    noisy.write_text(
        '{"type":"tool_progress"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"x"}]}}\n',
        encoding="utf-8",
    )
    clean = tmp_path / "clean.raw.jsonl"
    clean.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"x"}]}}\n',
        encoding="utf-8",
    )
    norm = ClaudeNormaliser()
    norm.normalise(noisy)
    assert dict(norm.skipped) == {"tool_progress": 1}
    norm.normalise(clean)
    assert dict(norm.skipped) == {}


def _skill_stream(tmp_path, *, result):
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Skill","input":{"skill":"demo"}}]}}',
    ]
    if result is not None:
        lines.append(
            '{"type":"user","message":{"content":[{"type":"tool_result",'
            f'"tool_use_id":"t1","content":"x"{result}}}]}}}}'
        )
    path = tmp_path / "claude.raw.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_skill_call_that_errored_is_not_an_activation(tmp_path):
    # The activation was emitted from the tool_use alone, so a call the CLI
    # rejected ("Unknown skill") still satisfied used_skill.
    errored = normalise_stream("claude", _skill_stream(tmp_path, result=',"is_error":true'))
    assert not any(e.is_skill_activation for e in errored)
    ok = normalise_stream("claude", _skill_stream(tmp_path, result=""))
    assert [e.target for e in ok if e.is_skill_activation] == ["demo"]
    # a stream cut before the result arrives is not proof either way: keep it
    cut = normalise_stream("claude", _skill_stream(tmp_path, result=None))
    assert [e.target for e in cut if e.is_skill_activation] == ["demo"]


def test_reported_token_totals_are_totals_not_increments(tmp_path):
    # Claude's `result` line reports the run's total usage on top of the
    # per-turn usage already attached inline, and Codex and Copilot report
    # cumulative totals that can repeat. Adding each report double counted:
    # a recorded run summed to 8162 output tokens against a reported 8119.
    raw = tmp_path / "claude.raw.jsonl"
    raw.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"a"}],'
        '"usage":{"input_tokens":10,"output_tokens":4}}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"b"}],'
        '"usage":{"input_tokens":6,"output_tokens":5}}}\n'
        '{"type":"result","usage":{"input_tokens":16,"output_tokens":100}}\n',
        encoding="utf-8",
    )
    events = normalise_stream("claude", raw)
    assert (sum(e.tokens_in for e in events), sum(e.tokens_out for e in events)) == (16, 100)

    cop = tmp_path / "copilot.raw.jsonl"
    cop.write_text(
        '{"type":"assistant_message"}\n'
        '{"type":"usage","input_tokens":100,"output_tokens":20}\n'
        '{"type":"assistant_message"}\n'
        '{"type":"usage","input_tokens":100,"output_tokens":20}\n',
        encoding="utf-8",
    )
    events = normalise_stream("copilot", cop)
    assert (sum(e.tokens_in for e in events), sum(e.tokens_out for e in events)) == (100, 20)
