"""Engine adapters (open, Apache-2.0): the harness pillar of the multi-engine
expansion (README §5, §6, §14).

One ``EngineAdapter`` per engine id — ``claude`` (headless ``-p`` print mode),
``codex`` (non-interactive ``exec``), ``copilot`` (programmatic mode), and the
offline ``replay`` engine — each building a headless CLI argv for a ``RunSpec``,
pinning the CLI version, minimising the subprocess environment to only that
engine's token, running through an injected subprocess seam, writing the §15
capture files under ``.gh-aw-advisory/``, handing the raw stream to a trace
normaliser, and returning a ``RunResult`` under the §14.2 status taxonomy (with
one automatic ``infra_fail`` retry).

Import boundary: ``core.interfaces`` + stdlib + ``subprocess`` only. No vendor
SDK (all model calls go through the CLI), no ``db``/``api``/``jobs``, and no
import of the sibling ``trace`` package — the normaliser is injected, so
``engines/`` and ``trace/`` build independently.
"""
from __future__ import annotations

from ._base import LiveCliAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .env_minimise import build_env
from .exec_seam import ExecFn, ExecResult, subprocess_exec
from .registry import ADAPTER_CLASSES, ADAPTERS, get_adapter
from .replay import ReplayAdapter, ReplaySourceError
from .version import EngineVersionError, check_range, parse_version, satisfies

__all__ = [
    # adapters
    "LiveCliAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "ReplayAdapter",
    # registry
    "ADAPTER_CLASSES",
    "ADAPTERS",
    "get_adapter",
    # exec seam
    "ExecFn",
    "ExecResult",
    "subprocess_exec",
    # version + env
    "build_env",
    "check_range",
    "satisfies",
    "parse_version",
    "EngineVersionError",
    "ReplaySourceError",
]
