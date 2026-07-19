"""Live docker-sandbox wiring, proven OFFLINE with a fake exec (SPEC I3).

No docker, no model, no live auth. A recording fake stands in for the Inspect
sandbox seam so the whole threading path is exercised end to end for a ``claude``
and a ``codex`` trial:

- the harness name resolves to its pinned runner image
  (``claude`` -> ``wololo-evals/claude-runner``, ``codex`` -> ``codex-runner``),
- the container-auth env KEY names land in the exec env with their VALUES
  threaded through to ``sandbox().exec`` but NEVER present on the loggable plan,
- the host credential files attach as read-only host->container bind-mounts,
- the injected ``WOLOLO_FIXTURE_URL`` is rewritten onto ``host.docker.internal``
  (loopback inside a container is the container itself, not the host).

Auth material is injected via ``source_env`` + a fake ``home`` so the test is
hermetic: it never reads the operator's real tokens or credential files.
"""
from __future__ import annotations

import json

import pytest

from wololo_evals.harness_solver import HarnessTrialConfig, run_harness_trial
from wololo_evals.harnesses import resolve_runner
from wololo_evals.inspect_adapter import (
    ADD_HOST,
    ContainerTrialWiring,
    container_fixture_url,
    wire_container_trial,
)

# Sentinel secrets — only ever set via injected source_env, never a real token.
_CLAUDE_TOKEN = "sk-claude-oauth-SENTINEL-DO-NOT-LOG"
_CODEX_TOKEN = "sk-codex-apikey-SENTINEL-DO-NOT-LOG"


class RecordingSandbox:
    """Sequential fake exec that records the env of every call and returns the
    canned outputs run_harness_trial needs for a baseline trial. Recognises the
    ``claude`` and ``codex`` CLI binaries spliced after ``exec "$@"``."""

    def __init__(self, *, binary: str, turns: int = 4):
        self.binary = binary
        self.turns = turns
        self.calls: list[tuple[list[str], dict]] = []
        self.harness_env: dict | None = None

    @staticmethod
    def _events(n: int) -> str:
        return "\n".join(
            json.dumps({"stage": "post-llm-event", "i": i}) for i in range(n)
        )

    async def exec(self, cmd, env=None, timeout=0):
        env = dict(env or {})
        self.calls.append((list(cmd), env))
        c0 = cmd[0]
        if c0 in ("rm", "mkdir"):
            return (0, "", "")
        if c0 == "git":
            if "clone" in cmd or "checkout" in cmd or "status" in cmd:
                return (0, "", "")
            if "diff" in cmd:
                return (0, "diff --git a/x b/x\n+patched\n", "")
        if c0 == "cat":
            return (0, self._events(self.turns), "")
        if c0 == "sh":
            script, rest = cmd[2], cmd[4:]
            if 'exec "$@"' in script and rest and rest[0] == self.binary:
                self.harness_env = env  # the env the CLI actually receives
                return (0, json.dumps({"result": "done"}), "")
            if 'exec "$@"' in script:
                return (0, "", "")  # acceptance / other exec passthrough
            if "printf" in script:
                return (0, "", "")  # skill-install or trace write
        raise AssertionError(f"unexpected command: {cmd}")


def _run_baseline(harness: str, wiring: ContainerTrialWiring):
    runner = resolve_runner(harness, smoke=True)
    cfg = HarnessTrialConfig(
        runner=runner,
        task_prompt="do the task",
        fixture_repo="/srv/fixtures/x.git",
        fixture_ref="deadbeef",
        arm="baseline",
        max_turns=8,
        wall_clock_cap_s=120,
    )
    return run_harness_trial(wiring.exec_factory(), cfg)


# --- container_fixture_url: loopback -> host.docker.internal -----------------
@pytest.mark.parametrize(
    "base, page, expected",
    [
        ("http://127.0.0.1:54123", "native-select.html",
         "http://host.docker.internal:54123/native-select.html"),
        # empty page preserves the base path as given (trailing slash kept)
        ("http://localhost:8080/", "", "http://host.docker.internal:8080/"),
        ("http://localhost:8080", "", "http://host.docker.internal:8080"),
        ("http://0.0.0.0:9000", "a/b.html",
         "http://host.docker.internal:9000/a/b.html"),
        # a non-loopback host is already routable and left untouched
        ("http://10.1.2.3:7000", "p.html", "http://10.1.2.3:7000/p.html"),
    ],
)
def test_container_fixture_url_rewrites_loopback(base, page, expected):
    assert container_fixture_url(base, page) == expected


# --- the full wiring, for a claude and a codex trial ------------------------
async def test_claude_trial_wiring(tmp_path):
    home = tmp_path / "home"
    cred = home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    cred.write_text('{"token": "unused-by-mount"}')

    fake = RecordingSandbox(binary="claude")
    wiring = wire_container_trial(
        "claude",
        "http://127.0.0.1:51000",
        "native-select.html",
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN},
        home=str(home),
        base_exec_factory=lambda: fake.exec,
    )

    # 1. resolved image + engine
    assert wiring.image == "wololo-evals/claude-runner"
    assert wiring.engine_id == "claude"

    # 2. loggable plan: KEY name present, VALUE absent from anything loggable
    assert wiring.plan.env_keys == ("CLAUDE_CODE_OAUTH_TOKEN",)
    assert wiring.plan.satisfied
    assert _CLAUDE_TOKEN not in repr(wiring.plan)
    assert _CLAUDE_TOKEN not in repr(wiring)

    # 3. read-only credential bind-mount, right host->container paths
    assert len(wiring.mounts) == 1
    mount = wiring.mounts[0]
    assert mount.host_path == str(cred)
    assert mount.container_path == "/root/.claude/.credentials.json"
    assert mount.read_only is True

    # 4. fixture URL rewritten onto host.docker.internal + add-host value
    assert wiring.fixture_url == (
        "http://host.docker.internal:51000/native-select.html"
    )
    assert wiring.add_host == "host.docker.internal:host-gateway"
    assert ADD_HOST == "host.docker.internal:host-gateway"

    # 5. drive a real trial through the wired seam: the CLI receives the auth
    #    VALUE + fixture URL + profile env at exec time (never logged).
    result = await _run_baseline("claude", wiring)
    assert result.exit_code == 0
    env = fake.harness_env
    assert env is not None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _CLAUDE_TOKEN
    assert env["WOLOLO_FIXTURE_URL"] == wiring.fixture_url
    assert env["CLAUDE_CONFIG_DIR"] == "/profile"
    assert env["WOLOLO_TRANSCRIPT_DIR"] == "/transcript"


async def test_codex_trial_wiring(tmp_path):
    home = tmp_path / "home"
    cred = home / ".codex" / "auth.json"
    cred.parent.mkdir(parents=True)
    # env token present, so the plan advertises OPENAI_API_KEY from the env;
    # the file existence drives the read-only mount independently.
    cred.write_text('{"tokens": {"access": "unused-by-mount"}}')

    fake = RecordingSandbox(binary="codex")
    wiring = wire_container_trial(
        "codex",
        "http://127.0.0.1:52000",
        "portal-menu.html",
        source_env={"OPENAI_API_KEY": _CODEX_TOKEN},
        home=str(home),
        base_exec_factory=lambda: fake.exec,
    )

    assert wiring.image == "wololo-evals/codex-runner"
    assert wiring.engine_id == "codex"
    assert wiring.plan.env_keys == ("OPENAI_API_KEY",)
    assert wiring.plan.satisfied
    assert _CODEX_TOKEN not in repr(wiring.plan)

    assert len(wiring.mounts) == 1
    assert wiring.mounts[0].host_path == str(cred)
    assert wiring.mounts[0].container_path == "/root/.codex/auth.json"
    assert wiring.mounts[0].read_only is True

    assert wiring.fixture_url == (
        "http://host.docker.internal:52000/portal-menu.html"
    )

    result = await _run_baseline("codex", wiring)
    assert result.exit_code == 0
    env = fake.harness_env
    assert env is not None
    assert env["OPENAI_API_KEY"] == _CODEX_TOKEN
    assert env["WOLOLO_FIXTURE_URL"] == wiring.fixture_url
    assert env["CODEX_HOME"] == "/profile"


# --- unsatisfied auth: no crash, operator-gate surfaced ---------------------
async def test_missing_auth_yields_operator_precondition(tmp_path):
    """No token env and no credential file: build_plan reports not-satisfied
    with an operator marker rather than raising — the wiring still resolves so
    the gate can be logged, but a live run must not proceed."""
    wiring = wire_container_trial(
        "claude",
        "http://127.0.0.1:53000",
        source_env={},                 # no CLAUDE token
        home=str(tmp_path / "empty"),  # no ~/.claude/.credentials.json
        base_exec_factory=lambda: RecordingSandbox(binary="claude").exec,
    )
    assert wiring.image == "wololo-evals/claude-runner"
    assert wiring.plan.satisfied is False
    assert wiring.plan.env_keys == ()
    assert wiring.mounts == ()
    assert wiring.plan.operator_preconditions  # non-empty operator gate


def test_unknown_harness_has_no_engine_mapping():
    with pytest.raises(ValueError):
        wire_container_trial("hermes", "http://127.0.0.1:5000")
