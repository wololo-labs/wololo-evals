"""Claude Code CLI adapter (README §6.1): headless ``claude -p`` print mode
with a JSON event stream on stdout.

The event stream is line-delimited JSON (``--output-format stream-json``); each
line is a Claude Code event the ``trace`` normaliser later maps onto
``TraceEvent``. The final ``result`` event carries cost + token usage, which
``_parse_usage`` reads back out.
"""
from __future__ import annotations

import json
from typing import Sequence

from ..core.interfaces import RunSpec
from ._base import LiveCliAdapter

__all__ = ["ClaudeAdapter"]


class ClaudeAdapter(LiveCliAdapter):
    engine_id = "claude"
    binary = "claude"
    default_version_range = ">=1.0.0,<3.0.0"
    #: §6.2 allow-list for the ``claude`` engine.
    allowed_models = frozenset(
        {
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        }
    )

    def build_command(self, spec: RunSpec) -> Sequence[str]:
        argv = [
            self.binary,
            "-p",
            self._compose_prompt(spec),
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            spec.model_id,
            "--dangerously-skip-permissions",
        ]
        max_turns = spec.extra.get("max_turns")
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        return argv

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
            if "total_cost_usd" in event:
                cost = float(event.get("total_cost_usd") or 0.0)
            usage = event.get("usage")
            if isinstance(usage, dict):
                tin = int(usage.get("input_tokens", tin) or tin)
                tout = int(usage.get("output_tokens", tout) or tout)
        return (cost, tin, tout)
