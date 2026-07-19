# RUNBOOK — operator-gated live smoke (~$2, 1 case × claude + codex)

This is the exact command sequence an operator runs to do the **first real model
trial** through the wave-1 docker-sandbox plumbing, once they have approved the
(~$2) spend. Everything up to this point is proven **offline** (no model call);
this runbook is where a live model is invoked for the first time.

> **Spend gate.** Nothing in the repo spends money. Running the steps under
> "Live smoke" below calls a real model twice (once per engine) and costs a
> few dollars. Do not run them until spend is approved.

---

## 1. What is proven offline vs what the live smoke exercises for the first time

| Concern | Proven **offline** (in CI, no model) | First exercised **live** by this runbook |
| --- | --- | --- |
| Engine CLI exists + right version in-container | `tests/test_image_claude_runner.py`, `tests/test_image_codex_runner.py` (`claude/codex --version` inside the image) | — |
| Per-engine image selection, auth-env KEY threading, read-only mounts, `WOLOLO_FIXTURE_URL` rewrite | `tests/test_sandbox_wiring.py` (fake exec, no docker, no model) | — |
| Host fixture server reachable from a container over `host.docker.internal` | `tests/test_fixture_from_container.py` (real container, real `curl`, **no model**) | — |
| `container_auth` plan is secret-free + never raises on missing auth | `tests/test_container_auth.py` | — |
| **Real auth actually authenticates the CLI inside the container** | — | ✅ first time here |
| **The CLI runs a real model turn headless** and exits 0 | — | ✅ first time here |
| **fleet-hooks writes `events.jsonl` with `post-llm-event` lines** during a real run | — | ✅ first time here |
| **The CLI leaves its edits as an uncommitted git diff in `/workspace`** | — | ✅ first time here |

The smoke deliberately uses a **trivial task** against a throwaway git workspace
(not the browser dropdown case) to keep cost ~$2. It proves *plumbing + auth +
trace capture*, **not** browser-driving correctness — that is the separate,
larger (~$30) dogfood A/B (`dropdowns v1 → v2`), which reuses this exact auth +
image + fixture-URL machinery once the smoke is green.

---

## 2. Operator preconditions (MUST do before the live smoke)

These need a human and are **not** performed anywhere in the wave. `build_plan`
reports `satisfied=True` on this host because a *credential file* exists — but
that is presence-optimism. On this host `resolve_exec_env` returns **no usable
API-key env** for either engine (both logins are ChatGPT/OAuth, not API keys),
and the images set `CLAUDE_CONFIG_DIR` / `CODEX_HOME` to `/profile` while the
`container_auth` mounts target `/root/.claude` / `/root/.codex` — a
location mismatch. So you must provision a working auth path first:

### 2a. Claude auth — pick ONE

- **Recommended (token env, bypasses the file-location question):**
  ```bash
  export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"   # interactive, one-time
  ```
  Then the smoke passes `-e CLAUDE_CODE_OAUTH_TOKEN` (docker forwards the value
  from your env; it never appears on the command line or in logs).

- **Alternative (mount the credential where the CLI actually reads it):** the
  CLI reads `$CLAUDE_CONFIG_DIR/.credentials.json`, and the image sets
  `CLAUDE_CONFIG_DIR=/profile`, so mount to `/profile`, **not** the
  `container_auth` default of `/root/.claude`:
  ```bash
  # in the docker run below, use:
  -v "$HOME/.claude/.credentials.json:/profile/.credentials.json:ro"
  ```

### 2b. Codex auth — pick ONE

`~/.codex/auth.json` on this host is a ChatGPT-plan OAuth login: its
`OPENAI_API_KEY` field is `null` and it carries OAuth `tokens`. So a bare
`-e OPENAI_API_KEY` will NOT authenticate.

- **Recommended (reuse the existing ChatGPT login):** mount `auth.json` to the
  path codex reads under `CODEX_HOME=/profile`:
  ```bash
  # in the docker run below, use:
  -v "$HOME/.codex/auth.json:/profile/auth.json:ro"
  ```

- **Alternative (real API key):**
  ```bash
  export OPENAI_API_KEY="sk-...your-key..."
  ```
  then pass `-e OPENAI_API_KEY` (this DOES incur OpenAI API spend on top of the
  smoke).

### 2c. Docker

`--add-host=host.docker.internal:host-gateway` must be honoured (built-in on
Docker Desktop; Linux Docker ≥ 20.10). `tests/test_fixture_from_container.py`
skips if it is not — run that test first to confirm your Docker honours it.

---

## 3. Step 0 — offline gate (no spend; run this first, every time)

From the repo root, with local Postgres available:

```bash
# 0.1  full offline suite stays green (304 passing / 2 skipped baseline)
uv run pytest -q

# 0.2  build both runner images (no secrets baked)
docker build -t wololo-evals/claude-runner images/claude-runner
docker build -t wololo-evals/codex-runner  images/codex-runner

# 0.3  CLIs run --version INSIDE the container (no auth, no model)
docker run --rm wololo-evals/claude-runner claude --version
docker run --rm wololo-evals/codex-runner  codex  --version

# 0.4  no baked secret in either image
docker history --no-trunc wololo-evals/claude-runner | grep -iE 'token|api_key|oauth|secret|credential' && echo 'LEAK!' || echo 'clean'
docker history --no-trunc wololo-evals/codex-runner  | grep -iE 'token|api_key|oauth|secret|credential' && echo 'LEAK!' || echo 'clean'

# 0.5  container can reach the host fixture server (real curl, no model)
uv run pytest tests/test_fixture_from_container.py -v
```

If 0.5 **skips** with "could not resolve host.docker.internal", your Docker does
not honour `host-gateway` — fix that before the live smoke, or the fixture URL
will be unreachable from the container.

---

## 4. Step 1 — start the host fixture server + a throwaway workspace

Run this in a **dedicated terminal** and leave it running for the duration of
the smoke. It binds an ephemeral loopback port and prints the container-reachable
URL and the export lines the docker steps need.

```bash
# terminal A — fixture server (leave running)
uv run python - <<'PY'
import pathlib, time
from wololo_evals.engines.browser_fixture_server import BrowserFixtureServer
from wololo_evals.inspect_adapter import container_fixture_url
pages = pathlib.Path("fixtures/browser/pages").resolve()
srv = BrowserFixtureServer(pages).start()
page = "native-select.html"
url = container_fixture_url(srv.base_url, page)   # loopback -> host.docker.internal
print("\n=== fixture server up ===")
print("host base url :", srv.base_url)
print("container url :", url)
print("\nrun these in terminal B:")
print(f"  export WOLOLO_FIXTURE_URL='{url}'")
print("\nCtrl-C here to stop the server when the smoke is done.\n", flush=True)
try:
    while True: time.sleep(3600)
except KeyboardInterrupt:
    srv.stop()
PY
```

In a **second terminal** (terminal B), copy the `export WOLOLO_FIXTURE_URL=...`
line it printed, then create the throwaway git workspace the CLI will edit:

```bash
# terminal B
export WOLOLO_FIXTURE_URL='http://host.docker.internal:<PORT>/native-select.html'  # from terminal A

SMOKE_WS="$(mktemp -d)/workspace"
mkdir -p "$SMOKE_WS"
git -C "$SMOKE_WS" init -q
printf 'smoke workspace\n' > "$SMOKE_WS/NOTES.md"
git -C "$SMOKE_WS" add -A && git -C "$SMOKE_WS" -c user.email=smoke@local -c user.name=smoke commit -qm init
echo "workspace: $SMOKE_WS"
```

---

## 5. Step 2 — claude live smoke (one real model turn)

```bash
# terminal B — after Step 1. Uses CLAUDE_CODE_OAUTH_TOKEN from precondition 2a.
CLAUDE_TR="$(mktemp -d)"
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -e CLAUDE_CODE_OAUTH_TOKEN \
  -e "WOLOLO_FIXTURE_URL=${WOLOLO_FIXTURE_URL}" \
  -v "${SMOKE_WS}:/workspace" \
  -v "${CLAUDE_TR}:/transcript" \
  wololo-evals/claude-runner \
  claude -p "Append one line reading 'smoke ok' to NOTES.md. Do not touch anything else." \
    --output-format stream-json --verbose \
    --model claude-haiku-4-5 --max-turns 4 --dangerously-skip-permissions
echo "claude exit: $?"
echo "claude transcript dir: ${CLAUDE_TR}"
```

- `claude-haiku-4-5` is the cheapest allow-listed claude model
  (`ClaudeAdapter.allowed_models`) — keeps the smoke at the low end of ~$2.
- If you chose the mount path from 2a instead of the token, drop
  `-e CLAUDE_CODE_OAUTH_TOKEN` and add
  `-v "$HOME/.claude/.credentials.json:/profile/.credentials.json:ro"`.

---

## 6. Step 3 — codex live smoke (one real model turn)

```bash
# terminal B. Uses the mounted ChatGPT login from precondition 2b (recommended).
CODEX_TR="$(mktemp -d)"
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -v "$HOME/.codex/auth.json:/profile/auth.json:ro" \
  -e "WOLOLO_FIXTURE_URL=${WOLOLO_FIXTURE_URL}" \
  -v "${SMOKE_WS}:/workspace" \
  -v "${CODEX_TR}:/transcript" \
  wololo-evals/codex-runner \
  codex exec --json --skip-git-repo-check \
    --model gpt-5.4-mini \
    "Append one line reading 'smoke ok' to NOTES.md. Do not touch anything else."
echo "codex exit: $?"
echo "codex transcript dir: ${CODEX_TR}"
```

- `gpt-5.4-mini` is the cheapest allow-listed codex model
  (`CodexAdapter.allowed_models`).
- If you chose the API-key path from 2b instead, drop the `auth.json` mount and
  add `-e OPENAI_API_KEY`.

> Re-run the same `SMOKE_WS` for codex only after inspecting/resetting the
> claude diff (`git -C "$SMOKE_WS" checkout -- .`), or use a second workspace, so
> each engine's diff is attributable.

---

## 7. Step 4 — confirm a REAL trace came back

For **each** engine's transcript dir (`$CLAUDE_TR` / `$CODEX_TR`) and the
workspace:

```bash
TR="$CLAUDE_TR"   # then repeat with TR="$CODEX_TR"

# 4.1  fleet-hooks event stream exists and has model-turn events
test -s "$TR/events.jsonl" && echo "events.jsonl present ($(wc -l < "$TR/events.jsonl") lines)" || echo "MISSING events.jsonl"

# 4.2  at least one post-llm-event line (this is the ONLY source of turn count;
#      harness-seam.md §3 count_turns()). A real model turn produced >= 1.
grep -c '"stage": *"post-llm-event"' "$TR/events.jsonl"

# 4.3  the CLI left its edits UNCOMMITTED as a git diff in the workspace
git -C "$SMOKE_WS" status --porcelain
git -C "$SMOKE_WS" diff
```

**A real trace is confirmed when:** `events.jsonl` is non-empty, 4.2 prints
`>= 1`, the engine's own stdout stream carried a terminal `result`/usage event
(the CLIs print token usage — `ClaudeAdapter._parse_usage` /
`CodexAdapter._parse_usage` read it), and 4.3 shows `NOTES.md` modified. Exit
code 0 means the harness loop completed — it does **not** assert the edit is
correct (correctness is scored separately by the acceptance test, not part of
the smoke).

> **Honest caveat — `events.jsonl` is a fleet-hooks deliverable, not the CLI's.**
> The runner images ship only the vanilla `claude` / `codex` CLI; the
> `post-llm-event` JSONL stream is emitted by the fleet-hooks integration behind
> each harness's manifest liveness claim, which is **out of this repo's scope**
> (`docs/INTEGRATION-DEBT.md`). If step 4.2 prints `0` on the live smoke, that is
> the expected signal that fleet-hooks event emission is not yet wired for these
> images — the auth + model-turn + git-diff half of the plumbing is still proven,
> and the turn-count seam is the known remaining integration debt to close before
> a verdict-bearing run. Record the result either way.

---

## 8. Cleanup

```bash
# terminal A: Ctrl-C to stop the fixture server.
# terminal B:
rm -rf "$SMOKE_WS" "$CLAUDE_TR" "$CODEX_TR"
unset CLAUDE_CODE_OAUTH_TOKEN OPENAI_API_KEY WOLOLO_FIXTURE_URL   # drop any secrets from the shell
```

No secret is written to the repo, an image layer, or any log by this runbook:
auth reaches the container only via `-e KEY` env-forwarding (value never on the
argv) and `:ro` credential mounts.

---

## 9. After the smoke is green: the ~$30 dogfood A/B

Once both engines authenticate, run a real model turn, and (turn-count debt
permitting) emit a trace, the separate operator-gated **`dropdowns v1 → v2`**
A/B (task #51) reuses this exact machinery — same images, same
`container_auth` env/mounts, same `host.docker.internal` fixture URL — but with
the real browser dropdown fixture repo, the v1/v2 treatment skill materialised
under `/profile/skills/…`, `n_trials` paired baseline/treatment runs, and the
acceptance test driving a headless browser at the fixture pages. That is a
larger spend and a separate approval; do not run it as part of this smoke.
```
