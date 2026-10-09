# A QE casebook the no-skill arm fails

The `qe-skill` casebook cannot currently tell a test-authoring skill apart from
a model that already writes tests. This is the design for one that can. It is a
design, not a built casebook: building and running it needs its own spend
approval.

## Why the current one cannot

Measured twice, live, against `examples/fixtures/quoting-service`:

| | No skill | Skill, normal | Skill, forced |
|---|---:|---:|---:|
| benchmark score | 0.5641 | 0.5644 | 0.8351 |
| mutation kill rate | 0.3103 | 0.3201 | 0.2916 |
| seeded defects caught | 0.4318 | 0.4545 | 0.4250 |
| fired on the 11 testing cases | n/a | 0 of 11 | 11 of 11 |

Three things that casebook proved about itself:

1. **The no-skill arm already passes.** Asked for pytest tests over a stdlib
   pricing service, the model writes reasonable ones. A skill has nothing left
   to add, so no honest arm can beat the control by much.
2. **Nothing routes to the skill.** Routing precision 0.00. The task does not
   feel hard enough to reach for help, so the skill is never consulted.
3. **The composite rewards activation over quality.** Forcing invocation moved
   the composite +0.2710 while every quality signal fell. A score that can move
   that far on activation alone is not measuring test quality.

## What a discriminating case needs

A case belongs in casebook II only if the control arm **fails it**, for a reason
a written procedure fixes. Four shapes qualify:

| Shape | Why the control fails | What a skill supplies |
|---|---|---|
| Defect reachable only through a specific sequence | The obvious test exercises one call; the defect needs state built up across several | A procedure that says to drive the state machine, not the function |
| Boundary buried behind a conversion | The model tests the value it was handed, not the value after rounding, truncation or unit change | An instruction to pin the boundary on both sides of every conversion |
| Failure path with a silent fallback | A wrong value is returned rather than raised, so an assertion on "no exception" passes | An instruction to assert on values, never on the absence of a throw |
| A suite that already passes while the code is wrong | The model extends a suite whose existing assertions are tautological, and inherits them | An instruction to read the existing suite critically before adding to it |

Deliberately excluded: anything a stronger model simply solves. A case the
control passes measures the model, not the asset, and belongs in a capability
benchmark instead.

## Build criteria

1. **Calibrate the control first.** Run the no-skill arm before any asset
   exists. Keep only the cases it fails. A case the control passes is dropped,
   not kept for completeness.
2. **One seeded defect per case, declared.** Each case names the defect in
   `seeded_defects.json` that a competent suite must go red on, so the catch
   rate is per-case and not a fixture-wide average.
3. **Fixture large enough to hide a defect.** The quoting service is small
   enough to read in a single pass, which is why coverage is near-total by
   accident. Casebook II needs a module where a defect can sit outside the
   obvious path.
4. **Reweight or drop the activation slot.** Either score activation separately
   from quality, as the forced arm does here, or remove it from the composite.
   It must not be possible to move the headline by invoking a skill.
5. **Both arms, same model, same cases, isolated settings.** Non-negotiable
   after a first pair was measured against the operator's own 399 installed
   skills.

## How to tell it worked

Casebook II is ready when the no-skill arm scores materially below the pass
threshold, and the gap a forced skill arm closes is visible in
`mutation_kill_rate` and `seeded_defects_caught` rather than in the activation
slot.

## Calibration result: the control does not fail

Two attempts, both live, both with the CLI pinned to project settings.

| Attempt | Cases | Control score | Control's defect catch |
|---|---|---:|---:|
| Slice named in the prompt | 6 | 0.8669 | every targeted defect caught |
| Unguided, scored on all six defects | 4 | 0.8308 | 5 of 6, measured directly |

The first attempt had an obvious flaw: naming the slice to test hands over the
defect's location, so a case about rounding gets a rounding boundary test. The
second removed that, and asked what a reviewer asks ("this suite is thin,
strengthen it"). The control still caught five of the six planted defects,
against zero for the suite the fixture ships.

Along the way the control's own tests found two real bugs in this fixture, a
reversal double-counted in `recompute` and asymmetric rounding in `convert`,
which is itself evidence about the control: it was not guessing.

So the precondition is still unmet, and the reason is not that the fixture is
too easy in the ordinary sense. Three fixtures now agree on the same thing:
**a strong model writing tests over small, conventional Python is already
near the ceiling this benchmark can measure.** There is no headroom for a
test-authoring skill to occupy, in either direction.

## What is left, honestly

Only two routes remain, and the first is not a benchmark change at all.

1. **Hold the arms' model fixed but lower it.** A skill that lifts a weaker
   model is a real result about the asset, measured the same way: same model
   both arms, skill against no skill. `claude-haiku-4-5` is in the engine's
   allow-list and cheap. This is the honest version of "does the asset add
   anything", and it stops measuring the frontier model's own competence.
2. **Change the domain, not the difficulty.** Test authoring over stdlib
   Python is something the model does well unaided. A domain where it does not
   (an unusual DSL, concurrency ordering, hardware timing, a legacy framework
   with surprising semantics) would leave room for written procedure to help.
   That is a new fixture and a new casebook, not a tweak to this one.

What should not happen: continuing to adjust this casebook until the asset
looks good. Three calibration runs have said the same thing, and the finding
belongs in the record rather than being engineered away.
