"""Container auth plan for the docker-sandbox integration (wave-1).

Proves the invariant behind ``engines/container_auth.py``: for a supported
engine it returns a **secret-free, loggable** plan (env KEY names + read-only
mount paths + operator-precondition marker), degrades to a
``satisfied=False`` marker rather than crashing when auth is absent, and never
lets a secret VALUE reach the returned/printable plan — values come out only of
the separate lazy :func:`resolve_exec_env`.

Pure test: no Postgres, no live CLI, no docker. Env and ``$HOME`` are injected
directly (``source_env=`` / ``home=``) so nothing depends on the host's real
credentials.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from wololo_evals.engines.container_auth import (
    BindMount,
    ContainerAuthPlan,
    SUPPORTED_ENGINES,
    build_plan,
    resolve_exec_env,
)

_CLAUDE_TOKEN = "sk-oauth-CLAUDE-SECRET-abc123"
_OPENAI_TOKEN = "sk-openai-CODEX-SECRET-xyz789"


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _write_claude_creds(home: pathlib.Path, body: str = '{"ok":true}') -> pathlib.Path:
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_codex_auth(home: pathlib.Path, token: str = _OPENAI_TOKEN) -> pathlib.Path:
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"OPENAI_API_KEY": token, "tokens": {"access_token": "t"}}),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# supported engines / unknown engine
# --------------------------------------------------------------------------- #
def test_claude_and_codex_are_supported():
    assert "claude" in SUPPORTED_ENGINES
    assert "codex" in SUPPORTED_ENGINES


def test_unknown_engine_raises():
    with pytest.raises(ValueError):
        build_plan("nope", source_env={}, home="/nowhere")
    with pytest.raises(ValueError):
        resolve_exec_env("nope", source_env={}, home="/nowhere")


# --------------------------------------------------------------------------- #
# claude — env token path
# --------------------------------------------------------------------------- #
def test_claude_env_token_present(tmp_path):
    plan = build_plan(
        "claude",
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN},
        home=str(tmp_path),
    )
    assert isinstance(plan, ContainerAuthPlan)
    assert plan.engine_id == "claude"
    assert plan.env_keys == ("CLAUDE_CODE_OAUTH_TOKEN",)
    assert plan.satisfied is True
    assert plan.operator_preconditions == ()
    # no credential file on this fake home -> no mount.
    assert plan.mounts == ()


def test_claude_credentials_file_mount(tmp_path):
    cred = _write_claude_creds(tmp_path)
    plan = build_plan("claude", source_env={}, home=str(tmp_path))
    assert plan.satisfied is True
    assert plan.env_keys == ()  # no env token, auth comes from the mount
    assert len(plan.mounts) == 1
    mount = plan.mounts[0]
    assert isinstance(mount, BindMount)
    assert mount.host_path == str(cred)
    assert mount.container_path == "/root/.claude/.credentials.json"
    assert mount.read_only is True


def test_claude_missing_auth_is_operator_precondition_not_crash(tmp_path):
    plan = build_plan("claude", source_env={}, home=str(tmp_path))
    assert plan.satisfied is False
    assert plan.env_keys == ()
    assert plan.mounts == ()
    assert plan.operator_preconditions  # non-empty marker
    assert "claude" in plan.operator_preconditions[0]


# --------------------------------------------------------------------------- #
# codex — env token and json-file fallback
# --------------------------------------------------------------------------- #
def test_codex_env_token_present(tmp_path):
    plan = build_plan(
        "codex",
        source_env={"OPENAI_API_KEY": _OPENAI_TOKEN},
        home=str(tmp_path),
    )
    assert plan.env_keys == ("OPENAI_API_KEY",)
    assert plan.satisfied is True
    assert plan.mounts == ()  # no auth.json on this fake home


def test_codex_auth_json_provides_mount_and_advertised_key(tmp_path):
    auth = _write_codex_auth(tmp_path)
    plan = build_plan("codex", source_env={}, home=str(tmp_path))
    assert plan.satisfied is True
    # the mount is present, read-only, at the CLI's expected path.
    assert len(plan.mounts) == 1
    assert plan.mounts[0].host_path == str(auth)
    assert plan.mounts[0].container_path == "/root/.codex/auth.json"
    assert plan.mounts[0].read_only is True
    # OPENAI_API_KEY is advertised as a KEY (value stays lazy).
    assert "OPENAI_API_KEY" in plan.env_keys


def test_codex_missing_auth_is_operator_precondition(tmp_path):
    plan = build_plan("codex", source_env={}, home=str(tmp_path))
    assert plan.satisfied is False
    assert plan.operator_preconditions
    assert "codex" in plan.operator_preconditions[0]


# --------------------------------------------------------------------------- #
# the security invariant — no secret VALUE in the loggable plan
# --------------------------------------------------------------------------- #
def test_plan_never_carries_secret_value_in_repr(tmp_path):
    _write_codex_auth(tmp_path)  # token embedded in the json file too
    claude_plan = build_plan(
        "claude",
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN},
        home=str(tmp_path),
    )
    codex_plan = build_plan("codex", source_env={}, home=str(tmp_path))
    for plan in (claude_plan, codex_plan):
        blob = repr(plan) + str(plan)
        assert _CLAUDE_TOKEN not in blob
        assert _OPENAI_TOKEN not in blob


# --------------------------------------------------------------------------- #
# resolve_exec_env — the only place VALUES appear
# --------------------------------------------------------------------------- #
def test_resolve_exec_env_from_environment(tmp_path):
    env = resolve_exec_env(
        "claude",
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN},
        home=str(tmp_path),
    )
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN}


def test_resolve_exec_env_parses_codex_auth_json(tmp_path):
    _write_codex_auth(tmp_path, token=_OPENAI_TOKEN)
    env = resolve_exec_env("codex", source_env={}, home=str(tmp_path))
    assert env == {"OPENAI_API_KEY": _OPENAI_TOKEN}


def test_resolve_exec_env_env_wins_over_json(tmp_path):
    _write_codex_auth(tmp_path, token="from-file")
    env = resolve_exec_env(
        "codex", source_env={"OPENAI_API_KEY": "from-env"}, home=str(tmp_path)
    )
    assert env == {"OPENAI_API_KEY": "from-env"}


def test_resolve_exec_env_missing_returns_empty_no_crash(tmp_path):
    assert resolve_exec_env("claude", source_env={}, home=str(tmp_path)) == {}
    assert resolve_exec_env("codex", source_env={}, home=str(tmp_path)) == {}


def test_resolve_exec_env_corrupt_json_returns_empty(tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{not valid json", encoding="utf-8")
    assert resolve_exec_env("codex", source_env={}, home=str(tmp_path)) == {}


def test_resolved_keys_are_subset_of_plan_keys(tmp_path):
    """resolve_exec_env must not surface keys the plan didn't advertise."""
    _write_codex_auth(tmp_path)
    plan = build_plan("codex", source_env={}, home=str(tmp_path))
    env = resolve_exec_env("codex", source_env={}, home=str(tmp_path))
    assert set(env).issubset(set(plan.env_keys))
