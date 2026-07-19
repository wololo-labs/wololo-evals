# wololo-evals/codex-runner — sandbox image for the Codex CLI harness-under-eval.
#
# Mirrors the claude-runner contract (docs/plans/wave1-contracts.md I2): a slim
# Node base carrying the pinned Codex CLI, invokable headless. NO secrets are
# ever baked into any layer — auth arrives at run time via read-only bind
# mounts + env constructed by engines/container_auth.py.
FROM node:22-bookworm-slim

# git: the harness seam (harness-seam.md §3) runs `git clone/checkout/diff/
# status` inside this sandbox to stage the fixture and capture the diff.
# ca-certificates: TLS for the clone. No build tools — the Codex npm package
# ships a prebuilt native binary per platform via optionalDependencies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Codex CLI, pinned to 0.141.x — consistent with CodexAdapter.default_version_range
# (>=0.5.0,<2.0.0) and the host CLI shape (`codex-cli 0.141.0`). npm resolves the
# platform-matching native binary (@openai/codex-linux-x64 etc.) automatically.
ARG CODEX_VERSION=0.141.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

# In-sandbox seam paths (harness-seam.md §3). CODEX_HOME is codex's profile_env,
# so the CLI reads its config/skills from /profile (the in-sandbox profile dir),
# never the host $HOME. These are defaults; the exec seam overrides them per run.
ENV CODEX_HOME=/profile \
    WOLOLO_TRANSCRIPT_DIR=/transcript
RUN mkdir -p /workspace /profile /transcript

WORKDIR /workspace
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# No args -> stay alive so the docker sandbox can `docker exec` trial steps in.
CMD ["sleep", "infinity"]
