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

## Positive control: first attempt invalid, rerun pending

A weaker-model pair was run and its numbers are withdrawn. During the no-skill
arm, one trial ran `git commit` in its workspace. The workspace sits inside
this repository, so the commit landed on the branch doing the measuring, and
what it committed was 87 generated tests written into the ledger fixture's own
shipped suite: the control baseline. Every case of the arm that ran afterwards
therefore materialised a fixture whose shipped suite was strong rather than
thin, which is exactly the variable the pair was meant to hold still.

The withdrawn figures, recorded here so the correction is legible rather than
silent: benchmark score 0.9731 to 0.9895, seeded defects caught 0.8333 to
1.0000, mutation kill rate 0.8030 to 0.8674, gate `pass` with
`significant_improvement: true`. They are not evidence of anything: the arms
did not run against the same code.

The commit is reverted, the fixture's shipped suite is thin again and catches 0
of 6 planted defects, and each case workspace now gets its own git repository
so a trial's commands cannot reach this repository. The pair is being rerun on
that footing.
