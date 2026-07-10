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

    def render_command(self, task_prompt: str, max_turns: int) -> list[str]:
        return [
            arg.format(task_prompt=task_prompt, max_turns=max_turns)
            for arg in self.command
        ]


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
    )
