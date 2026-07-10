"""Shared test helpers: mint entitlement tokens signed by the fixture issuer.

The fixture JWKS (``dev-jwks.json``) + private key (``dev-signing-key.pem``)
stand in for wololo-deploy 06a, the sole real issuer. The signing path here is
identical to production; only the key source swaps when 06a ships (SPEC S3.3).
"""
from __future__ import annotations

import pathlib
import time

import jwt

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
JWKS_PATH = str(FIXTURES / "dev-jwks.json")
_SIGNING_KEY = (FIXTURES / "dev-signing-key.pem").read_text()
_KID = "dev-2026-07"
_ISS = "wololo-deploy"


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
