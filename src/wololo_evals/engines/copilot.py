"""GitHub Copilot CLI adapter (README §6.1): programmatic mode emitting a raw
event log.

Copilot's programmatic mode (``copilot -p``) runs headless and writes a raw
line-delimited event log on stdout. Copilot bills against a GitHub token rather
than exposing a per-run USD figure, so ``_parse_usage`` reports tokens when the
log carries them and leaves cost at zero.

The wave proof does not depend on this adapter running live (spec §Edge cases):
it ships with recorded fixtures replayed through the ``replay`` engine; the swap
condition (token + one live smoke) is ledgered in ``docs/INTEGRATION-DEBT.md``.
"""
from __future__ import annotations

import json
from typing import Sequence

from ..core.interfaces import RunSpec
from ._base import LiveCliAdapter

__all__ = ["CopilotAdapter"]


class CopilotAdapter(LiveCliAdapter):
    engine_id = "copilot"
    binary = "copilot"
    default_version_range = ">=0.1.0,<2.0.0"
    #: §6.2 allow-list for the ``copilot`` engine (spans OpenAI + Anthropic
    #: models routed through Copilot).
    allowed_models = frozenset(
        {
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-4.1",
            "claude-sonnet-4.6",
            "claude-haiku-4.5",
            "claude-opus-4.7",
            "claude-opus-4.6",
        }
    )

    def build_command(self, spec: RunSpec) -> Sequence[str]:
        return [
            self.binary,
            "-p",
            self._compose_prompt(spec),
            "--model",
            spec.model_id,
            "--log-level",
            "all",
            "--allow-all-tools",
        ]

    def _parse_usage(self, raw_stdout: str) -> tuple[float, int, int]:
        tin, tout = 0, 0
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
            usage = event.get("usage")
            if isinstance(usage, dict):
                tin = int(usage.get("input_tokens", tin) or tin)
                tout = int(usage.get("output_tokens", tout) or tout)
        return (0.0, tin, tout)
