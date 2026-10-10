"""Envelope encryption for candidate payloads at rest (SPEC S10).

AES-256-GCM over the canonical-JSON payload with a per-tenant data key; the
data key is wrapped by a master key (production: KMS; dev/compose: the
``WOLOLO_EVALS_MASTER_KEY`` env value) and stored in a local keyring file.
``payload_key_id`` names the wrapping key so rotation is a re-wrap, not a
re-encrypt of history; ``payload_sha256`` verifies integrity after decrypt.
There is no plaintext payload column anywhere, and payload content never
appears in audit rows, OTEL attributes, or logs.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import secrets
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import canonical_payload_sha256

_NONCE_LEN = 12


class PayloadIntegrityError(Exception):
    """Decrypted payload does not match the recorded payload_sha256."""


class EnvelopeCipher:
    """Per-tenant data keys wrapped by one master key.

    Keyring file shape: ``{tenant_id: {"key_id", "nonce_hex", "wrapped_hex"}}``.
    ponytail: JSON-file keyring with a process lock; swap the wrap/unwrap pair
    for a KMS client when production key management lands (same interface).
    """

    def __init__(self, master_key_hex: str, master_key_id: str, keyring_path: str):
        key = bytes.fromhex(master_key_hex)
        if len(key) != 32:
            raise ValueError("master key must be 32 bytes (64 hex chars)")
        self._master = AESGCM(key)
        self._master_key_id = master_key_id
        self._path = pathlib.Path(keyring_path)
        self._lock = threading.Lock()

    # -- keyring ------------------------------------------------------------
    def _load(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _save(self, ring: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(ring, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    def _flock(self):
        """Exclusive lock on a sidecar file, so the load-modify-save of the
        ring is atomic across service processes, not only across threads."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _data_key(self, tenant_id: str) -> tuple[bytes, str]:
        with self._lock:
            fd = self._flock()
            try:
                return self._data_key_locked(tenant_id)
            finally:
                os.close(fd)  # closing releases the flock

    def _data_key_locked(self, tenant_id: str) -> tuple[bytes, str]:
        ring = self._load()
        entry = ring.get(tenant_id)
        if entry is None:
            data_key = AESGCM.generate_key(bit_length=256)
            nonce = secrets.token_bytes(_NONCE_LEN)
            wrapped = self._master.encrypt(nonce, data_key, tenant_id.encode())
            entry = {
                "key_id": self._master_key_id,
                "nonce_hex": nonce.hex(),
                "wrapped_hex": wrapped.hex(),
            }
            ring[tenant_id] = entry
            self._save(ring)
            return data_key, entry["key_id"]
        data_key = self._master.decrypt(
            bytes.fromhex(entry["nonce_hex"]),
            bytes.fromhex(entry["wrapped_hex"]),
            tenant_id.encode(),
        )
        return data_key, entry["key_id"]

    # -- payload envelope ---------------------------------------------------
    def encrypt_payload(self, tenant_id: str, payload: dict) -> tuple[bytes, str, str]:
        """Returns ``(ciphertext, payload_key_id, payload_sha256)``.

        Ciphertext layout: 12-byte nonce || AES-256-GCM ciphertext+tag, AAD is
        the tenant_id so a ciphertext cannot be replayed under another tenant.
        """
        data_key, key_id = self._data_key(tenant_id)
        sha = canonical_payload_sha256(payload)
        plaintext = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_LEN)
        ct = AESGCM(data_key).encrypt(nonce, plaintext, tenant_id.encode())
        return nonce + ct, key_id, sha

    def decrypt_payload(
        self, tenant_id: str, ciphertext: bytes, expected_sha256: str
    ) -> dict:
        data_key, _ = self._data_key(tenant_id)
        nonce, ct = ciphertext[:_NONCE_LEN], ciphertext[_NONCE_LEN:]
        plaintext = AESGCM(data_key).decrypt(nonce, ct, tenant_id.encode())
        payload = json.loads(plaintext.decode("utf-8"))
        if canonical_payload_sha256(payload) != expected_sha256:
            raise PayloadIntegrityError(
                "decrypted payload hash does not match payload_sha256"
            )
        return payload
