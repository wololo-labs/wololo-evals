"""Engine adapter pillar (README §6, §14), exercised with no live CLI.

Every subprocess goes through an injected fake exec seam — the adapters never
spawn ``claude``/``codex``/``copilot`` here — mirroring how ``test_harness_uplift``
runs the wave-1 bridge over a fake ``SandboxExec``. Three layers, matching the
A1 contract in ``docs/plans/wave-contracts.md``:

1. **pure** — ``build_command`` argv shape per engine, the version-range
   grammar (``check_range``/``satisfies``), and env minimisation (§6.3);
2. **adapter over a fake exec seam** (no DB) — the §14.2 status taxonomy, the
   single automatic ``infra_fail`` retry, the per-run cost cap, version-drift
   and model-allow-list refusals, capture-file writing, and trace hand-off;
3. **replay end-to-end** — the committed ``engines/testdata/example_run``
   archive re-emitted byte-stably with zero spend.
"""
from __future__ import annotations

import collections
import pathlib

import pytest

import wololo_evals.engines as engines_pkg
from wololo_evals.core.interfaces import (
    ADVISORY_DIR,
    ENGINE_IDS,
    AssetRef,
    AssetSnapshot,
    CaseSpec,
    RunCaps,
    RunResult,
    RunSpec,
    TraceEvent,
)
from wololo_evals.engines import (
    ClaudeAdapter,
    CodexAdapter,
    CopilotAdapter,
    ReplayAdapter,
    build_env,
    get_adapter,
)
from wololo_evals.engines.exec_seam import ExecResult
from wololo_evals.engines.registry import ADAPTERS
from wololo_evals.engines.version import EngineVersionError, check_range, parse_version, satisfies

FIXTURE_ARCHIVE = (
    pathlib.Path(engines_pkg.__file__).resolve().parent / "testdata" / "example_run"
)

_CLAUDE_OK_STDOUT = (
    '{"type":"system","subtype":"init"}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    '{"type":"result","subtype":"success","total_cost_usd":0.0123,'
    '"usage":{"input_tokens":128,"output_tokens":47}}\n'
)


# --------------------------------------------------------------------------- #
# fakes + builders
# --------------------------------------------------------------------------- #
class FakeExec:
    """Scripted synchronous exec seam. ``--version`` calls return
    ``version_out``; each non-version call pops the next queued result (or a
    generic exit-0 with empty output when the queue is empty)."""

    def __init__(self, version_out: str = "claude 2.1.0 (Claude Code)"):
        self.version_out = version_out
        self.run_queue: collections.deque[ExecResult] = collections.deque()
        self.calls: list[dict] = []

    def enqueue(self, *results: ExecResult) -> "FakeExec":
        self.run_queue.extend(results)
        return self

    def __call__(self, argv, *, env, cwd, timeout=None) -> ExecResult:
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd, "timeout": timeout})
        if "--version" in argv:
            return ExecResult(0, self.version_out, "")
        if self.run_queue:
            return self.run_queue.popleft()
        return ExecResult(0, _CLAUDE_OK_STDOUT, "")

    @property
    def run_calls(self) -> list[dict]:
        return [c for c in self.calls if "--version" not in c["argv"]]


class FakeNormaliser:
    engine_id = "claude"

    def __init__(self):
        self.seen: list[pathlib.Path] = []

    def normalise(self, raw_event_path: pathlib.Path) -> list[TraceEvent]:
        self.seen.append(raw_event_path)
        return [
            TraceEvent(seq=0, ts_ms=0, kind="tool_call", name="read", target="README.md"),
            TraceEvent(seq=1, ts_ms=5, kind="message", name="assistant"),
        ]


def _asset(files=None) -> AssetSnapshot:
    return AssetSnapshot(
        ref=AssetRef(kind="agent_skill", id="dropdowns", inline_body="body"),
        body="body",
        files=files or {},
        adapter_id="local",
        snapshot_digest="deadbeef",
    )


def _case(**kw) -> CaseSpec:
    base = dict(case_id="case-hello", input_text="open the dropdown")
    base.update(kw)
    return CaseSpec(**base)


def _spec(tmp: pathlib.Path, *, engine_id="claude", model_id="claude-opus-4-6", **kw) -> RunSpec:
    workspace = tmp / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    fields = dict(
        run_id="run-1",
        asset=kw.pop("asset", _asset()),
        case=kw.pop("case", _case()),
        engine_id=engine_id,
        model_id=model_id,
        prompt_id="p0",
        seed=kw.pop("seed", 0),
        cwd=str(workspace),
    )
    fields.update(kw)
    return RunSpec(**fields)


# --------------------------------------------------------------------------- #
# Layer 1 — pure: version grammar
# --------------------------------------------------------------------------- #
def test_parse_version_extracts_core_from_noisy_string():
    assert parse_version("claude 1.4.2 (Claude Code)") == (1, 4, 2)
    assert parse_version("codex-cli v0.9") == (0, 9, 0)
    with pytest.raises(EngineVersionError):
        parse_version("no numbers here")


def test_satisfies_comparators_and_wildcards():
    assert satisfies("1.4.2", ">=1.0.0,<3.0.0")
    assert not satisfies("3.0.0", ">=1.0.0,<3.0.0")
    assert satisfies("2.5.0", "*")
    assert satisfies("2.5.0", "")
    assert satisfies("1.2.9", "~=1.2.3")  # >=1.2.3,<1.3.0
    assert not satisfies("1.3.0", "~=1.2.3")
    assert satisfies("1.9.0", "~=1.2")  # >=1.2,<2.0
    assert not satisfies("2.0.0", "~=1.2")


def test_check_range_refuses_out_of_range_unless_drift():
    with pytest.raises(EngineVersionError):
        check_range("9.9.9", ">=1.0.0,<3.0.0", allow_drift=False)
    # allow_drift converts the refusal into a no-op
    check_range("9.9.9", ">=1.0.0,<3.0.0", allow_drift=True)
    check_range("1.4.2", ">=1.0.0,<3.0.0", allow_drift=False)


# --------------------------------------------------------------------------- #
# Layer 1 — pure: build_command per engine
# --------------------------------------------------------------------------- #
def test_build_command_shapes(tmp_path):
    spec = _spec(tmp_path)
    claude_argv = list(ClaudeAdapter().build_command(spec))
    assert claude_argv[0] == "claude" and "-p" in claude_argv
    assert "open the dropdown" in claude_argv
    assert claude_argv[claude_argv.index("--model") + 1] == "claude-opus-4-6"
    assert "stream-json" in claude_argv

    codex_argv = list(CodexAdapter().build_command(_spec(tmp_path, engine_id="codex", model_id="gpt-5.4")))
    assert codex_argv[:3] == ["codex", "exec", "--json"]
    assert "gpt-5.4" in codex_argv

    copilot_argv = list(
        CopilotAdapter().build_command(_spec(tmp_path, engine_id="copilot", model_id="gpt-4.1"))
    )
    assert copilot_argv[0] == "copilot" and "gpt-4.1" in copilot_argv


def test_multi_turn_prompt_composition(tmp_path):
    case = _case(
        input_text="",
        turns=({"role": "user", "content": "first"}, {"role": "user", "content": "second"}),
    )
    argv = list(ClaudeAdapter().build_command(_spec(tmp_path, case=case)))
    prompt = argv[argv.index("-p") + 1]
    assert "first" in prompt and "second" in prompt


# --------------------------------------------------------------------------- #
# Layer 1 — pure: env minimisation (§6.3, §14.1)
# --------------------------------------------------------------------------- #
def test_build_env_keeps_only_own_token_strips_the_rest():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
        "OPENAI_API_KEY": "openai-secret",
        "COPILOT_GITHUB_TOKEN": "copilot-secret",
        "SAML_GITHUB_TOKEN": "saml-secret",
        "GH_AW_FIXTURE_GITHUB_TOKEN": "fixture-secret",
        "GITHUB_TOKEN": "actions-secret",
    }
    env = build_env("claude", {}, source_env=source)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-secret"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/agent"
    for forbidden in (
        "OPENAI_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "SAML_GITHUB_TOKEN",
        "GH_AW_FIXTURE_GITHUB_TOKEN",
        "GITHUB_TOKEN",
    ):
        assert forbidden not in env


def test_build_env_keeps_the_identity_a_credential_lookup_needs():
    # Dropping USER does not fail loudly: the CLI starts, finds no stored
    # credential and reports "Not logged in", which reads as a missing token
    # rather than a missing username.
    env = build_env(
        "claude", {}, source_env={"PATH": "/usr/bin", "HOME": "/h", "USER": "agent"}
    )
    assert env["USER"] == "agent"


def test_build_env_declared_run_env_cannot_reintroduce_forbidden_tokens():
    env = build_env(
        "codex",
        {"MY_FLAG": "1", "GITHUB_TOKEN": "smuggled", "CLAUDE_CODE_OAUTH_TOKEN": "smuggled"},
        source_env={"CODEX_AUTH_TOKEN": "codex-secret"},
    )
    assert env["MY_FLAG"] == "1"
    assert env["CODEX_AUTH_TOKEN"] == "codex-secret"
    assert "GITHUB_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # other engine's token stripped


def test_codex_engine_selects_openai_key_fallback():
    env = build_env("codex", {}, source_env={"OPENAI_API_KEY": "oai"})
    assert env["OPENAI_API_KEY"] == "oai"
    assert "CODEX_AUTH_TOKEN" not in env


# --------------------------------------------------------------------------- #
# Layer 2 — adapter over the fake exec seam (no DB)
# --------------------------------------------------------------------------- #
def test_run_ok_writes_capture_files_and_parses_usage(tmp_path):
    fake = FakeExec().enqueue(ExecResult(0, _CLAUDE_OK_STDOUT, ""))
    adapter = ClaudeAdapter(exec_fn=fake)
    spec = _spec(tmp_path)
    result = adapter.run(spec)

    assert isinstance(result, RunResult)
    assert result.status == "ok" and result.scored
    assert result.cost_usd == pytest.approx(0.0123)
    assert (result.tokens_in, result.tokens_out) == (128, 47)
    assert result.attempts == 1
    assert result.metadata["cli_version"].startswith("claude 2.1.0")

    adv = pathlib.Path(spec.cwd) / ADVISORY_DIR
    assert (adv / "claude.raw.jsonl").read_text() == _CLAUDE_OK_STDOUT
    assert (adv / "claude.stdout.log").exists()
    assert (adv / "claude.stderr.log").exists()
    assert (adv / "prompt.md").read_text() == "open the dropdown"
    assert adapter.raw_event_path(spec) == adv / "claude.raw.jsonl"


def test_run_minimises_env_no_foreign_tokens_reach_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-secret")
    fake = FakeExec()
    ClaudeAdapter(exec_fn=fake).run(_spec(tmp_path))
    run_env = fake.run_calls[0]["env"]
    assert run_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "claude-secret"
    assert "OPENAI_API_KEY" not in run_env
    assert "GITHUB_TOKEN" not in run_env


def test_a_declared_preamble_leads_the_case_prompt(tmp_path):
    # The preamble is how an arm says whether it is scoring the asset's
    # contents or also whether the model finds it. Without it the prompt is
    # the case text alone.
    fake = FakeExec()
    spec = _spec(tmp_path, extra={"prompt_preamble": "Use the test-writer-fixer skill."})
    ClaudeAdapter(exec_fn=fake).run(spec)
    prompt = fake.run_calls[0]["argv"][2]
    assert prompt.startswith("Use the test-writer-fixer skill.")
    assert prompt.rstrip().endswith("open the dropdown")

    plain = FakeExec()
    ClaudeAdapter(exec_fn=plain).run(_spec(tmp_path))
    assert plain.run_calls[0]["argv"][2] == "open the dropdown"


def test_claude_refuses_a_cli_older_than_the_flag_it_always_passes(tmp_path):
    # --setting-sources is passed on every run, and the version floor admitted
    # any 1.x release. The oldest the changelog evidences working for this use
    # is 2.0.24 ("project-level skills were not loading when --setting-sources
    # 'project' was specified" was fixed there); the release that introduced
    # the flag is not recorded, so the floor is the earliest documented good one.
    for too_old in ("claude 1.0.60 (Claude Code)", "claude 2.0.23 (Claude Code)"):
        with pytest.raises(EngineVersionError):
            ClaudeAdapter(exec_fn=FakeExec(version_out=too_old)).run(_spec(tmp_path))
    ClaudeAdapter(exec_fn=FakeExec(version_out="claude 2.0.24 (Claude Code)")).run(_spec(tmp_path))


def test_claude_runs_on_project_settings_only(tmp_path):
    # Without this the trial inherits the operator's own skills and memory, so
    # it measures their installation and the asset competes with hundreds of
    # unrelated skills for the model's attention.
    fake = FakeExec()
    spec = _spec(tmp_path)
    ClaudeAdapter(exec_fn=fake).run(spec)
    argv = fake.run_calls[0]["argv"]
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"


def test_run_mounts_asset_files_with_traversal_guard(tmp_path):
    fake = FakeExec()
    spec = _spec(tmp_path, asset=_asset(files={"dropdowns/SKILL.md": "# skill"}))
    ClaudeAdapter(exec_fn=fake).run(spec)
    # a skill lands where the CLI looks for skills, not at the workspace root
    mounted = pathlib.Path(spec.cwd) / ".claude" / "skills" / "dropdowns" / "SKILL.md"
    assert mounted.read_text() == "# skill"

    bad = _spec(tmp_path, asset=_asset(files={"../escape.md": "x"}))
    with pytest.raises(ValueError):
        ClaudeAdapter(exec_fn=fake).run(bad)


def test_an_adapter_retry_starts_from_a_pristine_workspace(tmp_path):
    # The adapter retries an infra_fail once by itself. That retry ran in the
    # directory the failed attempt had written to.
    import shutil

    calls = {"n": 0}
    seen = []

    def exec_fn(argv, *, env, cwd, timeout=None):
        if "--version" in argv:
            return ExecResult(0, "claude 2.1.0 (Claude Code)", "")
        calls["n"] += 1
        root = pathlib.Path(cwd)
        seen.append(
            {
                "stray": (root / "stray.txt").exists(),
                "skill": (root / ".claude" / "skills" / "dropdowns" / "SKILL.md").is_file(),
            }
        )
        if calls["n"] == 1:
            (root / "stray.txt").write_text("left by the failed attempt")
            return ExecResult(1, "", "boom")
        return ExecResult(0, _CLAUDE_OK_STDOUT, "")

    def wipe(spec):
        for child in pathlib.Path(spec.cwd).iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    adapter = ClaudeAdapter(exec_fn=exec_fn)
    adapter.reset_workspace = wipe
    adapter.run(_spec(tmp_path, asset=_asset(files={"dropdowns/SKILL.md": "# skill"})))

    assert len(seen) == 2, "expected one retry"
    assert not seen[1]["stray"], "the retry saw the failed attempt's leftovers"
    assert seen[1]["skill"], "the retry lost the mounted skill"


def test_infra_fail_is_retried_once_then_succeeds(tmp_path):
    fake = FakeExec().enqueue(
        ExecResult(1, "", "boom"),  # first attempt crashes
        ExecResult(0, _CLAUDE_OK_STDOUT, ""),  # retry succeeds
    )
    result = ClaudeAdapter(exec_fn=fake).run(_spec(tmp_path))
    assert result.status == "ok"
    assert result.attempts == 2
    assert len(fake.run_calls) == 2


def test_infra_fail_twice_stays_infra_fail(tmp_path):
    fake = FakeExec().enqueue(ExecResult(1, "", "boom"), ExecResult(1, "", "boom again"))
    result = ClaudeAdapter(exec_fn=fake).run(_spec(tmp_path))
    assert result.status == "infra_fail"
    assert result.attempts == 2
    assert not result.scored
    assert result.error


def test_timeout_status_not_retried(tmp_path):
    fake = FakeExec().enqueue(ExecResult(124, "", "", timed_out=True))
    spec = _spec(tmp_path, caps=RunCaps(wall_time_seconds=1))
    result = ClaudeAdapter(exec_fn=fake).run(spec)
    assert result.status == "timeout"
    assert result.attempts == 1
    assert fake.run_calls[0]["timeout"] == 1.0


def test_exit_zero_empty_output_is_model_fail(tmp_path):
    fake = FakeExec().enqueue(ExecResult(0, "   \n", ""))
    result = ClaudeAdapter(exec_fn=fake).run(_spec(tmp_path))
    assert result.status == "model_fail"
    assert result.cost_usd == 0.0


def test_per_run_cost_cap_downgrades_ok_run(tmp_path):
    fake = FakeExec().enqueue(ExecResult(0, _CLAUDE_OK_STDOUT, ""))
    spec = _spec(tmp_path, caps=RunCaps(max_cost_usd=0.001))  # stream reports 0.0123
    result = ClaudeAdapter(exec_fn=fake).run(spec)
    assert result.status == "cost_cap_hit"


def test_version_drift_refused_then_allowed(tmp_path):
    drifted = FakeExec(version_out="claude 9.9.9")
    with pytest.raises(EngineVersionError):
        ClaudeAdapter(exec_fn=drifted).run(_spec(tmp_path))
    # same drift, but the run opts into --allow-engine-drift
    ok = ClaudeAdapter(exec_fn=FakeExec(version_out="claude 9.9.9")).run(
        _spec(tmp_path, extra={"allow_engine_drift": True})
    )
    assert ok.status == "ok"


def test_version_range_override_from_spec_extra(tmp_path):
    fake = FakeExec(version_out="claude 2.5.0")
    # 2.5.0 is inside the default range, but this run pins a tighter one
    with pytest.raises(EngineVersionError):
        ClaudeAdapter(exec_fn=fake).run(_spec(tmp_path, extra={"version_range": ">=1.0.0,<2.0.0"}))


def test_model_outside_allow_list_refused(tmp_path):
    fake = FakeExec()
    with pytest.raises(ValueError):
        ClaudeAdapter(exec_fn=fake).run(_spec(tmp_path, model_id="gpt-4.1"))


def test_normaliser_hand_off_writes_trace_jsonl(tmp_path):
    fake = FakeExec().enqueue(ExecResult(0, _CLAUDE_OK_STDOUT, ""))
    norm = FakeNormaliser()
    spec = _spec(tmp_path)
    ClaudeAdapter(exec_fn=fake, normaliser=norm).run(spec)
    trace = (pathlib.Path(spec.cwd) / ADVISORY_DIR / "trace.jsonl").read_text().splitlines()
    assert len(trace) == 2
    assert '"read"' in trace[0]
    assert norm.seen == [adapter_raw(spec)]


def adapter_raw(spec: RunSpec) -> pathlib.Path:
    return pathlib.Path(spec.cwd) / ADVISORY_DIR / "claude.raw.jsonl"


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_covers_every_engine_id():
    assert set(ADAPTERS) == set(ENGINE_IDS)
    assert isinstance(get_adapter("claude"), ClaudeAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)
    assert isinstance(get_adapter("copilot"), CopilotAdapter)
    assert isinstance(get_adapter("replay", runs_dir=str(FIXTURE_ARCHIVE)), ReplayAdapter)
    with pytest.raises(KeyError):
        get_adapter("nope")


# --------------------------------------------------------------------------- #
# Layer 3 — replay end-to-end over the committed archive (zero spend)
# --------------------------------------------------------------------------- #
def test_replay_reemits_archived_log_byte_stable_zero_cost(tmp_path):
    adapter = ReplayAdapter(runs_dir=str(FIXTURE_ARCHIVE))
    spec = _spec(tmp_path, engine_id="replay", model_id="")

    result = adapter.run(spec)
    assert result.status == "ok"
    assert result.cost_usd == 0.0  # zero spend, §14.4
    assert (result.tokens_in, result.tokens_out) == (128, 47)
    # carries the ARCHIVED provenance, not the RunSpec's empty model_id
    assert result.metadata["engine_id"] == "claude"
    assert result.metadata["model_id"] == "claude-opus-4-6"

    archived = (FIXTURE_ARCHIVE / "case-hello" / "seed-0" / "claude.raw.jsonl").read_text()
    emitted = (pathlib.Path(spec.cwd) / ADVISORY_DIR / "claude.raw.jsonl").read_text()
    assert emitted == archived  # byte-stable re-emission

    # second run reproduces identical bytes
    spec2 = _spec(tmp_path / "again", engine_id="replay", model_id="")
    adapter.run(spec2)
    assert (pathlib.Path(spec2.cwd) / ADVISORY_DIR / "claude.raw.jsonl").read_text() == archived


def test_replay_exempt_from_version_and_no_subprocess(tmp_path):
    adapter = ReplayAdapter(runs_dir=str(FIXTURE_ARCHIVE))
    assert adapter.cli_version() == "replay-1.0"
    assert list(adapter.build_command(_spec(tmp_path, engine_id="replay", model_id=""))) == []


def test_replay_normaliser_writes_trace(tmp_path):
    norm = FakeNormaliser()
    adapter = ReplayAdapter(runs_dir=str(FIXTURE_ARCHIVE), normaliser=norm)
    spec = _spec(tmp_path, engine_id="replay", model_id="")
    adapter.run(spec)
    assert (pathlib.Path(spec.cwd) / ADVISORY_DIR / "trace.jsonl").exists()
    assert len(norm.seen) == 1


def test_replay_missing_archive_fails_loudly(tmp_path):
    adapter = ReplayAdapter(runs_dir=str(tmp_path / "empty"))
    spec = _spec(tmp_path, engine_id="replay", model_id="", case=_case(case_id="absent"))
    with pytest.raises(FileNotFoundError):
        adapter.run(spec)


def test_replay_explicit_source_override(tmp_path):
    src = FIXTURE_ARCHIVE / "case-hello" / "seed-0" / "claude.raw.jsonl"
    adapter = ReplayAdapter(runs_dir=str(tmp_path / "unused"))
    spec = _spec(tmp_path, engine_id="replay", model_id="", extra={"replay_source": str(src)})
    result = adapter.run(spec)
    assert result.status == "ok"
    assert result.metadata["engine_id"] == "claude"
