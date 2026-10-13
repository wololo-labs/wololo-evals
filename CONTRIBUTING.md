+# Contributing to wololo-evals

Thanks for contributing.

## Before opening a pull request

1. Create a focused branch from `main`.
2. Keep commits small, signed, and written in Conventional Commit style.
3. Run relevant tests and formatters locally.
4. Do not commit credentials, private endpoints, personal paths, generated logs,
   or real evaluation data.
5. Describe behavior changes and verification in the pull request.

## Development setup

Requires Python 3.11 or newer.

```console
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Some tests require Docker or optional external tools. Tests with unavailable
prerequisites should report a clear skip rather than silently pass.

## Reporting bugs

Use a GitHub issue for reproducible non-security bugs. For security concerns,
follow [SECURITY.md](SECURITY.md).
