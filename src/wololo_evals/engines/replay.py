"""Replay engine (README §14.4): re-emit an archived raw event log with zero
network and zero spend.

The replay adapter is the substrate the framework's own regression tests run
on — it makes the whole scoring/trace/presenter stack reproducible in CI
without ever calling a model. It re-materialises an archived ``<engine>.raw.jsonl``
into the run workspace, carries the *archived* run's recorded
``engine_id``/``model_id`` (a replay combo is exempt from the live allow-list
and version-range checks, §14.4), hands the stream to the normaliser exactly
like a live run, and reports ``cost_usd == 0``.

Resolution: the archive root is ``[engines.replay] runs_dir``. For a given
``(case, seed)`` the source log is found (in order) at
``spec.extra["replay_source"]`` (an explicit file), then
``runs_dir/<case_id>/seed-<seed>/``, ``runs_dir/<case_id>/``, then ``runs_dir``
itself. Each candidate directory carries a ``provenance.json``
(``engine_id``/``model_id``/token counts) alongside ``<engine_id>.raw.jsonl``;
absent a provenance file the engine is derived from the ``*.raw.jsonl`` stem.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Optional

from ..core.interfaces import ADVISORY_DIR, RunResult, RunSpec, TraceNormaliser

__all__ = ["ReplayAdapter", "ReplaySourceError"]

_REPLAY_VERSION = "replay-1.0"


class ReplaySourceError(FileNotFoundError):
    """No archived raw log resolves for this (case, seed) under ``runs_dir``."""


@dataclasses.dataclass(frozen=True)
class _ArchivedRun:
    source: pathlib.Path
    engine_id: str
    model_id: str
    tokens_in: int
    tokens_out: int


class ReplayAdapter:
    """Structurally satisfies ``core.interfaces.EngineAdapter``; offline."""

    engine_id = "replay"

    def __init__(
        self,
        *,
        runs_dir: str,
        normaliser: Optional[TraceNormaliser] = None,
    ) -> None:
        self._runs_dir = pathlib.Path(runs_dir)
        self._normaliser = normaliser

    # ------------------------------------------------------------------ #
    # EngineAdapter surface
    # ------------------------------------------------------------------ #
    def cli_version(self) -> str:
        """No CLI is launched; replay is exempt from version capture (§14.4)."""
        return _REPLAY_VERSION

    def build_command(self, spec: RunSpec) -> list[str]:
        """Replay never spawns a subprocess."""
        return []

    def raw_event_path(self, spec: RunSpec) -> pathlib.Path:
        archived = self._resolve(spec)
        return self._advisory_dir(spec) / f"{archived.engine_id}.raw.jsonl"

    def run(self, spec: RunSpec) -> RunResult:
        adv = self._advisory_dir(spec)
        adv.mkdir(parents=True, exist_ok=True)
        archived = self._resolve(spec)

        raw_text = archived.source.read_text(encoding="utf-8")
        raw_path = adv / f"{archived.engine_id}.raw.jsonl"
        raw_path.write_text(raw_text, encoding="utf-8")
        # Mirror to the stdout capture slot so the §15 tree is complete.
        (adv / f"{archived.engine_id}.stdout.log").write_text(raw_text, encoding="utf-8")
        (adv / f"{archived.engine_id}.stderr.log").write_text("", encoding="utf-8")
        (adv / "prompt.md").write_text(self._compose_prompt(spec), encoding="utf-8")

        if self._normaliser is not None:
            events = self._normaliser.normalise(raw_path)
            lines = [
                json.dumps(
                    dataclasses.asdict(ev), ensure_ascii=False, separators=(",", ":")
                )
                for ev in events
            ]
            (adv / "trace.jsonl").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
            )

        return RunResult(
            run_id=spec.run_id,
            status="ok",
            artefact_dir=str(adv),
            runtime_seconds=0.0,
            cost_usd=0.0,  # zero spend, always (§14.4)
            tokens_in=archived.tokens_in,
            tokens_out=archived.tokens_out,
            attempts=1,
            error=None,
            metadata={
                "engine_id": archived.engine_id,
                "model_id": archived.model_id,
                "replay_source": str(archived.source),
            },
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _advisory_dir(self, spec: RunSpec) -> pathlib.Path:
        return pathlib.Path(spec.cwd) / ADVISORY_DIR

    def _compose_prompt(self, spec: RunSpec) -> str:
        case = spec.case
        if case.turns is None:
            return case.input_text
        return "\n".join(str(t.get("content") or t.get("text") or "") for t in case.turns)

    def _candidate_dirs(self, spec: RunSpec) -> list[pathlib.Path]:
        return [
            self._runs_dir / spec.case.case_id / f"seed-{spec.seed}",
            self._runs_dir / spec.case.case_id,
            self._runs_dir,
        ]

    def _resolve(self, spec: RunSpec) -> _ArchivedRun:
        explicit = spec.extra.get("replay_source")
        if explicit:
            src = pathlib.Path(str(explicit))
            if not src.exists():
                raise ReplaySourceError(f"replay_source {src} does not exist")
            return self._from_dir(src.parent, source=src)

        for cand in self._candidate_dirs(spec):
            if not cand.is_dir():
                continue
            archived = self._from_dir(cand)
            if archived is not None:
                return archived
        raise ReplaySourceError(
            f"no archived raw log for case {spec.case.case_id!r} seed {spec.seed} "
            f"under {self._runs_dir}"
        )

    def _from_dir(
        self, directory: pathlib.Path, *, source: Optional[pathlib.Path] = None
    ) -> Optional[_ArchivedRun]:
        prov_path = directory / "provenance.json"
        prov = {}
        if prov_path.exists():
            prov = json.loads(prov_path.read_text(encoding="utf-8"))

        if source is not None:
            engine_id = prov.get("engine_id") or source.name.split(".")[0]
        else:
            engine_id = prov.get("engine_id")
            if engine_id:
                source = directory / f"{engine_id}.raw.jsonl"
            else:
                matches = sorted(directory.glob("*.raw.jsonl"))
                if not matches:
                    return None
                source = matches[0]
                engine_id = source.name.split(".")[0]
            if not source.exists():
                return None

        return _ArchivedRun(
            source=source,
            engine_id=str(engine_id),
            model_id=str(prov.get("model_id", "")),
            tokens_in=int(prov.get("tokens_in", 0) or 0),
            tokens_out=int(prov.get("tokens_out", 0) or 0),
        )
