"""Service configuration, read once from the environment.

Fail-closed posture: anything security-load-bearing (database URL, master
key, JWKS path) has no silent default in production; dev/test values are
injected explicitly by the compose file or the test harness.
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
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_DATABASE_URL")
    )
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
    # Entitlement verification: path to the issuer JWKS. Fixture JWKS until
    # wololo-deploy (06a, the sole real issuer) publishes its JWKS URL; the
    # validation code path is identical, only the key source swaps.
    jwks_path: str = field(default_factory=lambda: _env("WOLOLO_EVALS_JWKS"))
    expected_iss: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_ISSUER", "wololo-deploy")
    )
    # LLM judge: anthropic-compatible HTTP endpoint (default) or the claude
    # CLI bridge for OAuth-only environments.
    judge_model: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_JUDGE_MODEL", "claude-opus-4-1")
    )
    judge_backend: str = field(
        default_factory=lambda: _env("WOLOLO_EVALS_JUDGE_BACKEND", "http")
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
