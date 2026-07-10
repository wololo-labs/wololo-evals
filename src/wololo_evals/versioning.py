"""Suite + rubric content-hash versioning (SPEC S9).

The rubric weights and the pass bar ARE the product; unversioned, every
rubric edit corrupts every longitudinal comparison. ``rubric_version`` =
sha256 of the suite's ``rubric.yaml`` bytes; ``suite_version`` = sha256 over
the sorted relative-path + content pairs of the suite directory. Both are
computed at run creation and pinned onto runs, every score row, every
leaderboard row, and every canary gate. Historical rubric contents are
archived content-addressed in the blob store at first use so old versions
remain resolvable after the file changes on disk.
"""
from __future__ import annotations

import hashlib
import pathlib

from .blobstore import rubric_archive_key


def rubric_version(rubric_path: str | pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(rubric_path).read_bytes()).hexdigest()


def suite_version(suite_dir: str | pathlib.Path) -> str:
    """sha256 over sorted (relative path, content) pairs of the suite dir.

    Deterministic across checkouts; ``__pycache__`` and dotfiles excluded.
    """
    root = pathlib.Path(suite_dir)
    if not root.is_dir():
        raise ValueError(f"suite directory {root} does not exist")
    h = hashlib.sha256()
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.name.startswith(".")
    )
    if not files:
        raise ValueError(f"suite directory {root} is empty")
    for p in files:
        h.update(str(p.relative_to(root)).encode("utf-8"))
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


def archive_rubric(blobstore, tenant_id: str, rubric_path: str | pathlib.Path) -> str:
    """Content-address the rubric bytes into the tenant's blob prefix.

    Idempotent: the key embeds the hash, so re-archiving the same content is
    a no-op overwrite with identical bytes.
    """
    path = pathlib.Path(rubric_path)
    version = rubric_version(path)
    key = rubric_archive_key(tenant_id, version)
    blobstore.put(tenant_id, key, path.read_bytes())
    return key
