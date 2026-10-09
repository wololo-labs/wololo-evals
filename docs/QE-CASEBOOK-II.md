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
