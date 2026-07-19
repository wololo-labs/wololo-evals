# Wave-1 docker-sandbox — binding contracts (Images phase)

Binding interface + ownership map for the three parallel **Images-phase**
builders (`claude-runner`, `codex-runner`, `sandbox-wiring`). Read this doc and
`.agents/research/harness-seam.md` first. Each builder touches **only** its
listed paths; the others are owned by a concurrent builder.

The Contracts phase (this doc) also lands `src/wololo_evals/engines/container_auth.py`
— the pure, secret-free auth-plan helper the wiring builder consumes. Its exact
public API is fixed below; treat it as frozen.

---

## NO-LIVE-CALL rule (restated — applies to every builder)

**No live model calls anywhere in this wave.** This wave builds and wires the
plumbing and proves it offline. The real model trial is a separate,
operator-gated step. You may verify only up to:

- the engine CLI runs `--version` **inside its container** (no auth, no model),
- the wiring selects the right per-engine image, constructs the auth env
  (KEY names present, values never logged), attaches the mounts, and injects
  the fixture URL — all over a **fake exec seam** (no docker, no model),
- the fixture server is reachable from inside a container (a `curl` of a static
  page, no model).

Never invoke `claude` / `codex` against a model. Do **not** run interactive auth
(`claude setup-token`, `codex login`, any browser). If a step would need
interactive auth or spend, STOP and record it under **Operator preconditions**
at the bottom of this doc rather than faking or forcing it.

The 304-passing / 2-skipped suite (`uv run pytest -q`, needs local Postgres)
stays green at every step. `inspect_ai` stays imported **only** in
`inspect_adapter.py`.

---

## Frozen dependency: `engines/container_auth.py` (landed this phase)

Pure, stdlib-only, secret-free. Two public functions + two public dataclasses.
The **plan is loggable; values are not** — they are two different calls by
design.

```python
from wololo_evals.engines.container_auth import (
    BindMount, ContainerAuthPlan, SUPPORTED_ENGINES,   # ("claude","codex","copilot")
    build_plan, resolve_exec_env,
)

# 1. Build the loggable plan (reads token PRESENCE + file EXISTENCE only):
plan = build_plan("claude", source_env=None, home=None)   # defaults: os.environ, ~
#   plan.engine_id: str
#   plan.env_keys:  tuple[str, ...]          # KEY names only, never values
#   plan.mounts:    tuple[BindMount, ...]    # host_path, container_path, read_only=True
#   plan.satisfied: bool                     # False => operator must act first
#   plan.operator_preconditions: tuple[str, ...]   # marker when not satisfied

# 2. At EXEC time only, resolve actual values for sandbox().exec(env=...):
env_values = resolve_exec_env("claude", source_env=None, home=None)  # {KEY: VALUE}
```

Guarantees the wiring relies on:

- `build_plan` **never raises for missing auth** — it returns
  `satisfied=False` + a `operator_preconditions` marker. It raises `ValueError`
  only for an unknown engine id.
- `ContainerAuthPlan` carries **no secret value and no reference to the source
  env**; `repr(plan)` / `str(plan)` are safe to log.
- `resolve_exec_env` is the **only** place secret values are read. Its result
  goes straight into `sandbox().exec(env=...)` and **must never be logged**.
  Its keys are always a subset of `plan.env_keys`. It never raises for a
  missing/corrupt credential file (unresolved key is simply absent).

Per-engine surface (data in `_AUTH_SPECS`, not re-derived by builders):

| engine | env token KEY(s) (first present wins) | read-only mount (host → container) | JSON-file token fallback |
| --- | --- | --- | --- |
| `claude` | `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` | `~/.claude/.credentials.json` → `/root/.claude/.credentials.json` | none (CLI reads the mounted file) |
| `codex` | `OPENAI_API_KEY`, `CODEX_AUTH_TOKEN` | `~/.codex/auth.json` → `/root/.codex/auth.json` | `OPENAI_API_KEY` parsed from `~/.codex/auth.json` |

A mount is added **only when the host file exists** (you cannot bind-mount a
missing file). `read_only` is always `True`.

---

## Images-phase components

### I1 — `claude-runner` image

**Owns:** `images/claude-runner/Dockerfile`, `images/claude-runner/entrypoint.sh`,
`tests/test_image_claude_runner.py`.

**Image contract:**
- **Base:** a slim Node base (Claude Code is an npm CLI). Match the host CLI's
  install shape — inspect the `claude` already installed on the host, or use the
  official npm package (`@anthropic-ai/claude-code`). Pin a version consistent
  with `ClaudeAdapter.default_version_range` (`>=1.0.0,<3.0.0`).
- **CLI:** `claude`, invokable headless (`claude -p … --output-format stream-json
  --dangerously-skip-permissions`, per `engines/claude.py`).
- **Entrypoint (`entrypoint.sh`):** runs the harness command headless and
  honours the seam env (harness-seam.md §3):
  - `{profile_env}` for claude is `CLAUDE_CONFIG_DIR` — the CLI reads its
    config/skills from there (the in-sandbox `/profile`), **never** host `$HOME`.
    The treatment-arm skill is materialised under
    `/profile/{skills_subdir}/{skill_slug}/…`.
  - `WOLOLO_TRANSCRIPT_DIR` (always `/transcript` in-sandbox) — fleet-hooks JSONL
    events are written to `$WOLOLO_TRANSCRIPT_DIR/events.jsonl`.
  - Leaves edits **uncommitted** as a git diff in `/workspace`; exit code
    reflects setup/loop success only (0 = the harness loop completed, not "task
    solved").
- **NO secrets baked into any image layer.** Auth arrives at run time via
  `container_auth` env + read-only mounts (below), never `COPY`/`ENV` of a token.

**Offline verification (must pass):**
- `docker build -t wololo-evals/claude-runner images/claude-runner`
- `docker run --rm wololo-evals/claude-runner claude --version` prints a version
  (no model call, no auth needed for `--version`).
- `docker history wololo-evals/claude-runner | grep -i token` is empty (no baked
  secret).
- `tests/test_image_claude_runner.py` shells out to the two commands above,
  skip-marked when docker is absent (docker IS present here).

### I2 — `codex-runner` image

**Owns:** `images/codex-runner/Dockerfile`, `images/codex-runner/entrypoint.sh`,
`tests/test_image_codex_runner.py`.

Same shape as I1 but for the **Codex CLI**:
- **Install:** determine the correct install for codex ~`0.141.x` (npm
  `@openai/codex` or the release binary). Pin consistent with
  `CodexAdapter.default_version_range` (`>=0.5.0,<2.0.0`).
- **CLI:** `codex`, invokable headless (`codex exec --json --skip-git-repo-check
  …`, per `engines/codex.py`).
- **Entrypoint:** honours `{profile_env}` for codex and `WOLOLO_TRANSCRIPT_DIR`,
  headless, uncommitted git diff in `/workspace`, no baked secrets.

**Offline verification:** `docker build -t wololo-evals/codex-runner …`;
`docker run --rm wololo-evals/codex-runner codex --version` prints a version;
`docker history … | grep -i token` empty; `tests/test_image_codex_runner.py`
green (skip-guarded on docker absence).

### I3 — sandbox wiring (image selection + auth/fixture threading)

**Owns (additive only):** `src/wololo_evals/harnesses/__init__.py`
(map each engine harness → its runner image name),
`src/wololo_evals/inspect_adapter.py` (thread `container_auth` env + mounts +
fixture URL into the sandbox config / exec env; keep `sandbox_type` default and
all existing behaviour), `tests/test_sandbox_wiring.py`.

**How the harness selects its per-engine image.** Add an image map beside the
`_RUNNERS` table (harness-seam.md §4/§7), e.g.:

```python
_RUNNER_IMAGES = {
    "claude-code": "wololo-evals/claude-runner",
    "codex":       "wololo-evals/codex-runner",
    # existing harnesses keep their current behaviour unchanged
}
```

Resolution is by the harness name already carried on the decrypted candidate
payload — no matrix-code change, mirroring the existing `_RUNNERS` pattern.
Existing harnesses (claude wave-1, `hermes`) and all suites stay unchanged and
green.

**How `container_auth` env/mounts thread through `sandbox().exec`.** The
production seam is `inspect_adapter._sandbox_exec` (harness-seam.md §5). Thread
auth without ever logging a value:

1. At trial setup, `plan = build_plan(engine_id)`. Log `plan` (safe:
   `env_keys` + mount paths + `satisfied`). If `not plan.satisfied`, surface
   `plan.operator_preconditions` as an operator gate — **do not** attempt a live
   run (and in this wave, do not attempt one regardless).
2. Attach `plan.mounts` to the sandbox as **read-only** bind-mounts
   (`host_path → container_path`, `read_only=True`).
3. At exec time only, build the exec env as
   `{**resolve_exec_env(engine_id), profile_env: "/profile",
   "WOLOLO_TRANSCRIPT_DIR": "/transcript", "WOLOLO_FIXTURE_URL": <url>}` and pass
   it to `sandbox().exec(env=…)`. The `resolve_exec_env(...)` result is **never
   logged**.

The `exec_factory` injection point already exists (`effective_sandbox = None if
exec_factory is not None else sandbox_type`), so the fake-exec test drives this
whole path with no docker and no model.

**How the fixture server is reached from inside a container.** The browser
fixtures are served on the host by `engines/browser_fixture_server.py`
(`127.0.0.1:0`, ephemeral port, loopback). A container reaches the host via the
Docker gateway alias `host.docker.internal`:

- Run the container with `--add-host=host.docker.internal:host-gateway` (Linux;
  built-in on Docker Desktop).
- Inject the reachable URL into the container as an env var, e.g.
  `WOLOLO_FIXTURE_URL=http://host.docker.internal:<host_port>/<page>` — built
  from the host server's bound port, not `127.0.0.1` (loopback inside the
  container is the container itself, not the host).

**Offline verification (must pass) — FAKE exec, no docker, no model:**
`tests/test_sandbox_wiring.py` asserts, for a `claude` and a `codex` trial:
- the resolved image name (`claude` → `wololo-evals/claude-runner`, `codex` →
  `wololo-evals/codex-runner`),
- the constructed exec env has the expected KEY names present (from
  `plan.env_keys`) and **values are not logged**,
- the read-only mounts are attached with the right host→container paths,
- the injected `WOLOLO_FIXTURE_URL` uses `host.docker.internal`.
- existing `test_harness_uplift` + suite tests untouched and green; `grep`
  confirms `inspect_ai` is imported only in `inspect_adapter.py`; full suite
  green.

---

## Integrate phase (for reference — not an Images owner)

**Owns:** `tests/test_fixture_from_container.py`, `docs/RUNBOOK-live-smoke.md`,
plus glue fixes. Proves (real, no model) that `BrowserFixtureServer` on the host
is reachable from a container via
`docker run --rm --add-host=host.docker.internal:host-gateway <slim+curl> curl
-s http://host.docker.internal:<port>/<page>` (skip-guarded if `--add-host` is
unsupported). Authors the exact operator command sequence for the later
operator-gated live smoke.

---

## Operator preconditions (record here; do NOT perform)

These need a human before any live run — none are done in this wave:

- **Interactive auth is not run here.** A live claude run needs either
  `CLAUDE_CODE_OAUTH_TOKEN` set (via `claude setup-token`) **or**
  `~/.claude/.credentials.json` present on the host for the read-only mount. A
  live codex run needs `OPENAI_API_KEY` set (or `codex login`) **or**
  `~/.codex/auth.json` present. `build_plan` reports which is missing via
  `plan.satisfied` / `plan.operator_preconditions`; provisioning is an operator
  step.
- **Model spend is operator-gated.** The `~$2` live smoke and the `~$30` dogfood
  A/B are separate, human-approved steps (see `docs/RUNBOOK-live-smoke.md` once
  authored). Nothing in this wave spends.
- **Docker `--add-host=host.docker.internal:host-gateway`** must be supported by
  the operator's Docker for the fixture-from-container path (built-in on Docker
  Desktop; `host-gateway` on Linux Docker ≥ 20.10). Proven reachable here by
  `tests/test_fixture_from_container.py` (real container `curl`, no model).
- **`build_plan` presence-optimism vs. usable auth (Integrate-phase finding).**
  On the reference host `build_plan` returns `satisfied=True` for both engines
  (a credential *file* exists), but `resolve_exec_env` returns **no usable
  API-key env** for either — both logins are ChatGPT/OAuth (`~/.codex/auth.json`
  has a `null` `OPENAI_API_KEY` + OAuth `tokens`; claude has only the mounted
  `.credentials.json`). Additionally the images set `CLAUDE_CONFIG_DIR` /
  `CODEX_HOME` to `/profile`, while the `container_auth` mounts target
  `/root/.claude` / `/root/.codex` — a location mismatch. So a live run needs an
  operator to first provision a working path: claude via
  `export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)` (or mount the cred to
  `/profile/.credentials.json`); codex via mounting `auth.json` to
  `/profile/auth.json` (reuse the ChatGPT login) or a real `OPENAI_API_KEY`. Exact
  commands are in `docs/RUNBOOK-live-smoke.md` §2.
- **fleet-hooks `events.jsonl` emission is out-of-repo integration debt.** The
  runner images ship the vanilla CLIs only; the `post-llm-event` JSONL stream
  (the sole `count_turns()` source) is a fleet-hooks concern behind the manifest
  liveness claim (`docs/INTEGRATION-DEBT.md`). The live smoke will show whether
  it is wired for these images; if the turn-count is `0`, that is the known
  remaining debt to close before a verdict-bearing run (auth + model-turn +
  git-diff are still proven). See `docs/RUNBOOK-live-smoke.md` §7.
