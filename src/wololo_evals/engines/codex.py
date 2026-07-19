"""Codex CLI adapter (README §6.1): non-interactive ``codex exec`` mode with a
JSON event stream.

``codex exec --json`` runs the prompt to completion headlessly and streams
JSON events on stdout; the terminal event carries token usage. Codex reports
tokens but not a normalised USD cost, so ``_parse_usage`` returns tokens with a
zero cost (matrix-side pricing turns tokens into spend).
"""
from __future__ import annotations

import json
from typing import Sequence

from ..core.interfaces import RunSpec
from ._base import LiveCliAdapter

__all__ = ["CodexAdapter"]


class CodexAdapter(LiveCliAdapter):
    engine_id = "codex"
    binary = "codex"
    default_version_range = ">=0.5.0,<2.0.0"
    #: §6.2 allow-list for the ``codex`` engine.
    allowed_models = frozenset(
        {"gpt-5.5", "gpt-5.4", "gpt-5.4-codex", "gpt-5.4-mini"}
    )

    def build_command(self, spec: RunSpec) -> Sequence[str]:
        return [
            self.binary,
            "exec",
            "--json",
            "--model",
            spec.model_id,
            "--skip-git-repo-check",
            self._compose_prompt(spec),
        ]

    def _parse_usage(self, raw_stdout: str) -> tuple[float, int, int]:
        cost, tin, tout = 0.0, 0, 0
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage") or event.get("token_usage")
            if isinstance(usage, dict):
                tin = int(
                    usage.get("input_tokens", usage.get("prompt_tokens", tin)) or tin
                )
                tout = int(
                    usage.get("output_tokens", usage.get("completion_tokens", tout))
                    or tout
                )
            if "cost_usd" in event:
                cost = float(event.get("cost_usd") or 0.0)
        return (cost, tin, tout)
