# Test-authoring skills cannot be scored against a frontier model here

The conclusion of slice C's second-asset work. It is scoped deliberately: it is
about **one asset class, test-authoring skills, measured over conventional
Python, with a frontier model on both arms.** It says nothing about other asset
classes, other domains, or weaker models, and it must not be quoted as if it
did.

## What was measured

Three fixtures, every arm live, both arms always the same engine and model,
with the CLI pinned to project settings so neither arm could see the operator's
own installed skills.

| Fixture | Arms | Control | With the skill |
|---|---|---:|---:|
| quoting-service, casebook I | natural | 0.5641 | 0.5644 |
| quoting-service, casebook I | forced invocation | 0.5641 | 0.8351, **regression** on guarded signals |
| ledger-service, casebook II, slice named | control only | 0.8669 | not run: precondition unmet |
| ledger-service, casebook II, unguided | control only | 0.8308, caught 5 of 6 defects | not run: precondition unmet |

## The three findings

**The skill never fires on its own.** Routing precision 0.00: across eleven
testing cases the model never once invoked the skill, with only that skill
installed and nothing competing. Asked for pytest tests, a capable model simply
writes them. Both arms correctly stayed silent on the distractor.

**Forced to fire, it makes the tests slightly worse.** Mutation kill rate
0.3103 to 0.2916, seeded defects caught 0.4318 to 0.4250, assertion strength
and changed-line coverage both down. The gate returns regression on the guarded
signals. The composite rose +0.2710 anyway, entirely from the activation slot,
which is why casebook II scores quality only.

**There is no headroom to measure.** On casebook II the control, unaided and
unguided, caught five of the six planted defects where the shipped suite caught
none, and its own tests found two real bugs in the fixture. A skill cannot
visibly improve a control that is already close to the ceiling.

## What follows

This asset gets **no frontier score** in the catalogue. The gate report on the
pack's branch stays as the honest record of the run, the toolkit pull request
stays unopened, and nothing is published that would read as a recommendation.

What would change the answer, in order of evidence quality:

1. A domain the model is genuinely weak at unaided: concurrency ordering, an
   unusual DSL, hardware timing, a legacy framework with surprising semantics.
   New fixture, new casebook.
2. A weaker model on both arms, which asks a narrower question: can this
   instrument detect any skill effect at all. That is a positive control for
   the eval, not a score for the asset.

What does not follow: that skills are worthless, or that this one is. It says
the instrument cannot separate this asset from a frontier model on this kind of
code, and that an honest catalogue therefore has nothing to badge.

## Positive control: the instrument does work

Run after the conclusion above, on casebook II, `claude-haiku-4-5` on both
arms, skill against no skill, four cases each. **This is a check on the
instrument, not a score for the asset**, and it is not a catalogue number: an
asset is scored against the model a consumer would actually run.

| | No skill | With the skill | Delta |
|---|---:|---:|---:|
| benchmark score | 0.9731 | 0.9895 | +0.0163 |
| seeded defects caught | 0.8333 | 1.0000 | +0.1667 |
| mutation kill rate | 0.8030 | 0.8674 | +0.0644 |
| fired unprompted | 0 of 4 | 3 of 4 | |

Gate verdict `pass`, with `significant_improvement: true` and a 95% CI of
`[+0.0060, +0.0322]`, an interval that excludes zero. This is the first
model-matched pair in slice C where the gate has reported a real effect.

Two things that makes true, and one it does not.

It makes true that the gate, the scorers and the guards can detect a skill
helping when there is room for one to help: the same machinery that returned
`+0.0003` with a CI straddling zero on a frontier model returns a significant
improvement here, on the same casebook and fixture.

It also makes true that this pack's instructions are not inert. The weaker
model reached for the skill on three of four cases unprompted, where the
frontier model reached for it on none of eleven, and following it took the
defect catch rate to all six.

It does **not** make true that the pack earns a catalogue score. The asset is
badged against the model a consumer runs, and on that model it is still
indistinguishable from no skill at all. A lift on a cheaper model is a reason
to keep the asset and to say where it helps, not a score to publish.

One caveat about comparing down the table: the frontier casebook II control ran
before two bugs in the ledger fixture were fixed, bugs its own tests found, so
its mutation and defect figures are not comparable with the haiku pair. Both
haiku arms ran after the fix, on identical code, which is what makes the pair
valid as a control.
