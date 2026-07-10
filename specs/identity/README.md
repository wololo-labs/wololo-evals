# specs/identity

The corpus-wide identity/entitlement contract family (DOCTRINE Section 15.1).
There is exactly ONE such family: no goal, repo, or component hand-rolls its
own entitlement-token or agent-identity interface. Non-conformant ad-hoc
shapes (an `X-MC-Token`-style per-repo header contract, for example) are
contract violations, not local conventions.

## Kinds

| Kind | Schema | Builder (`core/contracts/identity.py`) |
|---|---|---|
| `entitlement-token` | `schemas/entitlement-token.schema.json` | `build_entitlement_claims` |
| `agent-identity` | `schemas/agent-identity.schema.json` | `build_agent_identity` |

Both carry top-level integer `schemaVersion` registered in
`core/contracts/version.py::_SCHEMA_VERSIONS` and enforced by
`tests/test_contract_schemas.py::test_schema_versions_match_registry`.

## Semantics (fixed forever by this family)

- **wololo-deploy (goal 06a) is the SOLE real issuer of entitlement tokens.**
  Every other component stubs the issuer and imports this contract. The wire
  shape is final; only issuance is stubbed until 06a lands.
- **wololo-broker (goal 03) is the identity authority for `agent-identity`**
  (SPIFFE workload identity). `spiffe_id` is a `spiffe://` URI; `attested_by`
  names the attesting authority.
- `harness` is a free provider-neutral string (DOCTRINE Section 4). It is
  never an enum of provider names; Codex, Copilot, Gemini, NanoClaw and
  OpenClaw identities are as conformant as Hermes ones.
- **Expiry is semantic, not structural.** An expired token or identity is
  schema-valid; verifiers gate on `core.contracts.identity.is_expired`
  (`exp` / `expires_at`, epoch seconds). A payload with neither expiry field
  raises rather than silently never expiring.
- Verifiers are deny-by-default: `validate_entitlement_token` /
  `validate_agent_identity` raise on any non-conformant payload, including a
  token with `tenant_id` stripped or a tampered `exp` type (see the
  adversarial cases in `tests/test_identity_contract.py`).

## Test vectors

`test-vectors/<kind>/*.json`, minimum five per kind. Names are load-bearing:
vectors prefixed `invalid-` MUST fail schema validation and the test walker
asserts they do; every other vector MUST pass. Mandated set per kind:
`valid`, `expired` (schema-valid, semantically expired), a third valid edge
(`missing-feature` for tokens: empty `features`; `valid-other-harness` for
identities: non-Hermes harness), `invalid-missing-tenant`,
`invalid-wrong-schema-version`.

These vectors are part of the published spec, not test scaffolding: the BSL
kernel, closed services, and third-party provider adapters all prove
conformance against the same bytes.

## Tenancy model (invariant 1, current state — intentional)

`tenant_id` is a REQUIRED field on both wire shapes, so every future consumer
inherits tenancy from the contract. Today the kernel itself runs
tenant-scoped by deployment plus auth token (one tenant per deployed
profile); body-level tenant enforcement on the non-event families arrives
with 01-wololo-control's tenancy work. That omission is deliberate and
documented here so it cannot be mistaken for an accident: the contract
already carries the field, the control plane enforcement lands with goal 01.

## Versioning

MAJOR/MINOR policy per `core/contracts/VERSIONING.md`. This family was seeded
pre-split (SHAPING Step 4.0) so it rides the `wololo-contracts` extraction
inside the byte-identity baseline.
