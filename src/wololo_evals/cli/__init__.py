"""File-first CLI for wololo-evals (open surface, Apache-2.0).

A DB-free frontend over the eval core: ``cases compile`` (casebook ->
canonical ``cases.jsonl``), ``matrix`` (expand + ``--dry-run`` + injected-adapter
execution), ``bench`` (single combo), and ``replay`` (zero-spend re-score of an
archived run). All on-disk artefacts follow the README §15 layout via
``core.interfaces.artefacts``; scoring follows the §16.1 composition. This
package imports the eval core, the ``engines``/``trace`` pillars, and stdlib
only — never ``db``/``api``/``jobs``.
"""
from __future__ import annotations

from .main import main

__all__ = ["main"]
