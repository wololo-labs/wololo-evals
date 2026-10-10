# quoting-service

The code under test: a parcel quoting service in about 190 lines of standard
library Python, plus the tests that ship with it.

```
┌──────────────────┐   ┌───────────────────┐   ┌──────────────────┐
│ POST /quotes     │──▶│ parse_quote_      │──▶│ quote_pence      │
│ GET  /health     │   │ request           │   │ zone + rounding  │
│ service.py       │   │ validation        │   │ + surcharge      │
└──────────────────┘   └───────────────────┘   └──────────────────┘
                                │                        │
                                ▼                        ▼
                       QuoteRequestError          price in pence
                       400 + offending field
```

No framework, no database, no network calls: the whole thing starts in-process
in milliseconds.

## Layout

| Path | What it is |
| --- | --- |
| `quoting/pricing.py` | Validation and the pricing rules. Pure functions over plain values. |
| `quoting/service.py` | The HTTP layer: routing, status codes, JSON bodies. |
| `tests/test_quotes.py` | The shipped tests. |

## Running it

```console
$ cd docs/spikes/waza/find-missing-tests/fixtures/quoting-service
$ PYTHONPATH=. python -m quoting.service      # serves on :8080
$ curl -s localhost:8080/quotes -d '{"weight_grams": 1500, "destination": "eu"}'
{"quote_pence": 1269, "weight_grams": 1500, "destination": "eu", "express": false}
```

Licensed Apache-2.0, like the rest of the open surface.
