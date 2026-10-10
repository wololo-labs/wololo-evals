"""Environment minimisation for engine subprocesses (README §6.3, §14.1).

An engine subprocess must receive a *constructed* environment carrying only
the token(s) that engine needs, plus a small non-secret baseline (so the CLI
can find itself and its config home) and the caller's declared ``RunSpec.env``.
Fixture, SAML, and GitHub-Actions tokens — and *other engines'* tokens — must
never be present. This is the isolation guarantee that stops a Claude run from
seeing the Codex key or the private-repo fixture token.

The construction is allow-list only: we start from an empty dict and copy in
exactly the vars we name. A new secret introduced to the ambient environment
therefore cannot leak by default — it is dropped unless explicitly listed.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

__all__ = [
    "ENGINE_TOKEN_VARS",
    "SECRET_ENV_VARS",
    "FORBIDDEN_ENV_VARS",
    "SAFE_BASELINE_VARS",
    "build_env",
]

#: Per-engine token env vars (§6.3). The first present in the source
#: environment is forwarded; alternatives cover the OAuth-vs-API-key split.
#: ``replay`` needs no token (offline, §14.4).
ENGINE_TOKEN_VARS: Mapping[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("CODEX_AUTH_TOKEN", "OPENAI_API_KEY"),
    "copilot": ("COPILOT_GITHUB_TOKEN",),
    "replay": (),
}

#: Every engine token var (used to strip *other* engines' tokens).
SECRET_ENV_VARS: frozenset[str] = frozenset(
    v for vs in ENGINE_TOKEN_VARS.values() for v in vs
)

#: Tokens that must never reach any engine subprocess (§14.1): private-repo
#: fixture fetch, SAML materialisation, and Actions API tokens.
FORBIDDEN_ENV_VARS: frozenset[str] = frozenset(
    {"SAML_GITHUB_TOKEN", "GH_AW_FIXTURE_GITHUB_TOKEN", "GITHUB_TOKEN"}
)

#: Non-secret vars the CLI genuinely needs to launch and locate its config;
#: copied from the source environment when present. Not tokens — safe to
#: forward. A caller can still override any of these through ``RunSpec.env``.
SAFE_BASELINE_VARS: tuple[str, ...] = (
    "PATH",
    "HOME",
    # Not decoration: on macOS the CLI resolves its stored credential through
    # the login keychain, and without USER that lookup fails and the run comes
    # back "Not logged in" with no token missing anywhere. A username is not a
    # secret.
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
)


def build_env(
    engine_id: str,
    run_env: Mapping[str, str],
    *,
    source_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Construct the minimised environment for ``engine_id``.

    Result = safe non-secret baseline (from ``source_env``) + this engine's own
    token (from ``source_env``, first alternative present) + declared
    ``run_env`` (wins on conflict). Any forbidden token, or another engine's
    token, that a caller tries to smuggle in through ``run_env`` is stripped
    last, so the isolation guarantee cannot be bypassed by declaration.

    ``source_env`` defaults to ``os.environ`` (injectable for tests).
    """
    if engine_id not in ENGINE_TOKEN_VARS:
        raise ValueError(f"unknown engine_id {engine_id!r}")
    src = os.environ if source_env is None else source_env

    env: Dict[str, str] = {}
    for name in SAFE_BASELINE_VARS:
        if name in src:
            env[name] = src[name]

    # This engine's own token only — first alternative that is set.
    for name in ENGINE_TOKEN_VARS[engine_id]:
        if name in src and src[name]:
            env[name] = src[name]
            break

    # Declared run env, then a final strip so nothing forbidden survives.
    env.update(run_env)
    own_tokens = set(ENGINE_TOKEN_VARS[engine_id])
    for name in list(env):
        if name in FORBIDDEN_ENV_VARS:
            del env[name]
        elif name in SECRET_ENV_VARS and name not in own_tokens:
            del env[name]
    return env
