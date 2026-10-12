# specs/eval-gate-report

The verdict contract of a skill evaluation. `wololo-evals-cli run` writes it as
`eval-gate-report.json`; `wololo-evals-cli gate` writes the same shape (without
`mode`, `spend_usd`, `thresholds` and `scores`).

Anything that wants to show or act on a verdict, a skill catalogue or a
dashboard for instance, reads this file and validates it against
[`schemas/eval-gate-report.v1.schema.json`](schemas/eval-gate-report.v1.schema.json).
That schema is the whole interface: consumers do not import wololo-evals and
wololo-evals imports nothing from them.

## Versioning

`schema_version` is `"1.0.0"` and the v1 schema is frozen. A new field, a
renamed field or a changed meaning ships as `eval-gate-report.v2.schema.json`
with `schema_version` `"2.0.0"`, next to v1, never as an edit to v1. The schema
sets `additionalProperties: false` throughout so a producer that drifts fails
the drift test instead of reaching a consumer.

## Semantics

| Field | Meaning |
|---|---|
| `verdict` | `pass`, `regression` or `refused`; the CLI exits `0`, `1`, `2` |
| `reason` | empty on a pass; why not, otherwise |
| `run`, `baseline` | the skill arm and no-skill arm run trees, relative to the config |
| `stats` | paired-bootstrap delta of the skill arm over the no-skill arm; absent when refused |
| `signals` | per-evaluator mean in each arm, so a composite cannot hide a slipping measure |
| `subject` | repo, path and commit measured; present only when CI supplied them |
| `mode`, `spend_usd` | `replay` spends nothing; `live` records what the run cost |
| `scores` | mean benchmark score of each arm; `thresholds.pass` is the floor `scores.skill` must reach |

`test-vectors/` holds `valid-*` and `invalid-*` documents. `tests/test_gate_report_schema.py`
checks every vector, and validates the report a real run produces.

This directory publishes **Apache-2.0**.
