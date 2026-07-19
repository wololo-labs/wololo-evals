"""Engine adapter registry (README §6.1).

``get_adapter(engine_id)`` is the single lookup the CLI/matrix uses to obtain
an ``EngineAdapter`` for a resolved ``engine_id``; ``ADAPTERS`` is the default
instance map covering every ``ENGINE_IDS`` entry. Adding an engine is a new
class plus one ``ADAPTER_CLASSES`` row — no matrix-code change (mirrors the
wave-1 ``_RUNNERS`` pattern).

The live adapters run over the injected subprocess seam; ``replay`` needs an
archive root (``runs_dir``). ``get_adapter`` accepts the same seam/normaliser/
``runs_dir`` overrides so callers (and tests) can inject a fake exec seam or a
fixture archive without reaching past this module.
"""
from __future__ import annotations

from typing import Mapping, Optional

from ..core.interfaces import ENGINE_IDS, LIVE_ENGINE_IDS, EngineAdapter, TraceNormaliser
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .exec_seam import ExecFn, subprocess_exec
from .replay import ReplayAdapter

__all__ = ["ADAPTER_CLASSES", "ADAPTERS", "get_adapter"]

#: Live engine adapter classes keyed by engine id.
ADAPTER_CLASSES = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "copilot": CopilotAdapter,
}


def get_adapter(
    engine_id: str,
    *,
    exec_fn: ExecFn = subprocess_exec,
    normaliser: Optional[TraceNormaliser] = None,
    runs_dir: Optional[str] = None,
) -> EngineAdapter:
    """Return an adapter for ``engine_id``. ``runs_dir`` is required for the
    ``replay`` engine and ignored for the live engines; ``exec_fn`` is ignored
    for ``replay`` (it never spawns a subprocess)."""
    if engine_id == "replay":
        return ReplayAdapter(runs_dir=runs_dir or ".", normaliser=normaliser)
    cls = ADAPTER_CLASSES.get(engine_id)
    if cls is None:
        raise KeyError(
            f"no engine adapter for {engine_id!r}; known: {sorted(ENGINE_IDS)}"
        )
    return cls(exec_fn=exec_fn, normaliser=normaliser)


#: Default instances for every engine id (replay rooted at the cwd; override
#: via ``get_adapter(..., runs_dir=...)`` for a real archive).
ADAPTERS: Mapping[str, EngineAdapter] = {
    **{eid: get_adapter(eid) for eid in LIVE_ENGINE_IDS},
    "replay": get_adapter("replay"),
}
