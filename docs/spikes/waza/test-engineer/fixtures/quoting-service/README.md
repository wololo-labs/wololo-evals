# quoting-service

The code under test for the `qe-skill` benchmark: a parcel quoting service in
about 190 lines of standard library Python, plus the suite the repository
"already had".

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
in milliseconds, which is what makes it usable as a mutation target that gets
rebuilt and re-run a few hundred times per gate run.

## Layout

| Path | What it is |
| --- | --- |
| `quoting/pricing.py` | Validation and the pricing rules. Pure functions over plain values. |
| `quoting/service.py` | The HTTP layer: routing, status codes, JSON bodies. |
| `tests/test_quotes.py` | The shipped suite. This is the **no-agent baseline arm** of the benchmark. |
| `seeded_defects.json` | Named defects the harness plants to ask whether a suite goes red. |
| `changed_lines.json` | The lines under review, for the rubric's changed-line coverage. |

## The shipped suite is the point

`tests/test_quotes.py` is what a reviewer would wave through: seven tests,
happy paths for each destination zone, the express multiplier, two rejections.
It runs green, it reads fine, and it catches **one of the four seeded defects**.

```console
$ cd examples/fixtures/quoting-service && PYTHONPATH=. python -m pytest tests -q
7 passed
```

That gap is the benchmark. A QE skill is worth something if the suite it writes
closes it, and the gate measures that three ways, all mechanically and all at
gate time:

| Signal | Shipped suite | What it asks |
| --- | --- | --- |
| `mutation_kill_rate` | 0.237 | of every mutant of `quoting/`, how many does the suite notice |
| `seeded_defects_caught` | 0.250 | of the four named defects, how many does it go red on |
| `qe_rubric` | 0.878 | assertion strength, flake resistance, changed-line coverage, absence of tautologies |

## The fixture ships correct

The defects live in `seeded_defects.json`, not in the code, because mutation
analysis needs a green baseline: a suite that cannot run cannot be scored. Each
defect is an exact-string edit applied to a staged copy, so planting one is
deterministic and shows up in a diff. The headline defect,
`non-positive-weight-accepted`, drops the lower bound on `weight_grams` so a
-500g parcel is quoted at the zone base rate.

An edit whose `find` text no longer matches is a loud failure, not a silently
uncaught defect, so a manifest that drifts from the fixture fails the gate.

## Running it

```console
$ cd examples/fixtures/quoting-service
$ PYTHONPATH=. python -m quoting.service      # serves on :8080
$ curl -s localhost:8080/quotes -d '{"weight_grams": 1500, "destination": "eu"}'
{"quote_pence": 1269, "weight_grams": 1500, "destination": "eu", "express": false}
```

Licensed Apache-2.0, like the rest of the open surface.
