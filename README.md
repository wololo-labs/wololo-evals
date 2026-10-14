# wololo-evals

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

wololo-evals is an alpha Python service and CLI for evaluating agent assets. It
runs reproducible benchmark cases, compares candidate and baseline results, and
emits a gate verdict.

## Status

This repository is under active development. The offline replay and gate paths
are covered by automated tests. Live model runs, external identity issuers, and
deployment integrations require local configuration and are not exercised by
the default test suite.

## Install

Requires Python 3.11 or newer.

```console
pip install .
wololo-evals-cli --help
```

## Quick start

Run the bundled, offline regression example:

```console
wololo-evals-cli gate \
  --run examples/runs/dropdowns-synthetic/candidate-v2 \
  --baseline examples/runs/dropdowns-synthetic/baseline-v1 \
  --benchmark examples/runs/dropdowns-synthetic/benchmark \
  --golden-agreement 1.0
```

The example intentionally produces a regression verdict and exits with status
`1`. That behavior verifies the gate, not a successful evaluation.

## Evaluate a skill

Declare the evaluation once, in an `eval:` block of the pack's `apm.yml`:

```yaml
eval:
  asset: .apm/skills/my-skill   # relative to apm.yml; optional if the pack has one skill
  benchmark: bench/             # a directory with benchmark.json, or a built-in name
  engine: claude
  model: claude-sonnet-5-5
  max_cost_usd: 1.00            # a live run refuses without a cap
  thresholds: { pass: 0.7, max_regression: 0.0 }
  report: eval-gate-report.json # optional; default beside apm.yml
```

```console
wololo-evals-cli run --config apm.yml --replay replay   # recorded runs, no spend
wololo-evals-cli run --config apm.yml                   # live, under max_cost_usd
```

The gate report lands beside `apm.yml`; the measured result is never written
into the manifest. Unknown or missing keys are refused by name. A standalone
`eval.yaml` (the same keys at the top level) still works with a deprecation
notice, and an `eval:` block in a sibling `apm.yml` wins over it.
[`examples/hello-skill`](examples/hello-skill) is a complete offline example.

To gate pull requests, use the action in this repository:

```yaml
- uses: wololo-labs/wololo-evals@main
  with:
    config: apm.yml
    replay: replay   # recorded archive scored on pull requests, at zero spend
```

On `workflow_dispatch` it runs live under the cost cap and records an archive
to commit for the next pull request.

## Development

```console
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Some integration tests need Docker or externally configured credentials and
skip when those prerequisites are unavailable.

The Compose stack requires an issuer JWKS and issuer identifier. Set
`WOLOLO_EVALS_JWKS` to a local public JWKS file and `WOLOLO_EVALS_ISSUER` to
its issuer identifier before running `docker compose up`. The file is mounted
read-only into the API and worker containers. No private signing key ships
with this repository.

## Security

Use GitHub's private vulnerability reporting for security reports. Do not open
a public issue containing a credential or sensitive evaluation data.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Copyright 2026 Steven Gonsalvez. Licensed under the
[Apache License, Version 2.0](LICENSE).
