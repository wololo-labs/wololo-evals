"""Shared test helpers: mint tokens with an ephemeral test issuer."""
from __future__ import annotations

import atexit
import base64
import json
import os
import tempfile
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KID = "dev-2026-07"
_ISS = "test-issuer"


def _base64url_integer(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_SIGNING_KEY = _PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_PUBLIC_NUMBERS = _PRIVATE_KEY.public_key().public_numbers()
_JWKS_FILE = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
json.dump(
    {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _KID,
                "n": _base64url_integer(_PUBLIC_NUMBERS.n),
                "e": _base64url_integer(_PUBLIC_NUMBERS.e),
            }
        ]
    },
    _JWKS_FILE,
)
_JWKS_FILE.close()
JWKS_PATH = _JWKS_FILE.name
atexit.register(lambda: os.unlink(JWKS_PATH) if os.path.exists(JWKS_PATH) else None)


def mint_token(
    tenant_id: str,
    features: list[str],
    *,
    iat: int | None = None,
    exp: int | None = None,
    plan: str = "enterprise",
    licence_id: str | None = None,
    iss: str = _ISS,
    kid: str = _KID,
) -> str:
    """Sign a schema-valid entitlement token. ``iat``/``exp`` overridable so a
    test can mint an already-expired token (both timestamps in the past)."""
    now = int(time.time())
    resolved_iat = now if iat is None else iat
    resolved_exp = now + 3600 if exp is None else exp
    claims = {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "licence_id": licence_id or f"lic-{tenant_id}",
        "plan": plan,
        "features": features,
        "iss": iss,
        "kid": kid,
        "iat": resolved_iat,
        "exp": resolved_exp,
    }
    return jwt.encode(claims, _SIGNING_KEY, algorithm="RS256", headers={"kid": kid})
