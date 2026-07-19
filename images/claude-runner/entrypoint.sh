#!/bin/sh
# Runner entrypoint for the Claude Code harness (harness-seam.md §3).
#
# Honours the sandbox seam env and NEVER falls back to the host $HOME:
#   CLAUDE_CONFIG_DIR ({profile_env}) -> the in-sandbox /profile: the CLI reads
#       its config + the treatment-arm skill materialised under
#       /profile/skills/<slug>/… from here, not from any host dotfile.
#   WOLOLO_TRANSCRIPT_DIR             -> /transcript: fleet-hooks writes the JSONL
#       event stream to $WOLOLO_TRANSCRIPT_DIR/events.jsonl.
#
# Runs the harness command headless (passed as "$@") — e.g.
#   claude -p "<prompt>" --output-format json --max-turns N \
#          --dangerously-skip-permissions
# Edits are left UNCOMMITTED as a git diff in /workspace (the seam captures them
# with ``git diff`` afterwards). The exit code is the harness command's own —
# setup / loop success only, never "task solved". No secret is read or written
# here; auth is supplied by the caller via env + read-only mounts.
set -e

export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/profile}"
export WOLOLO_TRANSCRIPT_DIR="${WOLOLO_TRANSCRIPT_DIR:-/transcript}"
mkdir -p "$CLAUDE_CONFIG_DIR" "$WOLOLO_TRANSCRIPT_DIR"

exec "$@"
