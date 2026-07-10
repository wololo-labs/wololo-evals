"""Builders + validation helpers for the identity contract family.

DOCTRINE Section 15.1: ONE identity/entitlement contract family for the whole
corpus. ``specs/identity/schemas/entitlement-token.schema.json`` and
``specs/identity/schemas/agent-identity.schema.json`` are the wire shapes;
wololo-deploy (goal 06a) is the sole real entitlement-token ISSUER and
wololo-broker (goal 03) is the agent-identity authority (SPIFFE). Every other
component imports this module and stubs the issuer. No component hand-rolls
its own token interface.

Builders are pure and stdlib-only, mirroring ``core/contracts/events.py``:
required-field ``ValueError`` validation at the boundary. The ``validate_*``
helpers load the packaged JSON schema and raise
``jsonschema.ValidationError`` on a non-conformant payload (jsonschema is
imported lazily so the builders stay dependency-free).

Expiry is semantic, not structural: an expired token/identity is schema-valid;
callers gate on :func:`is_expired`.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Mapping, Optional, Sequence

__all__ = [
    "PLANS",
    "build_entitlement_claims",
    "build_agent_identity",
    "validate_entitlement_token",
    "validate_agent_identity",
    "is_expired",
]

PLANS = ("evaluation", "team", "enterprise")

_SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[3] / "specs" / "identity" / "schemas"


def build_entitlement_claims(
    tenant_id: str,
    licence_id: str,
    plan: str,
    features: Sequence[str],
    *,
    iss: str = "wololo-deploy",
    kid: str,
    iat: Optional[int] = None,
    exp: int,
) -> dict:
    """Build a schema-valid entitlement-token claims payload.

    ``features`` may be empty (a valid token with no gated features), but every
    entry must be a non-empty string. ``iat`` defaults to now (epoch seconds).
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not licence_id:
        raise ValueError("licence_id is required")
    if plan not in PLANS:
        raise ValueError(f"plan {plan!r} not one of {PLANS}")
    features = list(features)
    if any(not isinstance(f, str) or not f for f in features):
        raise ValueError("features must be non-empty strings")
    if not iss:
        raise ValueError("iss is required")
    if not kid:
        raise ValueError("kid is required")
    resolved_iat = int(iat if iat is not None else time.time())
    exp = int(exp)
    if exp <= resolved_iat:
        raise ValueError(f"exp {exp} must be after iat {resolved_iat}")
    return {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "licence_id": licence_id,
        "plan": plan,
        "features": features,
        "iss": iss,
        "kid": kid,
        "iat": resolved_iat,
        "exp": exp,
    }


def build_agent_identity(
    tenant_id: str,
    agent_id: str,
    spiffe_id: str,
    harness: str,
    *,
    issued_at: Optional[int] = None,
    expires_at: int,
    attested_by: str = "wololo-broker",
) -> dict:
    """Build a schema-valid agent-identity document.

    ``harness`` is a free provider-neutral string (DOCTRINE Section 4); this
    builder never restricts it to an enum of provider names.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not agent_id:
        raise ValueError("agent_id is required")
    if not spiffe_id.startswith("spiffe://") or len(spiffe_id) <= len("spiffe://"):
        raise ValueError(f"spiffe_id {spiffe_id!r} must be a spiffe:// URI")
    if any(ch.isspace() for ch in spiffe_id):
        raise ValueError(f"spiffe_id {spiffe_id!r} must not contain whitespace")
    if not harness:
        raise ValueError("harness is required")
    if not attested_by:
        raise ValueError("attested_by is required")
    resolved_issued = int(issued_at if issued_at is not None else time.time())
    expires_at = int(expires_at)
    if expires_at <= resolved_issued:
        raise ValueError(f"expires_at {expires_at} must be after issued_at {resolved_issued}")
    return {
        "schemaVersion": 1,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "spiffe_id": spiffe_id,
        "harness": harness,
        "issued_at": resolved_issued,
        "expires_at": expires_at,
        "attested_by": attested_by,
    }


def _validate_against(payload: Mapping, schema_filename: str) -> None:
    # Lazy import: jsonschema is only needed by verifiers, never by builders.
    from jsonschema import Draft202012Validator

    schema = json.loads((_SCHEMA_DIR / schema_filename).read_text())
    Draft202012Validator(schema).validate(payload)


def validate_entitlement_token(payload: Mapping) -> None:
    """Raise ``jsonschema.ValidationError`` unless *payload* conforms to the
    packaged entitlement-token schema. Returns ``None`` on success."""
    _validate_against(payload, "entitlement-token.schema.json")


def validate_agent_identity(payload: Mapping) -> None:
    """Raise ``jsonschema.ValidationError`` unless *payload* conforms to the
    packaged agent-identity schema. Returns ``None`` on success."""
    _validate_against(payload, "agent-identity.schema.json")


def is_expired(payload: Mapping, now: Optional[float] = None) -> bool:
    """True when the token/identity has expired at *now* (epoch seconds).

    Reads ``exp`` (entitlement-token) or ``expires_at`` (agent-identity).
    A payload carrying neither raises ``KeyError`` — silence here would turn a
    malformed token into a never-expiring one.
    """
    if "exp" in payload:
        expiry = payload["exp"]
    elif "expires_at" in payload:
        expiry = payload["expires_at"]
    else:
        raise KeyError("payload has neither 'exp' nor 'expires_at'")
    reference = time.time() if now is None else float(now)
    return float(expiry) <= reference
