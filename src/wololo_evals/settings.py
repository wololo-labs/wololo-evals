"""Service configuration, read once from the environment.

Fail-closed posture: anything security-load-bearing (database URL, master
key, JWKS path) has no silent default in production; dev/test values are
injected explicitly by the compose file or the test harness.

The configuration is split into cohesive sub-settings so a consumer only pays
the fail-closed cost of the env vars it actually needs. The DB-free eval core
(``core``, the file-first CLI, a judge-only smoke) constructs just
``JudgeSettings()`` / ``BlobSettings()`` and never has to satisfy
``WOLOLO_EVALS_DATABASE_URL`` / ``WOLOLO_EVALS_MASTER_KEY`` / ``WOLOLO_EVALS_JWKS``
to obtain a working judge backend. The service entrypoints (``asgi``,
``jobs.worker._main``) construct the aggregate :class:`Settings`, which composes
every sub-setting and keeps the historical flat attribute surface
(``cfg.database_url``, ``cfg.judge_model``, …) working unchanged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    """Postgres connection. The one mandatory var with no default."""

    database_url: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_DATABASE_URL")
    )


@dataclass(frozen=True)
class CryptoSettings:
    """Envelope-cipher master key material (crypto.py)."""

    # AES-256 master key, 64 hex chars. Production: KMS-sourced; dev/compose:
    # local value. Envelope wrapping in crypto.py.
    master_key_hex: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_MASTER_KEY")
    )
    master_key_id: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_MASTER_KEY_ID", "dev-master-1")
    )
    keyring_path: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_KEYRING", "./blobdata/keyring.json")
    )


@dataclass(frozen=True)
class AuthSettings:
    """Entitlement-token (JWT) verification against the issuer JWKS."""

    # Both values are mandatory. Deployments provide their issuer's public
    # JWKS and identifier explicitly; no development signing material ships.
    jwks_path: str = field(default_factory=lambda: _env("WOLOLO_EVALS_JWKS"))
    expected_iss: str = field(default_factory=lambda: _env("WOLOLO_EVALS_ISSUER"))


@dataclass(frozen=True)
class BlobSettings:
    """Blob store selection (``make_blobstore`` duck-types these four attrs)."""

    # Blob store: "fs" (dev/test) or "s3" (MinIO/production).
    blob_backend: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_BLOB_BACKEND", "fs")
    )
    blob_root: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_BLOB_ROOT", "./blobdata/blobs")
    )
    s3_bucket: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_S3_BUCKET", "wololo-evals")
    )
    s3_endpoint: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_S3_ENDPOINT", "")
    )


@dataclass(frozen=True)
class JudgeSettings:
    """LLM judge backend selection. Every field has a default, so
    ``JudgeSettings()`` needs no env var set at all — this is the seam that
    lets ``judge.run_judge`` be wired from config with zero DB/crypto/auth
    coupling."""

    # LLM judge: anthropic-compatible HTTP endpoint (default) or the claude
    # CLI bridge for OAuth-only environments.
    judge_model: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_JUDGE_MODEL", "claude-opus-4-1")
    )
    judge_backend: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_JUDGE_BACKEND", "http")
    )
    # Static scoped key until wololo-broker (03) issues short-TTL per-run
    # credentials (named integration debt).
    judge_base_url: str = field(
        default_factory=lambda: _env(
            "WOLOLO_EVALS_JUDGE_BASE_URL", "https://api.anthropic.com"
        )
    )
    judge_api_key: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_JUDGE_API_KEY", "")
    )


@dataclass(frozen=True)
class DeploySettings:
    """wololo-deploy canary rollback endpoint."""

    # The deploy_stub in compose today; swapped for the real Flagger
    # confirm-rollout webhook once 06c lands.
    deploy_base_url: str = field(
        default_factory=lambda: _env(
            "WOLOLO_EVALS_DEPLOY_BASE_URL", "http://wololo-deploy-stub:8080"
        )
    )


@dataclass(frozen=True)
class RepoEvalSettings:
    """Where a repo-eval run may read a git repository from on the host."""

    # os.pathsep-separated directories. Empty (the default) refuses every
    # repo_path, so naming a host repository is something an operator turns on.
    roots: tuple = field(
        default_factory=lambda: tuple(
            r for r in _env("WOLOLO_EVALS_REPO_EVAL_ROOTS", "").split(os.pathsep) if r
        )
    )


@dataclass(frozen=True)
class Settings:
    """Aggregate service configuration for the entrypoints that need all of it.

    Composes every sub-setting; constructing it stays fail-closed (all three
    mandatory vars — DB URL, master key, JWKS — are demanded because their
    sub-settings are constructed). The flat ``@property`` accessors preserve
    the historical attribute surface so ``api.py``/``asgi.py``/``jobs.worker``
    keep reading ``cfg.database_url``, ``cfg.judge_model``, … unchanged.
    """

    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    crypto: CryptoSettings = field(default_factory=CryptoSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    blob: BlobSettings = field(default_factory=BlobSettings)
    judge: JudgeSettings = field(default_factory=JudgeSettings)
    deploy: DeploySettings = field(default_factory=DeploySettings)
    repo_eval: RepoEvalSettings = field(default_factory=RepoEvalSettings)

    # --- Backwards-compatible flat attribute surface ---------------------
    @property
    def database_url(self) -> str:
        return self.database.database_url

    @property
    def master_key_hex(self) -> str:
        return self.crypto.master_key_hex

    @property
    def master_key_id(self) -> str:
        return self.crypto.master_key_id

    @property
    def keyring_path(self) -> str:
        return self.crypto.keyring_path

    @property
    def jwks_path(self) -> str:
        return self.auth.jwks_path

    @property
    def expected_iss(self) -> str:
        return self.auth.expected_iss

    @property
    def blob_backend(self) -> str:
        return self.blob.blob_backend

    @property
    def blob_root(self) -> str:
        return self.blob.blob_root

    @property
    def s3_bucket(self) -> str:
        return self.blob.s3_bucket

    @property
    def s3_endpoint(self) -> str:
        return self.blob.s3_endpoint

    @property
    def judge_model(self) -> str:
        return self.judge.judge_model

    @property
    def judge_backend(self) -> str:
        return self.judge.judge_backend

    @property
    def judge_base_url(self) -> str:
        return self.judge.judge_base_url

    @property
    def judge_api_key(self) -> str:
        return self.judge.judge_api_key

    @property
    def deploy_base_url(self) -> str:
        return self.deploy.deploy_base_url


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
