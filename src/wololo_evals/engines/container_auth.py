"""Container auth plan for the live docker-sandbox integration (wave-1).

The wave-1 harness runs each trial in a per-engine docker image via
``sandbox().exec(cmd, env, timeout)`` (harness-seam.md §2). For a *live* run the
engine CLI inside that container needs its own auth material — but the container
is otherwise hermetic (host ``$HOME`` / ambient env are never forwarded). This
module is the single pure helper that decides, per engine, **how** to hand that
one engine's auth into the container:

- **env passthrough** — the KEY names of the auth env vars that will be set on
  the ``sandbox().exec`` call (e.g. ``CLAUDE_CODE_OAUTH_TOKEN``,
  ``OPENAI_API_KEY``);
- **read-only bind-mounts** — host credential files mounted read-only into the
  container at the path the CLI reads (e.g. ``~/.claude/.credentials.json`` ->
  ``/root/.claude/.credentials.json``; ``~/.codex/auth.json`` ->
  ``/root/.codex/auth.json``).

Security contract (non-negotiable):

- :class:`ContainerAuthPlan` — the value this module *returns and that callers
  may log* — holds only env KEY names, mount paths, a ``satisfied`` flag, and
  human-readable operator-precondition strings. It contains **no secret values**
  and its ``repr`` is therefore safe to print. Building the plan reads
  *presence* of a token, never its value.
- Actual secret VALUES are fetched lazily, only at exec time, through the
  separate :func:`resolve_exec_env`. That function returns a plain
  ``{KEY: VALUE}`` dict for ``sandbox().exec`` and **never logs**. The plan and
  the value-resolution are deliberately two different calls so the "returned /
  logged" object and the "secret-bearing" object are never the same object.
- Missing auth is **not** a crash: :func:`build_plan` returns a plan with
  ``satisfied=False`` and a clear operator-precondition marker, so the wiring
  can surface an operator gate instead of raising deep in a trial.

Stdlib only. Imports nothing from ``db``/``api``/``jobs`` (open-dir rule).
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

__all__ = [
    "BindMount",
    "ContainerAuthPlan",
    "SUPPORTED_ENGINES",
    "build_plan",
    "resolve_exec_env",
]


@dataclass(frozen=True)
class BindMount:
    """One host->container read-only bind-mount for a credential file.

    ``host_path`` is absolute (``~`` already expanded) and is only ever added to
    a plan when the file actually exists on the host — you cannot bind-mount a
    missing file. ``read_only`` is always ``True``: the container may read the
    credential, never write it, so a trial can't tamper with host auth."""

    host_path: str
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class _JsonTokenSource:
    """How to recover an env token VALUE from a credential JSON file when the
    env var itself is unset (e.g. ``OPENAI_API_KEY`` embedded in
    ``~/.codex/auth.json``). Used *only* by :func:`resolve_exec_env` at exec
    time; never dereferenced while building a (loggable) plan."""

    env_var: str
    path: str
    #: candidate JSON key paths (each a tuple of nested keys), first hit wins.
    key_paths: Tuple[Tuple[str, ...], ...]


@dataclass(frozen=True)
class _EngineAuth:
    """Static, secret-free description of one engine's container auth surface."""

    engine_id: str
    #: env var candidates carrying the token; first one present (non-empty) in
    #: the source env is forwarded. Order encodes preference (OAuth over API key).
    token_env_vars: Tuple[str, ...]
    #: host credential file (``~``-relative) -> container mount point.
    credential_mounts: Tuple[Tuple[str, str], ...]
    #: optional fallback: parse a token value out of a JSON credential file.
    json_token_source: Optional[_JsonTokenSource] = None
    #: how the operator provisions the env token, for the precondition message.
    provision_hint: str = ""


#: Per-engine auth specs. Wave-1 targets claude + codex; copilot is described so
#: the shape extends without a code change to the resolver.
_AUTH_SPECS: Mapping[str, _EngineAuth] = {
    "claude": _EngineAuth(
        engine_id="claude",
        token_env_vars=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
        credential_mounts=(
            ("~/.claude/.credentials.json", "/root/.claude/.credentials.json"),
        ),
        json_token_source=None,
        provision_hint=(
            "set CLAUDE_CODE_OAUTH_TOKEN (run: claude setup-token) "
            "or ensure ~/.claude/.credentials.json exists on the host"
        ),
    ),
    "codex": _EngineAuth(
        engine_id="codex",
        token_env_vars=("OPENAI_API_KEY", "CODEX_AUTH_TOKEN"),
        credential_mounts=(("~/.codex/auth.json", "/root/.codex/auth.json"),),
        json_token_source=_JsonTokenSource(
            env_var="OPENAI_API_KEY",
            path="~/.codex/auth.json",
            key_paths=(("OPENAI_API_KEY",), ("tokens", "OPENAI_API_KEY")),
        ),
        provision_hint=(
            "set OPENAI_API_KEY (or run: codex login) "
            "or ensure ~/.codex/auth.json exists on the host"
        ),
    ),
    "copilot": _EngineAuth(
        engine_id="copilot",
        token_env_vars=("COPILOT_GITHUB_TOKEN",),
        credential_mounts=(),
        json_token_source=None,
        provision_hint="set COPILOT_GITHUB_TOKEN on the host",
    ),
}

#: Engines this module can build a container auth plan for.
SUPPORTED_ENGINES: Tuple[str, ...] = tuple(_AUTH_SPECS)


@dataclass(frozen=True)
class ContainerAuthPlan:
    """The loggable, secret-free container auth plan for one engine.

    Every field is safe to print: ``env_keys`` are KEY names (not values),
    ``mounts`` are file paths, ``operator_preconditions`` are human-readable
    strings. There is deliberately **no** field carrying a token value or a
    reference to the source environment, so ``repr(plan)`` can never leak a
    secret. Fetch the actual values with :func:`resolve_exec_env` at exec time.

    ``satisfied`` is the operator-precondition marker: ``True`` means enough
    auth was found to attempt a live run; ``False`` means the operator must act
    first (see ``operator_preconditions``) — it is a marker, not an error."""

    engine_id: str
    #: env var KEY names that will be set on ``sandbox().exec`` (values fetched
    #: lazily by :func:`resolve_exec_env`). Never carries values.
    env_keys: Tuple[str, ...] = ()
    #: read-only bind-mounts for credential files that exist on the host.
    mounts: Tuple[BindMount, ...] = ()
    #: whether enough auth was found to attempt a live run.
    satisfied: bool = False
    #: operator-gate markers when ``satisfied`` is ``False`` (else empty).
    operator_preconditions: Tuple[str, ...] = field(default_factory=tuple)


def _expand(path: str, home: pathlib.Path) -> pathlib.Path:
    """Expand a leading ``~`` against ``home`` (injectable for tests) and any
    ``$VAR`` against the process env; return an absolute path."""
    if path.startswith("~"):
        expanded = str(home) + path[1:]
    else:
        expanded = path
    return pathlib.Path(os.path.expandvars(expanded))


def _present_token_key(
    spec: _EngineAuth, src: Mapping[str, str]
) -> Optional[str]:
    """The first token env KEY that is set (non-empty) in ``src`` — KEY only,
    the value is never returned or logged."""
    for name in spec.token_env_vars:
        if src.get(name):
            return name
    return None


def build_plan(
    engine_id: str,
    *,
    source_env: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> ContainerAuthPlan:
    """Build the secret-free container auth plan for ``engine_id``.

    Reads only *presence* of tokens and *existence* of credential files — never
    a secret value. ``source_env`` defaults to ``os.environ`` and ``home`` to
    ``~`` (both injectable for tests). Raises ``ValueError`` only for an unknown
    engine id (a programming error); **missing auth never raises** — it yields a
    plan with ``satisfied=False`` and an operator-precondition marker.
    """
    spec = _AUTH_SPECS.get(engine_id)
    if spec is None:
        raise ValueError(
            f"unknown engine_id {engine_id!r}; supported: {list(SUPPORTED_ENGINES)}"
        )
    src = os.environ if source_env is None else source_env
    home_path = pathlib.Path(home) if home is not None else pathlib.Path.home()

    env_keys: list[str] = []
    mounts: list[BindMount] = []

    # 1. env token: forward the first present KEY (name only).
    token_key = _present_token_key(spec, src)
    if token_key is not None:
        env_keys.append(token_key)

    # 2. credential-file mounts: only for files that actually exist.
    have_cred_file = False
    for host_rel, container_path in spec.credential_mounts:
        host_abs = _expand(host_rel, home_path)
        if host_abs.is_file():
            have_cred_file = True
            mounts.append(
                BindMount(
                    host_path=str(host_abs),
                    container_path=container_path,
                    read_only=True,
                )
            )

    # 3. JSON-embedded token fallback: if the env token is absent but the
    #    credential JSON exists, the CLI's env var will be materialised from it
    #    at exec time — advertise the KEY now (value stays lazy).
    jts = spec.json_token_source
    if token_key is None and jts is not None:
        if _expand(jts.path, home_path).is_file() and jts.env_var not in env_keys:
            env_keys.append(jts.env_var)

    satisfied = bool(env_keys) or have_cred_file
    preconditions: Tuple[str, ...] = ()
    if not satisfied:
        preconditions = (
            f"operator precondition [{engine_id}]: no container auth found — "
            f"{spec.provision_hint} (read-only mount) before the live run",
        )

    return ContainerAuthPlan(
        engine_id=engine_id,
        env_keys=tuple(env_keys),
        mounts=tuple(mounts),
        satisfied=satisfied,
        operator_preconditions=preconditions,
    )


def _dig(obj: object, keys: Tuple[str, ...]) -> Optional[str]:
    """Walk nested dict keys; return a non-empty string leaf or ``None``."""
    cur: object = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur if isinstance(cur, str) and cur else None


def resolve_exec_env(
    engine_id: str,
    *,
    source_env: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> Dict[str, str]:
    """Resolve the actual ``{KEY: VALUE}`` auth env for ``sandbox().exec``.

    This is the ONLY function that touches secret values. It is called at exec
    time by the sandbox wiring, and its result is passed straight to
    ``sandbox().exec(env=...)`` — it must never be logged. Values come from
    ``source_env`` first, falling back to parsing the engine's credential JSON
    (e.g. ``OPENAI_API_KEY`` out of ``~/.codex/auth.json``) when the env var is
    unset. Only the keys advertised by :func:`build_plan` (``plan.env_keys``)
    can appear here, so a caller can log the plan and trust the resolved dict
    carries no surprise keys. Never raises for missing/corrupt files — a key
    that cannot be resolved is simply absent from the returned dict.
    """
    spec = _AUTH_SPECS.get(engine_id)
    if spec is None:
        raise ValueError(
            f"unknown engine_id {engine_id!r}; supported: {list(SUPPORTED_ENGINES)}"
        )
    src = os.environ if source_env is None else source_env
    home_path = pathlib.Path(home) if home is not None else pathlib.Path.home()

    resolved: Dict[str, str] = {}

    # env passthrough: first present token var (value copied, never logged).
    token_key = _present_token_key(spec, src)
    if token_key is not None:
        resolved[token_key] = src[token_key]

    # JSON fallback: materialise the env token from the credential file.
    jts = spec.json_token_source
    if token_key is None and jts is not None:
        cred = _expand(jts.path, home_path)
        try:
            data = json.loads(cred.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data is not None:
            for key_path in jts.key_paths:
                value = _dig(data, key_path)
                if value is not None:
                    resolved[jts.env_var] = value
                    break

    return resolved
