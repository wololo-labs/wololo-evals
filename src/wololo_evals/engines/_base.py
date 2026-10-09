"""Shared live-CLI adapter flow (README §14).

``LiveCliAdapter`` implements the whole engine-neutral pipeline once — version
pinning, model allow-list, asset mounting, env minimisation, subprocess launch
over the injected exec seam, capture-file writing, the §14.2 status taxonomy
with a single automatic ``infra_fail`` retry, cost-cap enforcement, and handing
the raw stream to an optional trace normaliser. The per-engine subclasses
(``claude``/``codex``/``copilot``) supply only what actually differs: the CLI
binary, the headless argv, the default pinned version range, the model
allow-list, and how to read cost/tokens out of that engine's native stream.

Every live call goes through ``self._exec`` (a :data:`ExecFn`); tests inject a
fake and never spawn a real CLI. The adapter never imports a vendor SDK and
never imports the ``trace``/``db``/``api`` packages — the normaliser is an
injected ``TraceNormaliser`` (or ``None``), so ``engines/`` and ``trace/`` stay
independently buildable.
"""
from __future__ import annotations

import dataclasses
import pathlib
import time
from typing import Mapping, Optional, Sequence

from ..core.interfaces import (
    ADVISORY_DIR,
    RunResult,
    RunSpec,
    TraceNormaliser,
)
from . import env_minimise, version
from .exec_seam import ExecFn, ExecResult, subprocess_exec

__all__ = ["LiveCliAdapter"]


class LiveCliAdapter:
    """Base for the three live engine adapters. Structurally satisfies
    ``core.interfaces.EngineAdapter``."""

    # --- per-engine class contract (subclasses override) ---
    engine_id: str = ""
    binary: str = ""
    version_argv: Sequence[str] = ("--version",)
    default_version_range: str = "*"
    allowed_models: frozenset[str] = frozenset()
    #: Workspace-relative directory this CLI discovers project skills in.
    #: Empty means the engine has no such directory and a skill asset mounts
    #: at the workspace root.
    skill_mount_prefix: str = ""

    def __init__(
        self,
        *,
        exec_fn: ExecFn = subprocess_exec,
        normaliser: Optional[TraceNormaliser] = None,
        version_range: Optional[str] = None,
    ) -> None:
        self._exec = exec_fn
        self._normaliser = normaliser
        self._version_range_override = version_range

    # ------------------------------------------------------------------ #
    # EngineAdapter surface
    # ------------------------------------------------------------------ #
    def cli_version(self) -> str:
        """Installed CLI ``--version`` string (raw), captured for provenance."""
        res = self._exec(
            [self.binary, *self.version_argv],
            env=env_minimise.build_env(self.engine_id, {}),
            cwd=".",
            timeout=30,
        )
        if res.returncode != 0:
            raise version.EngineVersionError(
                f"{self.binary} --version failed (exit {res.returncode}): "
                f"{res.stderr.strip()}"
            )
        return res.stdout.strip() or res.stderr.strip()

    def build_command(self, spec: RunSpec) -> Sequence[str]:  # pragma: no cover
        raise NotImplementedError

    def raw_event_path(self, spec: RunSpec) -> pathlib.Path:
        return self._advisory_dir(spec) / f"{self.engine_id}.raw.jsonl"

    def run(self, spec: RunSpec) -> RunResult:
        started = time.monotonic()
        adv = self._advisory_dir(spec)
        adv.mkdir(parents=True, exist_ok=True)

        # Pre-launch validation: version pin then model allow-list (§14.1).
        cli_version = self._check_version(spec)
        self._check_model(spec)

        # Materialise asset + prompt into the workspace.
        self._mount_asset(spec)
        prompt = self._compose_prompt(spec)
        (adv / "prompt.md").write_text(prompt, encoding="utf-8")

        env = env_minimise.build_env(self.engine_id, spec.env)
        argv = list(self.build_command(spec))
        timeout = (
            None if spec.caps.wall_time_seconds is None else float(spec.caps.wall_time_seconds)
        )

        # Launch with one automatic retry on infra_fail (§14.2).
        attempts = 0
        res: ExecResult
        status: str
        while True:
            attempts += 1
            res = self._exec(argv, env=env, cwd=spec.cwd, timeout=timeout)
            status = self._classify(res)
            if status == "infra_fail" and attempts == 1:
                continue
            break

        # Persist capture files (§15 tree).
        (adv / f"{self.engine_id}.stdout.log").write_text(res.stdout, encoding="utf-8")
        (adv / f"{self.engine_id}.stderr.log").write_text(res.stderr, encoding="utf-8")
        raw_path = self.raw_event_path(spec)
        raw_path.write_text(res.stdout, encoding="utf-8")

        cost, tokens_in, tokens_out = (0.0, 0, 0)
        if status == "ok":
            cost, tokens_in, tokens_out = self._parse_usage(res.stdout)
            # Per-run cost cap: breaching it downgrades an otherwise-ok run.
            cap = spec.caps.max_cost_usd
            if cap is not None and cost > cap:
                status = "cost_cap_hit"

        # Normalise the raw stream into trace.jsonl when a normaliser is wired.
        if status == "ok" and self._normaliser is not None:
            self._write_trace(spec, raw_path, adv)

        runtime = time.monotonic() - started
        error = None
        if status != "ok":
            error = (res.stderr.strip() or f"engine exited {res.returncode}")[:2000]
        return RunResult(
            run_id=spec.run_id,
            status=status,
            artefact_dir=str(adv),
            runtime_seconds=runtime,
            cost_usd=cost,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            attempts=attempts,
            error=error,
            metadata={
                "engine_id": self.engine_id,
                "model_id": spec.model_id,
                "cli_version": cli_version,
                "exit_code": res.returncode,
            },
        )

    # ------------------------------------------------------------------ #
    # per-engine hooks (subclasses may override)
    # ------------------------------------------------------------------ #
    def _parse_usage(self, raw_stdout: str) -> tuple[float, int, int]:
        """Read (cost_usd, tokens_in, tokens_out) from the engine-native
        stream. Default: unknown -> zeros (still a valid ``ok`` run)."""
        return (0.0, 0, 0)

    def _is_valid_output(self, res: ExecResult) -> bool:
        """An exit-0 run with empty output is ``model_fail`` (§14.2)."""
        return bool(res.stdout.strip())

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _advisory_dir(self, spec: RunSpec) -> pathlib.Path:
        return pathlib.Path(spec.cwd) / ADVISORY_DIR

    def _classify(self, res: ExecResult) -> str:
        if res.timed_out:
            return "timeout"
        if res.returncode != 0:
            return "infra_fail"
        if not self._is_valid_output(res):
            return "model_fail"
        return "ok"

    def _check_version(self, spec: RunSpec) -> str:
        """Capture the installed CLI version, enforce the pinned range, and
        return the raw version string for run provenance."""
        installed = self.cli_version()
        rng = (
            spec.extra.get("version_range")
            or self._version_range_override
            or self.default_version_range
        )
        allow_drift = bool(spec.extra.get("allow_engine_drift", False))
        version.check_range(installed, str(rng), allow_drift=allow_drift)
        return installed

    def _check_model(self, spec: RunSpec) -> None:
        if self.allowed_models and spec.model_id not in self.allowed_models:
            raise ValueError(
                f"model {spec.model_id!r} is not in the {self.engine_id} "
                f"allow-list {sorted(self.allowed_models)}"
            )

    def _mount_asset(self, spec: RunSpec) -> None:
        """Write ``AssetSnapshot.files`` into the run workspace, path-traversal
        checked exactly like the wave-1 load-profile stage (harness-seam.md
        §3): no absolute paths, no ``..`` escape.

        An ``agent_skill`` asset lands under :attr:`skill_mount_prefix`, the
        directory this CLI discovers project skills in. Dropped at the
        workspace root it is just a markdown file the harness never loads, and
        the run would score zero on activation for a packaging reason rather
        than anything about the skill.
        """
        base = pathlib.Path(spec.cwd)
        if spec.asset.ref.kind == "agent_skill" and self.skill_mount_prefix:
            base = base / self.skill_mount_prefix
        for rel, content in spec.asset.files.items():
            if rel.startswith("/") or ".." in pathlib.PurePosixPath(rel).parts:
                raise ValueError(f"unsafe asset file path {rel!r}")
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(content), encoding="utf-8")

    def _compose_prompt(self, spec: RunSpec) -> str:
        """The case prompt, after any declared preamble.

        A preamble exists for one reason: to separate what an asset *contains*
        from whether a model *routes* to it. Told to use a skill, a run scores
        the skill's instructions; left to choose, it scores discovery as well.
        Both are worth knowing and they are not the same number, so an arm
        declares which it is measuring.
        """
        case = spec.case
        preamble = str(spec.extra.get("prompt_preamble") or "").strip()
        body = self._case_body(spec)
        return f"{preamble}\n\n{body}" if preamble else body

    def _case_body(self, spec: RunSpec) -> str:
        case = spec.case
        if case.turns is None:
            return case.input_text
        chunks = []
        for turn in case.turns:
            text = turn.get("content") or turn.get("text") or turn.get("input_text") or ""
            role = turn.get("role", "user")
            chunks.append(f"[{role}] {text}")
        return "\n".join(chunks)

    def _write_trace(
        self, spec: RunSpec, raw_path: pathlib.Path, adv: pathlib.Path
    ) -> None:
        import json

        events = self._normaliser.normalise(raw_path)  # type: ignore[union-attr]
        lines = [
            json.dumps(dataclasses.asdict(ev), ensure_ascii=False, separators=(",", ":"))
            for ev in events
        ]
        (adv / "trace.jsonl").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
