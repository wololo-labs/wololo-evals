#!/usr/bin/env bash
# Run the find-missing-tests eval with no GitHub credential available to the agent.
#   - GH_TOKEN and GITHUB_TOKEN are removed from the environment waza and the agent inherit.
#   - GH_CONFIG_DIR points at an empty directory, so the gh CLI finds no stored login.
# Not covered: Copilot's own GitHub MCP tools. A skill has no tool allow-list, so those stay
# reachable from a live run. Do not run waza for this eval without this script.
# Usage (from the repo root): docs/spikes/waza/find-missing-tests/run-isolated.sh [waza run flags]
set -euo pipefail
cfg="$(mktemp -d)"
trap 'rm -rf "$cfg"' EXIT
exec env -u GH_TOKEN -u GITHUB_TOKEN GH_CONFIG_DIR="$cfg" \
  waza run docs/spikes/waza/find-missing-tests/eval.yaml "$@"
