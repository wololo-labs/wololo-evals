#!/bin/sh
# codex-runner entrypoint — headless, no baked secrets.
#
# Honours the harness seam env (harness-seam.md §3):
#   CODEX_HOME            codex's profile_env; the CLI reads its config/skills
#                         from here (the in-sandbox /profile), never host $HOME.
#                         The treatment-arm skill is materialised under
#                         $CODEX_HOME/skills/<slug>/... by the load-profile stage.
#   WOLOLO_TRANSCRIPT_DIR fleet-hooks JSONL events dir (always /transcript
#                         in-sandbox); one post-llm-event per model turn is
#                         written to $WOLOLO_TRANSCRIPT_DIR/events.jsonl.
#
# Like the claude-runner, this CAPTURES THE CLI'S OWN EVENT STREAM into
# events.jsonl. Vanilla `codex exec --json` streams thread/item events but does
# not itself emit the fleet-hooks post-llm-event stream that count_turns()
# (harness_solver.count_turns) counts, so without this the trial turn count would
# be 0 and the trace unscoreable. The wrapper below passes the CLI stdout through
# unchanged (raw harness_stdout preserved for the trace normaliser) while
# emitting one {"stage":"post-llm-event",...} line per model assistant turn.
#
# Auth is NEVER baked in — OPENAI_API_KEY / CODEX_AUTH_TOKEN and the read-only
# ~/.codex/auth.json mount are supplied at exec time by container_auth.
set -e

# This image only ever wraps the Codex CLI, so the transform is fixed to the
# codex `exec --json` shape (item.completed with item_type agent_message).
ENGINE=codex

# --- fleet-hooks event transform ---------------------------------------------
# Written to a temp file so the wrapper pipeline AND the --emit-events test hook
# run identical code. node is always present (Node base image); reliable per-line
# JSON parsing with no python/jq assumed. Reads the CLI's JSONL stream on stdin,
# passes every line to stdout UNCHANGED (raw harness_stdout preserved), and
# appends one post-llm-event line per model turn to the events file.
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

: "${CODEX_HOME:=/profile}"
: "${WOLOLO_TRANSCRIPT_DIR:=/transcript}"
export CODEX_HOME WOLOLO_TRANSCRIPT_DIR
mkdir -p "$CODEX_HOME" "$WOLOLO_TRANSCRIPT_DIR"

# No command: stay alive so the docker sandbox can `docker exec` trial steps in.
if [ "$#" -eq 0 ]; then
  exec sleep infinity
fi

# Wrapper: run the rendered `codex exec …` (or a `codex --version` probe), then
# stream its stdout through the transform, which passes every line to the caller
# UNCHANGED while extracting post-llm-event lines into events.jsonl. Stdout is
# buffered to a temp file so codex's own exit code is captured directly (a
# pipeline would lose it and, under `set -e`, abort on a non-zero exit first).
# Codex leaves its edits uncommitted in /workspace; the exit code reflects the
# harness loop, not task correctness (scored separately). A bare `codex
# --version` passes straight through (no JSON, no events written).
EVENTS_FILE="${WOLOLO_TRANSCRIPT_DIR}/events.jsonl"
RAW_FILE="$(mktemp "${TMPDIR:-/tmp}/wololo-raw.XXXXXX")" || RAW_FILE="/tmp/wololo-raw"
set +e
"$@" > "$RAW_FILE"
code=$?
set -e
node "$TRANSFORM_JS" "$ENGINE" "$EVENTS_FILE" < "$RAW_FILE"
rm -f "$RAW_FILE"
exit "$code"
