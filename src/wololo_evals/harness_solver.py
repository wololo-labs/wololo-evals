"""Harness-under-eval subprocess bridge (SPEC S6, the core IP).

Runs a REAL coding harness CLI as the thing being measured: fresh clone of
the task fixture at /workspace, candidate skill materialized into the
harness's containerized profile (treatment arm only, host dotfiles never
touched), the harness's own autonomous multi-turn loop headless, results
read back as git diff + fleet-hooks transcript events.

Import boundary decision (SPEC says both "custom Inspect Solver" and
"inspect_adapter.py is the ONLY module importing inspect_ai"): this module
holds the harness execution logic as pure functions over an injected
``SandboxExec`` seam; ``inspect_adapter`` wraps :func:`run_harness_trial`
into the actual Inspect ``Solver`` and passes ``sandbox().exec`` through the
seam. That keeps the import boundary hard AND the bridge testable with a
fake executor.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Optional

from .harnesses import HarnessRunner

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from .core.interfaces import TraceEvent

WORKSPACE = "/workspace"
PROFILE = "/profile"
TRANSCRIPT = "/transcript"
EVENTS_FILE = f"{TRANSCRIPT}/events.jsonl"
TRACE_FILE = f"{WORKSPACE}/trace.jsonl"

# (cmd, env, timeout_seconds) -> (returncode, stdout, stderr)
SandboxExec = Callable[
    [list[str], Optional[Mapping[str, str]], int],
    Awaitable[tuple[int, str, str]],
]


class TrialErrored(Exception):
    """Setup or harness failure: the trial is 'errored', never scored as a
    zero (a zero would silently poison the arm mean, SPEC S6.8)."""


@dataclass(frozen=True)
class HarnessTrialConfig:
    runner: HarnessRunner
    task_prompt: str
    fixture_repo: str          # clone source (URL or path visible in-sandbox)
    fixture_ref: str           # pinned ref
    arm: str                   # "baseline" | "treatment"
    max_turns: int = 20
    wall_clock_cap_s: int = 900
    # treatment arm only: {relative path under the skill slug: content}
    skill_slug: Optional[str] = None
    skill_files: Optional[Mapping[str, str]] = None


@dataclass
class HarnessTrialResult:
    exit_code: int
    diff: str
    untracked: str            # git status --porcelain (captures new files)
    events: list[dict] = field(default_factory=list)
    turns: int = 0
    wall_clock_s: float = 0.0
    harness_stdout: str = ""
    # Normalised trajectory (SPEC M2): the engine's raw event stream mapped to
    # TraceEvent[] via trace/. Empty for the wave-1 harnesses (no normaliser)
    # and for any engine whose stream fails to parse — trace is an additive
    # artefact, never a reason to error a trial.
    trace: "list[TraceEvent]" = field(default_factory=list)


async def _check(exec_fn: SandboxExec, cmd: list[str], timeout: int = 120,
                 env: Optional[Mapping[str, str]] = None) -> str:
    code, out, err = await exec_fn(cmd, env, timeout)
    if code != 0:
        raise TrialErrored(f"{shlex.join(cmd)} failed ({code}): {err[:500]}")
    return out


def count_turns(events: list[dict]) -> int:
    """Turn count off the fleet-hooks event stream, identical for every
    harness: one turn per post-llm event."""
    return sum(1 for e in events if e.get("stage") == "post-llm-event")


async def _normalise_and_persist_trace(
    exec_fn: SandboxExec, engine_id: str, raw_stream: str
) -> "list[TraceEvent]":
    """Normalise the engine's raw event stream into ``TraceEvent[]`` via the
    open ``trace/`` package and drop a canonical ``trace.jsonl`` beside the
    other trial artefacts in the workspace (SPEC M2, open-surface §14.3/§15.2).

    Best-effort by design: a harness with no registered normaliser (the wave-1
    ``claude-code``/``hermes`` runners) or a stream that fails to parse yields
    an empty trace and never errors the trial — normalisation strictness is
    enforced where the trace is *consumed* for scoring, not in this bridge.
    ``trace/`` imports only ``core.interfaces`` + stdlib, so this adds no DB or
    vendor coupling; the import is lazy so trace-less trials never load it."""
    from wololo_evals import trace as trace_pkg

    if engine_id not in trace_pkg.NORMALISERS:
        return []
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".raw.jsonl", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(raw_stream)
        tmp.close()
        raw_path = pathlib.Path(tmp.name)
        try:
            events = trace_pkg.normalise_stream(engine_id, raw_path)
        except trace_pkg.TraceParseError:
            return []
        # Canonicalise through trace/'s own writer so the persisted bytes match
        # what the replay engine's regression tests expect, then materialise it
        # inside the trial workspace via the exec seam (host dotfiles untouched).
        trace_pkg.write_trace(raw_path, events, engine_id=engine_id)
        content = raw_path.read_text(encoding="utf-8")
    finally:
        os.unlink(tmp.name)
    await exec_fn(
        ["sh", "-c", 'printf %s "$1" > "$2"', "-", content, TRACE_FILE], None, 60
    )
    return events


async def run_harness_trial(
    exec_fn: SandboxExec, cfg: HarnessTrialConfig
) -> HarnessTrialResult:
    if cfg.arm not in ("baseline", "treatment"):
        raise ValueError(f"arm {cfg.arm!r} must be baseline|treatment")

    # 1. Fresh workspace: never a reused checkout (SPEC S6.3).
    await _check(exec_fn, ["rm", "-rf", WORKSPACE, PROFILE, TRANSCRIPT])
    await _check(exec_fn, ["mkdir", "-p", PROFILE, TRANSCRIPT])
    await _check(
        exec_fn,
        ["git", "clone", "--quiet", cfg.fixture_repo, WORKSPACE],
        timeout=300,
    )
    await _check(exec_fn, ["git", "-C", WORKSPACE, "checkout", "--quiet", cfg.fixture_ref])

    # 2. load-profile stage: treatment arm materializes the decrypted skill
    #    into the harness's profile INSIDE the sandbox (SPEC S6.4).
    if cfg.arm == "treatment":
        if not cfg.skill_slug or not cfg.skill_files:
            raise TrialErrored("treatment arm requires skill_slug + skill_files")
        base = f"{PROFILE}/{cfg.runner.skills_subdir}/{cfg.skill_slug}"
        for rel, content in sorted(cfg.skill_files.items()):
            if rel.startswith("/") or ".." in rel.split("/"):
                raise TrialErrored(f"skill file path escapes profile: {rel!r}")
            path = f"{base}/{rel}"
            parent = path.rsplit("/", 1)[0]
            await _check(exec_fn, ["mkdir", "-p", parent])
            # tee reads stdin via sh -c heredoc-free write; content passed as
            # a single argv element so no shell interpolation of payload.
            code, _, err = await exec_fn(
                ["sh", "-c", 'printf %s "$1" > "$2"', "-", content, path], None, 60
            )
            if code != 0:
                raise TrialErrored(f"skill install failed: {err[:200]}")

    # 3. Harness runs its own autonomous multi-turn loop, headless, profile
    #    env pointing INSIDE the sandbox (host dotfiles never mounted).
    env = {cfg.runner.profile_env: PROFILE, "WOLOLO_TRANSCRIPT_DIR": TRANSCRIPT}
    cmd = cfg.runner.render_command(cfg.task_prompt, cfg.max_turns)
    started = time.monotonic()
    code, out, err = await exec_fn(
        ["sh", "-c", f"cd {WORKSPACE} && exec \"$@\"", "-", *cmd],
        env,
        cfg.wall_clock_cap_s,
    )
    wall = time.monotonic() - started
    if code != 0:
        raise TrialErrored(f"harness exited {code}: {err[:500]}")

    # 4. Result capture (SPEC S6.7).
    diff = await _check(exec_fn, ["git", "-C", WORKSPACE, "diff"], timeout=120)
    untracked = await _check(
        exec_fn, ["git", "-C", WORKSPACE, "status", "--porcelain"], timeout=60
    )
    ev_code, ev_out, _ = await exec_fn(["cat", EVENTS_FILE], None, 60)
    events: list[dict] = []
    if ev_code == 0:
        for line in ev_out.splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn tail line never errors the trial

    # 5. Trajectory (SPEC M2, additive): normalise the harness's raw event
    #    stream into trace.jsonl alongside the diff. No-op for harnesses with
    #    no trace/ normaliser, so wave-1 runners are wholly unaffected.
    trace = await _normalise_and_persist_trace(exec_fn, cfg.runner.harness, out)

    return HarnessTrialResult(
        exit_code=code,
        diff=diff,
        untracked=untracked,
        events=events,
        turns=count_turns(events),
        wall_clock_s=wall,
        harness_stdout=out,
        trace=trace,
    )


async def materialize_oracle_files(
    exec_fn: SandboxExec, commit_ref: str, paths: "list[str] | tuple[str, ...]"
) -> None:
    """Overwrite the given workspace paths with their content AT ``commit_ref``,
    so the correctness dimension is measured against the fix-commit's own oracle
    test rather than whatever existed at the redacted parent checkout: a
    newly-added test file is absent at the parent (pytest would exit non-zero on
    a genuinely-correct fix), and a modified test file holds stale pre-fix
    content. Run AFTER the harness diff/transcript are captured and BEFORE
    acceptance; this also defeats a harness that edited the oracle test to cheat.
    ``git checkout <ref> -- <paths>`` creates absent files and needs no clean
    tree. No-op when the fixture has no oracle paths."""
    if not paths:
        return
    await _check(
        exec_fn,
        ["git", "-C", WORKSPACE, "checkout", commit_ref, "--", *paths],
        timeout=120,
    )


async def run_acceptance_test(
    exec_fn: SandboxExec, command: list[str], timeout: int = 600
) -> bool:
    """Correctness dimension: run the task's acceptance test in the SAME
    sandbox against the modified workspace. Pass/fail by exit code, never
    LLM opinion (SPEC S5.2)."""
    code, _, _ = await exec_fn(
        ["sh", "-c", f"cd {WORKSPACE} && exec \"$@\"", "-", *command], None, timeout
    )
    return code == 0
