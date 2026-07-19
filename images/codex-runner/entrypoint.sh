#!/bin/sh
# codex-runner entrypoint — headless, no baked secrets.
#
# Honours the harness seam env (harness-seam.md §3):
#   CODEX_HOME            codex's profile_env; the CLI reads its config/skills
#                         from here (the in-sandbox /profile), never host $HOME.
#                         The treatment-arm skill is materialised under
#                         $CODEX_HOME/skills/<slug>/... by the load-profile stage.
#   WOLOLO_TRANSCRIPT_DIR fleet-hooks JSONL events dir (always /transcript
#                         in-sandbox); the CLI's events land in events.jsonl.
#
# Auth is NEVER baked in — OPENAI_API_KEY / CODEX_AUTH_TOKEN and the read-only
# ~/.codex/auth.json mount are supplied at exec time by container_auth.
set -e

: "${CODEX_HOME:=/profile}"
: "${WOLOLO_TRANSCRIPT_DIR:=/transcript}"
export CODEX_HOME WOLOLO_TRANSCRIPT_DIR

mkdir -p "$CODEX_HOME" "$WOLOLO_TRANSCRIPT_DIR"

# Run whatever the sandbox/exec seam hands us: the rendered `codex exec …`
# headless invocation for a trial, or `codex --version` for offline
# verification. Codex leaves its edits uncommitted in /workspace; exit code
# reflects the harness loop, not task correctness (scored separately).
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# No command: stay alive so the docker sandbox can `docker exec` trial steps in.
exec sleep infinity
