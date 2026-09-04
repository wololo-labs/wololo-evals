# qe-quoting-synthetic: the recorded QE corpus

Two recorded arms of the `qe-skill` benchmark against
`examples/fixtures/quoting-service`, replayable offline at zero spend.

> **The raw streams here are hand-authored fixtures, not captured model runs.**
> They are written to the Claude Code `stream-json` shape so the replay engine
> and the trace assertions have something deterministic to run against, and they
> say so in their own `provenance.json`: `model_id` is
> `synthetic:hand-authored`, and they carry no token counts, no timestamps and
> no session ids because no model produced them. The directory is named
> `-synthetic` for the same reason `dropdowns-synthetic` is.
>
> **The QE numbers are not fixtures.** The mutation kill rate, the
> seeded-defect catch rate and all four `qe-review` rubric dimensions are absent
> from this archive entirely and are recomputed at gate time by really mutating
> the fixture, really planting each defect, and really running the suites. That
> split is the whole design: see the table below.

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
| `claude.raw.jsonl` - a hand-authored stream in the engine's native shape | every `trace:` assertion, over the normalised trace |
| `workspace/tests_generated/*.py` - the suite the run wrote | the mutation kill rate against the fixture |
| `workspace/.gh-aw-advisory/output.json` - what the run reported | the seeded-defect catch rate |
| `provenance.json` - engine, model, token counts | all four `qe-review` rubric dimensions |

That split is the point, and it is what a hand-authored stream can and cannot
support. The streams settle the *trajectory* questions: did the skill activate,
did it read before writing, did it leave the code under test alone. Those are
fixture answers, authored to describe a run rather than captured from one, and
they should be read that way. Every *quality* question is settled by running
code against the artefacts: re-running the gate re-mutates the fixture, re-plants
every seeded defect and re-runs the suites, so a change to a scorer, a rubric
weight or the fixture moves the number, exactly as it would on a live run.

A corpus that recorded its own scores would prove only that JSON can be copied.

The candidate's test files are committed source, not generated output: they are
the artefact under evaluation, and they want reviewing like any other code.

## Reproducing the gate

```console
$ wololo-evals-cli matrix --config examples/runs/qe-quoting-synthetic/matrix-baseline.toml
$ wololo-evals-cli matrix --config examples/runs/qe-quoting-synthetic/matrix-candidate.toml
$ wololo-evals-cli gate \
    --run      examples/runs/qe-quoting-synthetic/out/candidate/iterations/001/matrix-<ts> \
    --baseline examples/runs/qe-quoting-synthetic/out/baseline/iterations/001/matrix-<ts> \
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
    --run      examples/runs/qe-quoting-synthetic/recorded/candidate \
    --baseline examples/runs/qe-quoting-synthetic/recorded/baseline \
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

**`candidate/`** describes a QE test-authoring skill: it activates, reads the
pricing and service modules and the existing suite, writes one test module for
the case's slice, runs it, and reports which behaviour it pinned and which input
would break it. The trajectory is authored; the test module at the end of it is
real code that really runs, and is what every QE number is computed from.

The twelfth case is a distractor - a licence question with no testing work in
it - where the correct behaviour is to answer and not activate. Both arms stay
silent, so it is the one case the gate classifies as `working` rather than
`fixed`.

Licensed Apache-2.0.
