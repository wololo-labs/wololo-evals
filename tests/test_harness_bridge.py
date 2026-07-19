"""The engine-adapter seam of the harness bridge (SPEC M2).

A new engine plugs into the wave-1 harness registry as pure data — an adapter
manifest plus a ``_RUNNERS`` entry — with zero change to ``harness_solver``,
``inspect_adapter`` or the worker. This module proves that seam end to end
without a docker daemon, using its OWN fakes (the ``test_harness_uplift`` fakes
hard-code the wave-1 binaries and are deliberately left untouched):

1. the claude/codex/copilot manifests resolve with honest liveness — all three
   are ledgered integration debt (adapter wired, no live LLM run ever exercised
   in the build env), so their event/runtime stages are honestly not live and a
   verdict-bearing run is refused: they are smoke-only until a real live-smoke
   proof exists;
2. the ``extra_args`` seam threads a per-engine model id + permission flags
   through ``render_command`` while leaving the wave-1 runners byte-identical;
3. a live trial normalises the engine's raw event stream into ``TraceEvent[]``
   and drops a canonical ``trace.jsonl`` in the workspace — additively, so a
   harness with no normaliser produces neither trace nor file.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from wololo_evals.harness_solver import (
    TRACE_FILE,
    HarnessTrialConfig,
    run_harness_trial,
)
from wololo_evals.harnesses import (
    HarnessNotEligible,
    load_manifest,
    resolve_runner,
)
from wololo_evals.trace import read_trace


# --- fakes (local, not shared with test_harness_uplift) ---------------------
# A minimal claude ``--output-format stream-json`` stream: one tool_use turn
# (normalises to a ``read`` tool_call on kyc.py) plus the terminal result row.
CLAUDE_STREAM = "\n".join(
    [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "kyc.py"}}
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ),
        json.dumps({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]
)


class FakeEngineSandbox:
    """Sequential fake executor for a single engine trial. Records every file
    write (skill install AND trace.jsonl) by path so the trace wiring can be
    asserted, and returns ``stdout`` as the harness's raw event stream."""

    def __init__(self, stdout: str, *, events_n: int = 2):
        self.stdout = stdout
        self.events_n = events_n
        self.writes: dict[str, str] = {}
        self.harness_runs: list[str] = []

    @staticmethod
    def _events(n: int) -> str:
        return "\n".join(
            json.dumps({"stage": "post-llm-event", "i": i}) for i in range(n)
        )

    async def exec(self, cmd, env=None, timeout=0):
        c0 = cmd[0]
        if c0 in ("rm", "mkdir"):
            return (0, "", "")
        if c0 == "git":
            if "clone" in cmd or "checkout" in cmd:
                return (0, "", "")
            if "diff" in cmd:
                return (0, "diff --git a/kyc.py b/kyc.py\n+    validated\n", "")
            if "status" in cmd:
                return (0, "", "")
        if c0 == "cat":
            return (0, self._events(self.events_n), "")
        if c0 == "sh":
            script, rest = cmd[2], cmd[4:]
            if "printf" in script:  # both skill install and trace.jsonl write
                content, path = rest[-2], rest[-1]
                self.writes[path] = content
                return (0, "", "")
            if 'exec "$@"' in script:
                if rest and rest[0] in ("claude", "codex", "copilot"):
                    self.harness_runs.append(rest[0])
                    return (0, self.stdout, "")
                return (0, "", "")  # acceptance command passes
        raise AssertionError(f"fake sandbox got an unexpected command: {cmd}")


# --- 1. manifests resolve with honest liveness ------------------------------
@pytest.mark.parametrize("engine", ["claude", "codex", "copilot"])
def test_new_engine_manifests_are_honestly_smoke_only(engine):
    # None of the three new engines has a live-smoke proof of its fleet-hooks
    # wiring (no LLM CLI is exercised anywhere in this repo — the live run is an
    # OPERATOR precondition, see docs/goals/evals-core.tasks.md T16). Their
    # event/runtime stages are therefore honestly marked live:false, so a
    # verdict-bearing resolve is refused and only a smoke run may proceed.
    with pytest.raises(HarnessNotEligible):
        resolve_runner(engine)
    smoke = resolve_runner(engine, smoke=True)
    assert smoke.harness == engine
    assert smoke.abi_version == "1.0"
    assert smoke.all_stages_live is False
    assert smoke.skills_subdir  # non-empty profile skills path
    assert smoke.command[0] == engine  # the CLI binary


@pytest.mark.parametrize("engine", ["claude", "codex", "copilot"])
def test_new_engine_setup_stages_live_event_stages_not(engine):
    # Honesty is applied identically across all three: the statically-verifiable
    # setup stages (install/load-profile/pre-llm-context) are live; the four
    # stages that can only be proven by a real LLM run are not.
    manifest = load_manifest(engine)
    live = {s["stage"]: s["live"] for s in manifest["stages"]}
    assert live["install"] is True
    assert live["load-profile"] is True
    assert live["pre-llm-context"] is True
    assert live["post-llm-event"] is False
    assert live["event-emit"] is False
    assert live["collector-flush"] is False
    assert live["runtime-smoke"] is False


# --- 2. the extra_args model/permission seam --------------------------------
def test_extra_args_seam_appends_model_and_flags():
    claude = resolve_runner("claude", smoke=True)
    # no model → the bare base template, no model/permission tail
    bare = claude.render_command("fix the bug", 7)
    assert "fix the bug" in bare and "7" in bare
    assert "--model" not in bare
    assert "--dangerously-skip-permissions" not in bare
    # model supplied → the tail is appended, model id substituted
    full = claude.render_command("fix the bug", 7, model="claude-opus-4-7")
    assert full[: len(bare)] == bare  # base is a strict prefix
    assert full[-3:] == ["--model", "claude-opus-4-7", "--dangerously-skip-permissions"]


def test_extra_args_seam_per_engine_flags():
    codex = resolve_runner("codex", smoke=True).render_command("p", 5, model="gpt-5.4-codex")
    assert codex[-2:] == ["--model", "gpt-5.4-codex"]
    copilot = resolve_runner("copilot", smoke=True).render_command(
        "p", 5, model="gpt-5.4"
    )
    assert copilot[-3:] == ["--model", "gpt-5.4", "--allow-all-tools"]


def test_wave1_runner_render_unaffected_by_seam():
    # claude-code has no extra_args: its rendered command is identical whether
    # or not a model is passed, so the wave-1 runners are wholly unaffected.
    cc = resolve_runner("claude-code")
    assert cc.extra_args == ()
    without = cc.render_command("do it", 9)
    with_model = cc.render_command("do it", 9, model="claude-sonnet-4-6")
    assert without == with_model
    assert "--model" not in without


# --- 3. trace normalisation + trace.jsonl in the workspace ------------------
async def test_live_trial_normalises_trace_and_writes_workspace_file():
    fake = FakeEngineSandbox(CLAUDE_STREAM)
    cfg = HarnessTrialConfig(
        runner=resolve_runner("claude", smoke=True), task_prompt="fix it",
        fixture_repo="r", fixture_ref="ref", arm="baseline", max_turns=8,
    )
    result = await run_harness_trial(fake.exec, cfg)

    assert result.exit_code == 0
    assert fake.harness_runs == ["claude"]
    # normalised trajectory attached to the result
    assert len(result.trace) == 1
    ev = result.trace[0]
    assert ev.kind == "tool_call" and ev.target == "kyc.py"
    # a canonical trace.jsonl was materialised in the trial workspace
    assert TRACE_FILE in fake.writes
    content = fake.writes[TRACE_FILE]
    header = json.loads(content.splitlines()[0])
    assert header["kind"] == "trace" and header["engine_id"] == "claude"


async def test_trace_file_is_canonical_and_roundtrips(tmp_path):
    fake = FakeEngineSandbox(CLAUDE_STREAM)
    cfg = HarnessTrialConfig(
        runner=resolve_runner("claude", smoke=True), task_prompt="fix it",
        fixture_repo="r", fixture_ref="ref", arm="baseline",
    )
    result = await run_harness_trial(fake.exec, cfg)
    # the persisted bytes read back through trace/'s own reader to equal events
    p = tmp_path / "trace.jsonl"
    p.write_text(fake.writes[TRACE_FILE], encoding="utf-8")
    restored = read_trace(p)
    assert [e.target for e in restored] == [e.target for e in result.trace]


async def test_wave1_harness_produces_no_trace_and_no_file():
    # claude-code has no trace/ normaliser → trace stays empty and no
    # trace.jsonl is written, so the wave-1 artefact capture is unchanged.
    fake = FakeEngineSandbox("{}")
    cfg = HarnessTrialConfig(
        runner=resolve_runner("claude-code"), task_prompt="fix it",
        fixture_repo="r", fixture_ref="ref", arm="baseline",
    )
    result = await run_harness_trial(fake.exec, cfg)
    assert result.trace == []
    assert TRACE_FILE not in fake.writes


async def test_malformed_engine_stream_yields_empty_trace_not_errored():
    # an engine whose stream fails to parse must not error the trial: trace is
    # best-effort and left empty, and no partial trace.jsonl is written.
    fake = FakeEngineSandbox("not json at all\n{oops")
    cfg = HarnessTrialConfig(
        runner=resolve_runner("codex", smoke=True), task_prompt="fix it",
        fixture_repo="r", fixture_ref="ref", arm="baseline",
    )
    result = await run_harness_trial(fake.exec, cfg)
    assert result.exit_code == 0
    assert result.trace == []
    assert TRACE_FILE not in fake.writes
