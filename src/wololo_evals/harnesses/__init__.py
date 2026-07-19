"""Harness registry: adapter-manifest-driven provider selection (SPEC S6.2).

Which harness may run is resolved from its adapter manifest (the Apache
``specs/harness-adapter`` contract: 7 lifecycle stages, ``abiVersion``);
manifests here are vendored copies of the kernel's declarations, pinned in
docs/INTEGRATION-DEBT.md. A harness whose manifest does not declare all 7
stages ``live: true`` is refused for verdict-bearing runs (a ``smoke``-
flagged run may proceed but can never produce a verdict-bearing ``passed``).
Nothing in suite code hard-codes a provider name: adding a harness is a new
manifest + runner config entry, zero suite-code change.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

_DIR = pathlib.Path(__file__).resolve().parent

# Runner config per harness: how the pinned CLI is launched headless inside
# the sandbox and where the load-profile stage materializes a skill.
_RUNNERS = {
    "claude-code": {
        "profile_env": "CLAUDE_CONFIG_DIR",
        "skills_subdir": "skills",
        "command": [
            "claude", "-p", "{task_prompt}",
            "--output-format", "json",
            "--max-turns", "{max_turns}",
            "--dangerously-skip-permissions",
        ],
    },
    "hermes": {
        "profile_env": "HERMES_HOME",
        "skills_subdir": "skills",
        "command": [
            "hermes", "run", "--non-interactive",
            "--prompt", "{task_prompt}",
            "--max-turns", "{max_turns}",
            "--output", "json",
        ],
    },
    # Engine-aligned runners (SPEC M2). Keyed by the engine id (claude/codex/
    # copilot) so a live trial's raw stdout stream normalises through the
    # matching trace/ normaliser. Each command is the CLI's real headless
    # invocation emitting a line-delimited event stream; the model id and
    # permission flags sit in extra_args (rendered only for a model-bound run).
    "claude": {
        "profile_env": "CLAUDE_CONFIG_DIR",
        "skills_subdir": "skills",
        "command": [
            "claude", "-p", "{task_prompt}",
            "--output-format", "stream-json", "--verbose",
            "--max-turns", "{max_turns}",
        ],
        "extra_args": ["--model", "{model}", "--dangerously-skip-permissions"],
    },
    "codex": {
        "profile_env": "CODEX_HOME",
        "skills_subdir": "skills",
        "command": [
            "codex", "exec", "--json", "--skip-git-repo-check",
            "{task_prompt}",
        ],
        "extra_args": ["--model", "{model}"],
    },
    "copilot": {
        "profile_env": "XDG_CONFIG_HOME",
        "skills_subdir": "copilot/skills",
        "command": [
            "copilot", "-p", "{task_prompt}",
            "--log-level", "all",
        ],
        "extra_args": ["--model", "{model}", "--allow-all-tools"],
    },
}


class HarnessNotEligible(Exception):
    """Manifest missing or not all-stages-live for a verdict-bearing run."""


@dataclass(frozen=True)
class HarnessRunner:
    harness: str
    abi_version: str
    all_stages_live: bool
    profile_env: str
    skills_subdir: str
    command: tuple[str, ...]
    # The one per-engine seam (SPEC M2): model id + permission flags live here,
    # rendered ONLY when a model is supplied so the base command stays a pure
    # {task_prompt}/{max_turns} template. Empty for the wave-1 runners, so their
    # rendered command is byte-identical to before.
    extra_args: tuple[str, ...] = ()

    def render_command(
        self, task_prompt: str, max_turns: int, model: str | None = None
    ) -> list[str]:
        subs = {"task_prompt": task_prompt, "max_turns": max_turns, "model": model or ""}
        argv = [arg.format(**subs) for arg in self.command]
        # The model/permission tail is appended only for a real (model-bound)
        # run; a model-less invocation renders the bare base command (the
        # smoke shape). Runners with no extra_args are unaffected either way.
        if model is not None and self.extra_args:
            argv.extend(arg.format(**subs) for arg in self.extra_args)
        return argv


def load_manifest(harness: str) -> dict:
    path = _DIR / f"{harness}.manifest.json"
    if not path.exists():
        raise HarnessNotEligible(f"no adapter manifest for harness {harness!r}")
    return json.loads(path.read_text())


def resolve_runner(harness: str, *, smoke: bool = False) -> HarnessRunner:
    manifest = load_manifest(harness)
    runner_cfg = _RUNNERS.get(harness)
    if runner_cfg is None:
        raise HarnessNotEligible(f"no runner config for harness {harness!r}")
    all_live = all(stage.get("live") is True for stage in manifest["stages"])
    if not all_live and not smoke:
        raise HarnessNotEligible(
            f"harness {harness!r} manifest is not all-stages live; "
            "only smoke runs may use it and smoke never yields a verdict"
        )
    return HarnessRunner(
        harness=manifest["harness"],
        abi_version=manifest["abiVersion"],
        all_stages_live=all_live,
        profile_env=runner_cfg["profile_env"],
        skills_subdir=runner_cfg["skills_subdir"],
        command=tuple(runner_cfg["command"]),
        extra_args=tuple(runner_cfg.get("extra_args", ())),
    )
