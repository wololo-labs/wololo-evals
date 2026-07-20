#!/bin/sh
# Runner entrypoint for the Claude Code harness (harness-seam.md §3).
#
# Honours the sandbox seam env and NEVER falls back to the host $HOME:
#   CLAUDE_CONFIG_DIR ({profile_env}) -> the in-sandbox /profile: the CLI reads
#       its config + the treatment-arm skill materialised under
#       /profile/skills/<slug>/… from here, not from any host dotfile.
#   WOLOLO_TRANSCRIPT_DIR             -> /transcript: the fleet-hooks JSONL event
#       stream is written to $WOLOLO_TRANSCRIPT_DIR/events.jsonl.
#
# Runs the harness command headless (passed as "$@") — e.g.
#   claude -p "<prompt>" --output-format stream-json --verbose --max-turns N \
#          --dangerously-skip-permissions
# and, crucially, CAPTURES THE CLI'S OWN EVENT STREAM into events.jsonl: vanilla
# Claude Code does not itself write the fleet-hooks post-llm-event stream that is
# the sole source for count_turns() (harness_solver.count_turns), so without this
# a trial's turn count would be 0 and the trace unscoreable. The wrapper below
# streams the CLI's stdout through unchanged (so the seam still captures the raw
# harness_stdout for the trace normaliser) while emitting one
# {"stage":"post-llm-event",...} line per model turn.
#
# Edits are left UNCOMMITTED as a git diff in /workspace (the seam captures them
# with ``git diff`` afterwards). The exit code is the harness command's own —
# setup / loop success only, never "task solved". No secret is read or written
# here; auth is supplied by the caller via env + read-only mounts.
set -e

# This image only ever wraps the Claude Code CLI, so the event transform is fixed
# to the Claude stream-json shape (one `assistant` event per model turn).
ENGINE=claude

# --- fleet-hooks event transform ---------------------------------------------
# The transform is written to a temp file so the wrapper pipeline AND the
# --emit-events test hook run the identical code. node is always present (this is
# a Node base image) and gives reliable per-line JSON parsing — no python/jq is
# assumed. It reads the CLI's JSONL event stream on stdin, passes every line to
# stdout UNCHANGED (raw harness_stdout is preserved for the trace normaliser),
# and appends one post-llm-event line per model turn to the events file.
TRANSFORM_JS="$(mktemp "${TMPDIR:-/tmp}/wololo-events.XXXXXX")" || TRANSFORM_JS="/tmp/wololo-events.js"
cat > "$TRANSFORM_JS" <<'NODE_EOF'
'use strict';
// argv: <engine> <eventsFile>
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const engine = process.argv[2] || 'unknown';
const eventsFile = process.argv[3];

// Does this parsed CLI event represent one model/assistant turn?
function isModelTurn(ev) {
  if (!ev || typeof ev !== 'object') return false;
  if (engine === 'claude') {
    // Claude Code stream-json emits exactly one `assistant` event per model
    // response turn (text + tool_use blocks in one message); `user` events are
    // tool results and `result` is the terminal summary.
    return ev.type === 'assistant';
  }
  if (engine === 'codex') {
    // Codex `exec --json` (thread/item model): the model's assistant message
    // lands as an item.completed with item_type `agent_message`. Tolerate tag
    // drift across codex versions (assistant_message; classic msg.type).
    const t = ev.type;
    if (t === 'item.completed' || t === 'item.updated') {
      const it = ev.item || {};
      const itype = it.item_type || it.type;
      return itype === 'agent_message' || itype === 'assistant_message';
    }
    if (t === 'agent_message' || t === 'assistant_message') return true;
    const m = ev.msg;
    if (m && typeof m === 'object' &&
        (m.type === 'agent_message' || m.type === 'assistant_message')) return true;
    return false;
  }
  return false;
}

let out = null;
if (eventsFile) {
  fs.mkdirSync(path.dirname(eventsFile), { recursive: true });
  out = fs.createWriteStream(eventsFile, { flags: 'a' });
}

let turn = 0;
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  process.stdout.write(line + '\n'); // passthrough, unchanged
  const s = line.trim();
  if (!s) return;
  let ev;
  try { ev = JSON.parse(s); } catch (_) { return; } // non-JSON line: skip
  if (!isModelTurn(ev)) return;
  turn += 1;
  const rec = {
    stage: 'post-llm-event',
    engine: engine,
    turn: turn,
    ts: Date.now() / 1000,
    source_type: (ev && ev.type) || (ev && ev.msg && ev.msg.type) || null,
  };
  if (out) out.write(JSON.stringify(rec) + '\n');
});
rl.on('close', () => { if (out) out.end(); });
NODE_EOF

# --emit-events: run ONLY the transform over stdin. Used internally by the
# wrapper pipeline below and directly by tests/test_events_emission.py (offline,
# no CLI, no model). Optional arg: the events file (defaults to the seam path).
if [ "$1" = "--emit-events" ]; then
  shift
  exec node "$TRANSFORM_JS" "$ENGINE" "${1:-${WOLOLO_TRANSCRIPT_DIR:-/transcript}/events.jsonl}"
fi

export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/profile}"
export WOLOLO_TRANSCRIPT_DIR="${WOLOLO_TRANSCRIPT_DIR:-/transcript}"
mkdir -p "$CLAUDE_CONFIG_DIR" "$WOLOLO_TRANSCRIPT_DIR"

# No command: keep the container alive so the sandbox can `docker exec` steps in.
if [ "$#" -eq 0 ]; then
  exec sleep infinity
fi

# Wrapper: run the harness CLI, then stream its stdout through the transform,
# which passes every line to the caller UNCHANGED while extracting post-llm-event
# lines into events.jsonl. Stdout is buffered to a temp file so the CLI's own
# exit code is captured directly (a pipeline would both lose it and, under
# `set -e`, abort on a non-zero CLI before we could read it). The seam consumes
# stdout only after the command returns, so buffering changes nothing there. A
# bare `claude --version` probe passes straight through — no JSON, no events.
EVENTS_FILE="${WOLOLO_TRANSCRIPT_DIR}/events.jsonl"
RAW_FILE="$(mktemp "${TMPDIR:-/tmp}/wololo-raw.XXXXXX")" || RAW_FILE="/tmp/wololo-raw"
set +e
"$@" > "$RAW_FILE"
code=$?
set -e
node "$TRANSFORM_JS" "$ENGINE" "$EVENTS_FILE" < "$RAW_FILE"
rm -f "$RAW_FILE"
exit "$code"
