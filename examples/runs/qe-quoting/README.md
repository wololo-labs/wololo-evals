# qe-quoting: the recorded QE corpus

Two recorded arms of the `qe-skill` benchmark against
`examples/fixtures/quoting-service`, replayable offline at zero spend.

```
┌──────────────────┐        ┌───────────────────┐        ┌────────────────┐
│ baseline-noagent │───┐    │ ReplayAdapter     │        │ gate           │
│ shipped suite    │   ├───▶│ re-emit raw log   │───────▶│ paired         │
│ nothing authored │   │    │ restore workspace │        │ bootstrap      │
└──────────────────┘   │    └───────────────────┘        │ + signal table │
┌──────────────────┐   │              │                  └────────────────┘
│ candidate        │───┘              ▼
│ generated tests  │        ┌───────────────────┐
└──────────────────┘        │ production grader │
                            │ trace|code|judge  │
                            │ + mutation        │
                            │ + seeded defects  │
                            │ + qe-review       │
                            └───────────────────┘
```

## What is recorded and what is computed

The archive records what the **run** did. It does not record what the run
**scored**.

| Recorded (committed here) | Computed at gate time |
| --- | --- |
| `claude.raw.jsonl` - the engine's native stream | every `trace:` assertion, over the normalised trace |
| `workspace/tests_generated/*.py` - the suite the run wrote | the mutation kill rate against the fixture |
| `workspace/.gh-aw-advisory/output.json` - what the run reported | the seeded-defect catch rate |
| `provenance.json` - engine, model, token counts | all four `qe-review` rubric dimensions |

That split is the point. A corpus that recorded its own scores would prove only
that JSON can be copied. Re-running the gate re-mutates the fixture, re-plants
every seeded defect and re-runs the suites, so a change to a scorer, a rubric
weight or the fixture moves the number, exactly as it would on a live run.

The candidate's test files are committed source, not generated output: they are
the artefact under evaluation, and they want reviewing like any other code.

## Reproducing the gate

```console
$ wololo-evals-cli matrix --config examples/runs/qe-quoting/matrix-baseline.toml
$ wololo-evals-cli matrix --config examples/runs/qe-quoting/matrix-candidate.toml
$ wololo-evals-cli gate \
    --run      examples/runs/qe-quoting/out/candidate/iterations/001/matrix-<ts> \
    --baseline examples/runs/qe-quoting/out/baseline/iterations/001/matrix-<ts> \
    --signal-guard mutation_kill_rate \
    --signal-guard qe_flake_resistance \
    --signal-guard seeded_defects_caught
```

Both matrix runs together take about 100 seconds, nearly all of it mutation
analysis; the baseline arm is one analysis rather than twelve, because all
twelve of its cases share the same shipped suite and the grader caches on the
content hash of the target and tests.

`recorded/` holds that pair already run, so `gate` alone reproduces the verdict
from a fresh clone with no recomputation:

```console
$ wololo-evals-cli gate \
    --run      examples/runs/qe-quoting/recorded/candidate \
    --baseline examples/runs/qe-quoting/recorded/baseline \
    --signal-guard mutation_kill_rate --signal-guard qe_flake_resistance
```

`out/` is generated and git-ignored. `recorded/` is the pinned pair, carrying
the artefacts `gate` reads with the re-materialised workspaces stripped (they
are byte-identical copies of the archives above them).

## The arms

**`baseline-noagent/`** is the control: no skill, no authoring. Each case runs
the repository's shipped suite as it stands and reports it. The trace has one
`bash` call and no `write`, so `read_before_writing_tests` passes vacuously
while `ran_what_it_wrote` fails - which is the honest reading of an arm that
wrote nothing.

**`candidate/`** is a QE test-authoring skill: it activates, reads the pricing
and service modules and the existing suite, writes one test module for the
case's slice, runs it, and reports which behaviour it pinned and which input
would break it.

The twelfth case is a distractor - a licence question with no testing work in
it - where the correct behaviour is to answer and not activate. Both arms stay
silent, so it is the one case the gate classifies as `working` rather than
`fixed`.

Licensed Apache-2.0.
