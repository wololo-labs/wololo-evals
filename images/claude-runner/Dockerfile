# Per-harness runner image for the Claude Code CLI (SPEC S6). A slim Node base
# with the official npm CLI, version-pinned inside
# ``ClaudeAdapter.default_version_range`` (">=1.0.0,<3.0.0"). NO secret is ever
# baked into any layer — auth arrives at run time via read-only bind mounts +
# env (engines/container_auth.py), never a COPY/ENV of a token.
FROM node:22-slim

# git:             the harness seam clones the fixture repo and reads
#                  ``git diff`` / ``git status --porcelain`` for the /workspace
#                  checkout inside this container (harness-seam.md §3-4).
# ca-certificates: TLS for the eval workspace tooling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pin to the exact CLI the host runs (2.1.19), inside the adapter's semver range.
# Override at build time with --build-arg CLAUDE_CODE_VERSION=x.y.z.
ARG CLAUDE_CODE_VERSION=2.1.19
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# In-sandbox paths from the seam (harness-seam.md §3). Setting them as image ENV
# guarantees the CLI reads its config/skills from the sandbox profile dir and
# writes fleet-hooks events to the sandbox transcript dir even on the
# ``docker exec`` path (which does NOT run ENTRYPOINT) — the host $HOME is never
# consulted.
ENV HOME=/root \
    CLAUDE_CONFIG_DIR=/profile \
    WOLOLO_TRANSCRIPT_DIR=/transcript
RUN mkdir -p /profile /transcript /workspace
WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Default keeps the container alive when started without a command; the eval
# seam and the ``--version`` verification both override this with explicit argv.
CMD ["sleep", "infinity"]
