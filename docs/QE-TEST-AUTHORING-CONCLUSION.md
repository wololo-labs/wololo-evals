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

## Positive control: rerun clean, and it does not demonstrate detection

A weaker-model pair, `claude-haiku-4-5` on both arms, casebook II, four cases
each, every workspace sealed from this repository. **A check on the instrument,
not a score for the asset**, and not a catalogue number under any reading.

| | No skill | With the skill | Delta |
|---|---:|---:|---:|
| benchmark score | 0.9716 | 0.9769 | +0.0053 |
| seeded defects caught | 0.7917 | 0.8750 | +0.0833 |
| mutation kill rate | 0.8210 | 0.8362 | +0.0152 |
| fired unprompted | 0 of 4 | 4 of 4 | |

Gate verdict `pass`, 95% CI `[-0.0014, +0.0154]`, `significant_improvement:
false`. Every direction is favourable to the skill and none of it is
significant.

### The first attempt said otherwise, and it was wrong

The withdrawn run reported `+0.0163` with a CI of `[+0.0060, +0.0322]` and
`significant_improvement: true`. That arm pair had been corrupted: during the
no-skill arm one trial ran `git commit` in its workspace, which sits inside
this repository, and committed 87 generated tests into the ledger fixture's own
shipped suite. Every case that ran afterwards started from a strong suite
rather than a thin one, so the two arms were not measuring the same code.

The commit is reverted, the shipped suite is thin again and catches 0 of 6
planted defects, and each case workspace now gets its own git repository
(`caf5aad`) so a trial's git commands land in a throwaway it owns. The honest
reading of the pair before and after: **the only model-matched "significant
improvement" slice C has produced came from a contaminated baseline, and it
disappeared when the contamination did.**

### So what does the instrument show

One thing it does show: the skill fires for this model. Four of four cases
unprompted, against none of eleven on the frontier model. Routing is a real,
measurable difference between models.

What it does not show, still: that the gate can detect a skill *effect*. Four
cases at one seed gives a confidence interval about 0.017 wide, which cannot
resolve a delta of 0.005. That is a power problem rather than a null result,
and the fix is more cases and more seeds rather than a different conclusion.
Until that is run, slice C has no model-matched pair, at any model strength,
where the gate reports an effect it can stand behind.
