"""Offline proof that the runner entrypoints capture each CLI's OWN event stream
into ``$WOLOLO_TRANSCRIPT_DIR/events.jsonl`` as fleet-hooks ``post-llm-event``
lines — the sole source ``harness_solver.count_turns`` reads (harness-seam.md §3).

Vanilla ``claude`` / ``codex`` do not themselves emit that stream, so without the
entrypoint transform a trial's turn count is 0 and the trace is unscoreable. Each
test here drives the transform over a CANNED fake event stream — NO CLI is
invoked against a model, nothing spends. Two surfaces are exercised:

* the ``--emit-events`` transform hook directly (stdin stream -> events.jsonl),
* the full wrapper path via a FAKE ``claude``/``codex`` script that prints a
  canned stream (proves passthrough stdout + exit-code preservation too).

Skip-guarded when ``node`` is absent (the transform is a Node one-liner; the
runner images are Node-based so node is always present there).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from wololo_evals.harness_solver import count_turns

_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parents[1]
_CLAUDE_ENTRY = _REPO / "images" / "claude-runner" / "entrypoint.sh"
_CODEX_ENTRY = _REPO / "images" / "codex-runner" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    _NODE is None, reason="node not on PATH; the entrypoint event transform needs it"
)

# --- canned fake event streams (no model, no CLI) ----------------------------

# Claude Code stream-json: one `assistant` event per model turn; `user` events
# are tool results, `result` is terminal. A non-JSON log line is included to
# prove the transform skips torn/plain lines without erroring.
_CLAUDE_STREAM = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps(
            {"type": "assistant", "message": {"role": "assistant",
                                              "content": [{"type": "text", "text": "hi"}]}}
        ),
        json.dumps(
            {"type": "user", "message": {"role": "user",
                                         "content": [{"type": "tool_result", "content": "ok"}]}}
        ),
        "a plain non-JSON log line the transform must skip",
        json.dumps(
            {"type": "assistant", "message": {"role": "assistant",
                                              "content": [{"type": "tool_use", "name": "bash"}]}}
        ),
        json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.01}),
    ]
) + "\n"
_CLAUDE_EXPECTED_TURNS = 2  # two `assistant` events

# Codex `exec --json` thread/item model: the model's assistant message lands as
# an item.completed with item_type `agent_message`; reasoning / command items are
# not turns.
_CODEX_STREAM = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i0", "item_type": "reasoning", "text": "think"}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i1", "item_type": "agent_message", "text": "first"}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i2", "item_type": "command_execution", "command": "ls"}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i3", "item_type": "agent_message", "text": "second"}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 20}}),
    ]
) + "\n"
_CODEX_EXPECTED_TURNS = 2  # two agent_message item.completed events


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _run_emit(entry: Path, stream: str, events_file: Path) -> subprocess.CompletedProcess[str]:
    """Drive only the transform hook: stdin stream -> passthrough stdout + events."""
    return subprocess.run(
        ["sh", str(entry), "--emit-events", str(events_file)],
        input=stream,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def _make_fake_cli(tmp_path: Path, name: str, stream: str, exit_code: int = 0) -> Path:
    """A fake CLI that prints a canned event stream then exits — no model call."""
    cli = tmp_path / name
    cli.write_text(
        "#!/bin/sh\n"
        "cat <<'STREAM_EOF'\n"
        f"{stream}"
        "STREAM_EOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


# --- transform hook (--emit-events) ------------------------------------------

@pytest.mark.parametrize(
    "entry, stream, expected",
    [
        pytest.param(_CLAUDE_ENTRY, _CLAUDE_STREAM, _CLAUDE_EXPECTED_TURNS, id="claude"),
        pytest.param(_CODEX_ENTRY, _CODEX_STREAM, _CODEX_EXPECTED_TURNS, id="codex"),
    ],
)
def test_emit_events_writes_one_post_llm_event_per_turn(
    tmp_path: Path, entry: Path, stream: str, expected: int
) -> None:
    events_file = tmp_path / "transcript" / "events.jsonl"
    proc = _run_emit(entry, stream, events_file)

    events = _read_events(events_file)
    # Every emitted line is a fleet-hooks post-llm-event...
    assert events, "no events.jsonl lines written"
    assert all(e.get("stage") == "post-llm-event" for e in events)
    # ...and count_turns (the ONLY turns source) counts exactly one per turn.
    assert count_turns(events) == expected
    assert len(events) == expected
    # Turn indices are 1..N in order.
    assert [e["turn"] for e in events] == list(range(1, expected + 1))


@pytest.mark.parametrize(
    "entry, stream",
    [
        pytest.param(_CLAUDE_ENTRY, _CLAUDE_STREAM, id="claude"),
        pytest.param(_CODEX_ENTRY, _CODEX_STREAM, id="codex"),
    ],
)
def test_emit_events_passes_stream_through_unchanged(
    tmp_path: Path, entry: Path, stream: str
) -> None:
    """The raw CLI stream is teed to stdout unchanged so the seam still captures
    harness_stdout for the trace normaliser."""
    events_file = tmp_path / "events.jsonl"
    proc = _run_emit(entry, stream, events_file)
    src_lines = [ln for ln in stream.splitlines() if ln.strip()]
    out_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert out_lines == src_lines


def test_emit_events_skips_non_json_without_erroring(tmp_path: Path) -> None:
    """Torn tail / plain log lines never produce an event and never crash."""
    events_file = tmp_path / "events.jsonl"
    stream = "not json at all\n{\"type\": \"other\"}\n{ broken json\n"
    proc = _run_emit(_CLAUDE_ENTRY, stream, events_file)
    assert proc.returncode == 0
    assert _read_events(events_file) == []


# --- full wrapper path (CLI stdout -> events.jsonl) --------------------------

@pytest.mark.parametrize(
    "entry, cli_name, stream, expected",
    [
        pytest.param(_CLAUDE_ENTRY, "claude", _CLAUDE_STREAM, _CLAUDE_EXPECTED_TURNS, id="claude"),
        pytest.param(_CODEX_ENTRY, "codex", _CODEX_STREAM, _CODEX_EXPECTED_TURNS, id="codex"),
    ],
)
def test_wrapper_captures_events_from_fake_cli(
    tmp_path: Path, entry: Path, cli_name: str, stream: str, expected: int
) -> None:
    """End-to-end wrapper: the entrypoint runs the (fake) CLI, tees stdout, and
    writes events.jsonl to the seam path — proven with a canned stream, no model."""
    transcript = tmp_path / "transcript"
    cli = _make_fake_cli(tmp_path, cli_name, stream)
    proc = subprocess.run(
        ["sh", str(entry), str(cli)],
        capture_output=True,
        text=True,
        timeout=60,
        env=_wrapper_env(tmp_path, transcript),
    )
    assert proc.returncode == 0, proc.stderr
    # Passthrough stdout preserved.
    out_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    src_lines = [ln for ln in stream.splitlines() if ln.strip()]
    assert out_lines == src_lines
    # Events written to the seam path and counted.
    events = _read_events(transcript / "events.jsonl")
    assert count_turns(events) == expected


def test_wrapper_preserves_nonzero_cli_exit(tmp_path: Path) -> None:
    """A non-zero CLI exit (harness loop failure) propagates so the seam errors
    the trial rather than scoring a false zero (SPEC S6.8)."""
    transcript = tmp_path / "transcript"
    cli = _make_fake_cli(tmp_path, "claude", _CLAUDE_STREAM, exit_code=3)
    proc = subprocess.run(
        ["sh", str(_CLAUDE_ENTRY), str(cli)],
        capture_output=True,
        text=True,
        timeout=60,
        env=_wrapper_env(tmp_path, transcript),
    )
    assert proc.returncode == 3
    # Events are still captured even though the loop exited non-zero.
    assert count_turns(_read_events(transcript / "events.jsonl")) == _CLAUDE_EXPECTED_TURNS


def test_wrapper_version_probe_passes_through_no_events(tmp_path: Path) -> None:
    """A bare `--version`-style probe (non-JSON, single line) passes straight
    through and writes no events, so the offline image `--version` check stays
    green."""
    transcript = tmp_path / "transcript"
    cli = _make_fake_cli(tmp_path, "claude", "2.1.19 (Claude Code)\n")
    proc = subprocess.run(
        ["sh", str(_CLAUDE_ENTRY), str(cli)],
        capture_output=True,
        text=True,
        timeout=60,
        env=_wrapper_env(tmp_path, transcript),
    )
    assert proc.returncode == 0
    assert "2.1.19" in proc.stdout
    assert count_turns(_read_events(transcript / "events.jsonl")) == 0


def _path_env() -> str:
    """PATH that includes the dir holding `node` plus system bins (the fake CLI
    scripts and `sh`/`mktemp`/`cat` the wrapper shells out to)."""
    node_dir = str(Path(_NODE).parent) if _NODE else ""
    return f"{node_dir}:/usr/bin:/bin:/usr/local/bin"


def _wrapper_env(tmp_path: Path, transcript: Path) -> dict[str, str]:
    """Env for a host-side wrapper run. In-container the profile dirs default to
    the read-only-here /profile; point them at a writable tmp dir so the offline
    test can exercise the same entrypoint on the host."""
    profile = tmp_path / "profile"
    return {
        "WOLOLO_TRANSCRIPT_DIR": str(transcript),
        "CLAUDE_CONFIG_DIR": str(profile),
        "CODEX_HOME": str(profile),
        "PATH": _path_env(),
    }
