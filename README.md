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
