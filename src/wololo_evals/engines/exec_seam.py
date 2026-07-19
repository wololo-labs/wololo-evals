"""The single subprocess seam every live engine adapter runs on.

The adapters do all their execution through one injected callable, exactly the
way the wave-1 ``harness_solver`` runs over ``SandboxExec`` (harness-seam.md
§2): production plugs in :func:`subprocess_exec` (a thin ``subprocess.run``
wrapper); tests inject a fake that never spawns a process. This keeps the
adapters unit-testable with **no live CLI calls** — the whole point of the A1
contract.

Unlike the wave-1 seam this one is *synchronous*: the ``EngineAdapter`` Protocol
is ``run(spec) -> RunResult`` (not a coroutine), so a blocking ``subprocess.run``
is the natural fit and the fake seam stays a plain function.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

__all__ = ["ExecResult", "ExecFn", "subprocess_exec"]


@dataclass(frozen=True)
class ExecResult:
    """One subprocess outcome. ``timed_out`` distinguishes a wall-clock kill
    (``RunResult.status == "timeout"``) from an ordinary non-zero exit
    (``infra_fail``)."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


#: The seam signature. An adapter calls ``exec_fn(argv, env=..., cwd=...,
#: timeout=...)`` and gets back an :class:`ExecResult`. ``timeout`` is the
#: per-run wall-clock cap in seconds (``None`` = uncapped).
ExecFn = Callable[..., ExecResult]


def subprocess_exec(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    timeout: Optional[float] = None,
) -> ExecResult:
    """Production seam: run ``argv`` headless with a *constructed* environment.

    ``env`` fully replaces the child environment (it is the minimised env from
    :func:`env_minimise.build_env`, never ``os.environ`` merged in) so no
    ambient token leaks past the adapter. Never raises on a non-zero exit or a
    timeout — both are reported through :class:`ExecResult` for the status
    taxonomy (§14.2) to classify.
    """
    try:
        proc = subprocess.run(
            list(argv),
            env=dict(env),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecResult(
            returncode=124,
            stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            timed_out=True,
        )
    except (FileNotFoundError, OSError) as exc:  # CLI not installed / not launchable
        return ExecResult(returncode=127, stdout="", stderr=str(exc))
    return ExecResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
