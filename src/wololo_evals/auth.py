"""Entitlement verification (invariant 4) on the imported identity family.

DOCTRINE 15.1: wololo-deploy (06a) is the SOLE real entitlement issuer; this
service never hand-rolls a token interface. Verification runs against a JWKS
file (fixture JWKS until 06a publishes its URL; the code path is identical,
only the key source swaps — named integration debt).

The resolved principal is the ONLY source of tenant identity for every route:
a ``tenant_id`` in a body or query never overrides it (SPEC S4.1). In-tenant
RBAC comes from the token's ``features`` claim (SPEC S12):
``wololo-evals:submit`` / ``wololo-evals:gatekeeper`` / ``wololo-evals:admin``.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

import jwt
from jsonschema import ValidationError

from .contracts.identity import is_expired, validate_entitlement_token

ROLE_SUBMIT = "wololo-evals:submit"
ROLE_GATEKEEPER = "wololo-evals:gatekeeper"
ROLE_ADMIN = "wololo-evals:admin"


class AuthError(Exception):
    """Any verification failure. Fail closed: callers map this to 401/403
    BEFORE any database write (the row-count-unchanged invariant tests prove
    the ordering)."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor: str  # licence_id: the server-derived actor written to audit rows
    features: tuple[str, ...]
    plan: str

    def require(self, role: str) -> None:
        if role not in self.features:
            raise AuthError(f"missing feature {role!r}", status=403)


class Verifier:
    def __init__(self, jwks_path: str, expected_iss: str):
        jwks = json.loads(pathlib.Path(jwks_path).read_text())
        self._keys = {
            k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
            for k in jwks["keys"]
        }
        self._expected_iss = expected_iss

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"malformed token: {exc}") from exc
        key = self._keys.get(header.get("kid", ""))
        if key is None:
            raise AuthError(f"unknown kid {header.get('kid')!r}")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self._expected_iss,
                options={"require": ["exp", "iss"]},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"token rejected: {exc}") from exc
        try:
            validate_entitlement_token(claims)
        except ValidationError as exc:
            raise AuthError(f"claims failed identity schema: {exc.message}") from exc
        if is_expired(claims):
            raise AuthError("token expired")
        return Principal(
            tenant_id=claims["tenant_id"],
            actor=claims["licence_id"],
            features=tuple(claims["features"]),
            plan=claims["plan"],
        )


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("missing bearer token")
    return authorization[len("Bearer ") :]
