# specs/eval-candidate

The run-input contract of the Wololo OS quality gate (04-wololo-evals SPEC
Section 3.1). One kind, `eval-candidate`, `schemaVersion: 1`: the thing being
evaluated — a skill, prompt, harness config or model config — produced by
`autoresearch` (goal 09), any other machine producer, or a human via CLI, and
consumed by `wololo-evals` at `POST /api/v1/candidates`.

This directory publishes **Apache-2.0** (DOCTRINE Section 11: the eval spec +
schemas are open for credibility; the `wololo-evals` service itself is closed,
proprietary and entitlement-keyed). Builders live in
`core/contracts/eval_candidate.py`; registry rows in
`core/contracts/version.py`; drift tests in `tests/test_contract_schemas.py`
and `tests/test_eval_contract.py`.

## Semantics that are part of the contract

- **Fail-closed required fields.** `tenant_id`, `candidate_type`,
  `candidate_ref`, `proposed_by`, `baseline_ref`, `payload` are required at
  submission; a missing field is a 400, never a defaulted value (a missing
  `baseline_ref` is NEVER silently defaulted to "currently deployed").
- **`candidate_type`** is a closed enum (`skill`, `prompt`, `harness-config`,
  `model-config`); an unknown kind is a 400 at ingest, never routed to a
  default suite.
- **`payload_sha256`** is sha256 over the canonical-JSON payload (sorted keys,
  compact separators, UTF-8, no ASCII escaping): the content address for
  dedupe, derived blob keys, and post-decrypt integrity. The payload travels
  in the clear only on the TLS wire; the service persists it
  envelope-encrypted (AES-256-GCM) with no plaintext column.
- **`proposed_by`** on the persisted row is server-derived from the verified
  caller identity; a body-supplied value never overrides it.

## Test vectors

`test-vectors/eval-candidate/`: `invalid-` prefixed vectors MUST fail schema
validation, all others MUST pass. Every invalid vector isolates a single
violation. Conformance vectors for producing harnesses (Codex, Copilot,
Gemini, NanoClaw, OpenClaw) accrete here per the provider coverage policy.
