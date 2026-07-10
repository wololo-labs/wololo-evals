"""Tenant-prefixed blob store for raw ``.eval`` logs and rubric archives.

Invariant 1's blob half (SPEC S10): every key is ``evals/{tenant_id}/...``;
the store layer asserts the RESOLVED tenant (from the entitlement token,
never the URL) matches the key's tenant prefix before any read or write. A
mismatch raises :class:`BlobAccessDenied`, which the API maps to a 404 (not
403: no existence oracle) plus an ``audit_events(decision='denied')`` row.

Two backends behind one interface: filesystem (dev/test, no daemon needed)
and S3-compatible (MinIO in compose, any S3 in production; bucket SSE is
enabled at the bucket, not here).
"""
from __future__ import annotations

import pathlib


class BlobAccessDenied(Exception):
    """Resolved tenant does not match the key's tenant prefix."""


class BlobNotFound(Exception):
    """Key absent (only raised after the tenant check passes)."""


def check_tenant_prefix(resolved_tenant_id: str, key: str) -> None:
    """Fail-closed prefix check shared by every backend.

    Also rejects traversal segments: a key like
    ``evals/northwind/../meridian/...`` passes the naive prefix test but
    resolves into another tenant's space on the filesystem backend.
    """
    if not resolved_tenant_id:
        raise BlobAccessDenied("no resolved tenant")
    if "\\" in key or ".." in key.split("/"):
        raise BlobAccessDenied("traversal segment in key")
    if not key.startswith(f"evals/{resolved_tenant_id}/"):
        raise BlobAccessDenied(
            f"key does not belong to tenant {resolved_tenant_id!r}"
        )


class FsBlobStore:
    """Filesystem backend. Keys map to paths under *root*."""

    def __init__(self, root: str):
        self._root = pathlib.Path(root)

    def _path(self, key: str) -> pathlib.Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise BlobAccessDenied("path escape attempt")
        return path

    def put(self, resolved_tenant_id: str, key: str, data: bytes) -> str:
        check_tenant_prefix(resolved_tenant_id, key)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, resolved_tenant_id: str, key: str) -> bytes:
        check_tenant_prefix(resolved_tenant_id, key)
        path = self._path(key)
        if not path.exists():
            raise BlobNotFound(key)
        return path.read_bytes()

    def exists(self, resolved_tenant_id: str, key: str) -> bool:
        check_tenant_prefix(resolved_tenant_id, key)
        return self._path(key).exists()


class S3BlobStore:
    """S3-compatible backend (MinIO in compose)."""

    def __init__(self, bucket: str, endpoint_url: str | None = None):
        import boto3  # optional dependency, extras group "s3"

        self._bucket = bucket
        self._client = boto3.client("s3", endpoint_url=endpoint_url or None)

    def put(self, resolved_tenant_id: str, key: str, data: bytes) -> str:
        check_tenant_prefix(resolved_tenant_id, key)
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        return key

    def get(self, resolved_tenant_id: str, key: str) -> bytes:
        check_tenant_prefix(resolved_tenant_id, key)
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            raise BlobNotFound(key) from None
        return resp["Body"].read()

    def exists(self, resolved_tenant_id: str, key: str) -> bool:
        check_tenant_prefix(resolved_tenant_id, key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False


def make_blobstore(settings):
    if settings.blob_backend == "s3":
        return S3BlobStore(settings.s3_bucket, settings.s3_endpoint or None)
    return FsBlobStore(settings.blob_root)


def eval_log_key(tenant_id: str, run_id: str, arm: str, trial_index: int) -> str:
    return f"evals/{tenant_id}/{run_id}/trial-{arm}-{trial_index}.eval"


def rubric_archive_key(tenant_id: str, rubric_version: str) -> str:
    return f"evals/{tenant_id}/_rubrics/{rubric_version}.yaml"
