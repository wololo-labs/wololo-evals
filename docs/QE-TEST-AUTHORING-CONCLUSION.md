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

## Correction: the answer key was reachable, and about half the frontier runs read it

Found while building the next control. The fixture is copied into every case
workspace, and the defect manifest and changed-line map sat beside it. Their
rationales name the rules a good suite is meant to discover, and the runs could
open them. Counting tool calls that read them, per arm:

| Arm | Runs that opened the answer key |
|---|---:|
| quoting, no skill | 7 of 16 |
| quoting, skill natural | 6 of 12 |
| quoting, skill forced | 5 of 12 |
| ledger control | 1 of 4 |
| haiku, no skill | 0 of 10 |

What that does and does not change:

- It did not bias any comparison. The frontier arms read the key at about the
  same rate, so the deltas between them stand.
- It did inflate every frontier catch rate, including the control's, which
  weakens the "no headroom" reading drawn from them. The ledger control's
  "caught 5 of 6" was measured from a run that never opened the key, so that
  one figure stands; the per-case scores of the others do not.
- Haiku never opened it, so the weaker-model pairs were unaffected.

The files are now withheld from the workspace (`7896dda`); the scorers read the
committed copy benchmark-relative, so nothing else changed. Every frontier
figure above this section predates that fix.

## Fourth fixture: an invented protocol, answer key withheld, control still strong

Built as the briefed positive control: a frame protocol the model cannot know
(Skerry), with a reserved checksum value, a pad byte, a banned source address,
a no-reading sentinel, modular sequence order and a reset that opens a new
epoch. No rule appears in a comment, a prompt or a file name. A skill carrying
the spec was written and every claim in it was checked against the fixture.
The shipped suite catches 0 of 6 planted defects.

The no-skill frontier control, calibrated first and alone, with the key
withheld (0 of 4 runs touched it):

| Case | Defects caught |
|---|---:|
| strengthen the suite | 5 of 6 |
| the codec | 3 of 6 |
| pre-release review | 5 of 6 |
| inheriting the package | 6 of 6 |

Composite 0.9621. Every one of the six defects was caught in at least one case.
**The control did not fail, so the precondition is unmet and the skill arms
were not run.** About $2 of the round was spent on this calibration.

### Why it did not fail, and why that is structural

The protocol is invented, but the code that implements it is in the workspace.
The control read `frame.py` (once per case), saw `return 251 if c == 0 else c`,
and in two of four runs wrote a small search for an input that reaches that
branch. The rule was never in a comment and was recoverable anyway, because an
implementation is the most precise statement of its own rules.

That is a property of the design and not of this fixture. A skill that carries
a protocol's rules adds nothing a model with the source and a shell cannot
recover from the source. **Spec knowledge in a skill can only help when the
code is not available to read.**

### What would make the control fail

A black-box variant: the model gets the package as importable bytecode only, or
as an executable it can call but not read, while the scorers keep mutating the
real source benchmark-relative. Then the rules are not on the page and the only
way to test them is to know which inputs to probe, which is what the skill
supplies. That is a new materialisation step and a new fixture build, not a
tweak, and it needs its own budget. Model-driven decompilation is still
possible, so even that variant would need the control calibrated before it is
trusted.

### Round accounting

The round budget was 10 USD. The scaled haiku pair on casebook II, stopped and
not gated, cost 2.94 USD measured over 11 completed runs. The Skerry control
cost about 1.99 USD. Roughly 5 USD remains, unspent.

## Do not quote the earlier absolute catch rates

Every absolute seeded-defect catch rate in this document and on the pull
request that predates commit `7896dda` was measured while the answer key was
readable from the workspace, and roughly half the frontier runs read it. That
includes the control figures for casebook II and for the first Skerry fixture.
The deltas between frontier arms were not biased, because the arms read the key
at about the same rate, but the absolute rates are inflated and **are not to be
quoted**. One of them, the ledger control's "5 of 6", came from a run that never
opened the key; treat it as unquotable all the same, because the runs around it
did. The figures in the section below were measured with the key withheld and 0
of 9 runs touching it.

## Instrument validation: the gate detects a skill effect once the fixture allows it

The first Skerry control was readable: it found a visible branch in the source
and searched for an input to reach it. Skerry was rebuilt (`fe845c3`) so that no
rule can be recovered from the code. Every constant is invented and the code
has no special branches. Each of the six seeded defects changes a value that
encode and decode, or a conversion and its inverse, share, so the mutant still
round-trips. Before any model ran, the fixture was bracketed:

| Suite | Catches |
|---|---:|
| the shipped suite | 0 of 6 |
| round-trip tests only | 0 of 6 |
| tests built from the protocol's own vectors | 6 of 6 |

All three are green on the clean code. The skill carries the protocol's
constants and four known byte frames, and every claim in it was checked against
the fixture first, which caught one wrong claim in its own first draft.

Same frontier model on every arm, three unguided cases, one seed, workspaces
sealed from the host repository, answer key withheld:

| Arm | Defect catch per case | Mean | Skill invoked | Composite |
|---|---|---:|---:|---:|
| no skill | 0.50, 0.50, 0.00 | 0.333 | n/a | 0.8372 |
| skill, left to choose | 1.00, 1.00, 1.00 | 1.000 | 3 of 3 | 0.9816 |
| skill, named in the prompt | 1.00, 1.00, 1.00 | 1.000 | 3 of 3 | 0.9804 |

| Gate against the no-skill control | Delta | 95% CI | Significant improvement |
|---|---:|---|---|
| skill, left to choose | +0.1445 | [+0.0323, +0.2554] | yes |
| skill, named in the prompt | +0.1432 | [+0.0311, +0.2550] | yes |

The guarded signal moved the way the design predicts: seeded defects caught
0.333 to 1.000. The control caught the checksum constants and the field order,
which it pinned from exact output bytes, and never caught the fill byte, the unit
constant or the sequence space in any case. Evidence files are under
`examples/runs/qe-skerry-live/evidence/`.

### What this licenses

That the gate, its scorers and its guards can detect a skill effect when the
fixture leaves room for one. Together with the frontier results above, that
changes how the earlier flat results read: they were consistent with an
instrument that works but had no headroom to measure, not with a broken one.
This is also the first model-matched pair in slice C where the gate reports an
effect from a baseline that was not contaminated.

### What it does not license

- **Nothing about `test-writer-pack`.** This is a purpose-built skill carrying
  facts a model cannot know. The pack carries procedure a frontier model
  already follows. It stays unscored and its pull request stays unopened.
- **Nothing about breadth.** Mutation kill rate fell slightly (0.822 to 0.786
  natural, 0.788 forced) and the rubric composite fell a little (0.980 to 0.966
  and 0.947). The skill made the author pin the specified values and the
  suites were otherwise no stronger.
- **Not a robust interval.** The unit is the case, there are three of them, and
  the bootstrap resamples three pairs at one seed. The lower bounds sit at
  +0.03. Read this as the instrument showing a signal, to be replicated with
  more cases and seeds before it is treated as established. The budget for that
  was spent.
- **Not a catalogue badge.** This is instrument validation. Nothing here is an
  asset score.

### Round accounting

Round budget 10 USD, measured spend 8.94. The stopped scaled haiku pair cost
2.94, the first Skerry control 1.99, and the rebuilt Skerry control plus both
skill arms 4.01 over 9 runs. About 1 USD remains, too little to replicate.

## Replication of the Skerry result

**Instrument validation, not an asset score.** The three original cases were run
again, fresh and independent of the first run, across all three arms, with the
cases frozen. They are the pre-registered evidence. The calibration-selected
cases are not evidence and were not run.

**Verdict: the effect held in direction and in significance, shrank in size, and
is borderline at its lower bound.**

| Arm | Run | Defect catch | Mutation kill | Rubric | Skill invoked |
|---|---|---:|---:|---:|---:|
| no skill | first | 0.333 | 0.822 | 0.980 | n/a |
| no skill | rerun | 0.611 | 0.815 | 0.974 | n/a |
| skill, left to choose | first | 1.000 | 0.786 | 0.966 | 3 of 3 |
| skill, left to choose | rerun | 1.000 | 0.770 | 0.969 | 3 of 3 |
| skill, named in the prompt | first | 1.000 | 0.788 | 0.947 | 3 of 3 |
| skill, named in the prompt | rerun | 1.000 | 0.781 | 0.943 | 3 of 3 |

| Gate against the no-skill control, case as the unit | Delta | 95% CI | Significant |
|---|---:|---|---|
| first run, left to choose | +0.1445 | [+0.0323, +0.2554] | yes |
| first run, named | +0.1432 | [+0.0311, +0.2550] | yes |
| rerun, left to choose | +0.1238 | [+0.0058, +0.2375] | yes |
| rerun, named | +0.1228 | [+0.0069, +0.2348] | yes |

What held: the skill took defect catch to 1.000 in every case in both runs, was
invoked 3 of 3 when left to choose both times, and the gate reported a
significant improvement both times. The small cost to mutation kill rate
(about 0.04) and rubric reproduced too, so it is a real property of the skill
and not noise.

What shrank: the composite delta fell from +0.1445 to +0.1238, and the defect
catch gap from 0.667 to 0.389. The lower bound of the interval fell from +0.032
to +0.006, which is close enough to zero that three cases cannot be said to
exclude it comfortably.

Why it shrank: nothing about the skill arms changed. All twelve skill case-runs,
two runs by two arms by three cases, scored 1.000, so they sit at the ceiling and
cannot move. The whole difference is the control, which scored 0.333 the first
time and 0.611 the second on the same three cases. Per case, the control went
0.50, 0.50, 0.00 and then 0.83, 0.67, 0.33. **A single control run is a noisy
baseline here**, and the first one looks to have been a low draw. Pooling both
control runs gives about 0.47, against which the skill's gap is about 0.53.

What this does not settle: the unit is still three cases, so an interval of this
width is a signal that replicated once, not an established effect. It says
nothing about `test-writer-pack`, which stays unscored, and none of it is a
catalogue badge.

### Side note: every calibration run, disclosed

Five further candidate cases were calibrated on the control alone under a rule
fixed before any run: invalid if the control catches more than half of the six
defects. Case 07 was dropped, and 08 and 09 were written, after seeing the first
calibration, so **these cases are calibration-selected and are not evidence**.

| Case | Control catch | Outcome | Reason |
|---|---:|---|---|
| qe3-05 consumer contract | 0.833 | dropped | invalid by the rule; asked to pin behaviour, the control pinned exact bytes |
| qe3-06 gap review | 0.500 | valid, not run | kept, but plan Y runs the originals only |
| qe3-07 find the bugs | scorer error | dropped | the control wrote tests asserting a belief, red on the clean fixture; dropped after seeing it, on a ground the rule did not cover |
| qe3-08 maintainer | 0.667 | invalid | written after calibration, and still failed the rule |
| qe3-09 CI suite | 0.667 | invalid | likewise |

Only one of five candidates passed. Unguided prompts mostly get a control that
catches more than half the defects on this fixture, which is a finding about how
hard it is to write a discriminating case, and a warning that the three original
cases may have been unusually favourable draws on the control side.

### Round accounting

Round budget 8 USD, measured spend 6.89: calibration 1.51, second calibration
1.16, rerun 4.23 over nine runs. 1.11 USD is kept as reserve and unspent. No run
read the answer key.

## Audit after code review: did any Skerry run reach the answer key or the skill?

Review of the pull request found that trials ran with `--dangerously-skip-permissions`
and a working directory 16 levels below the repository root, so `../` reached the answer key, the skill file and the casebook. Confirmed
by resolving it from a real case workspace. Trials now run in a throwaway
directory outside the repository (`2aaf74e`), which removes the casual route and
is not a sandbox: an absolute path still works.

Because that could have changed the result, every Skerry run still on disk was
audited from its recorded tool calls, for any path outside its own workspace, any
`../`, any answer-key filename, any touch of `SKILL.md` outside the mounted copy,
and for answer-key or skill text in a tool result.

| Arm | Runs | Left workspace | `../` | Key file | Skill file | Key or skill text in a result | Retried |
|---|---:|---:|---:|---:|---:|---:|---:|
| first run, control | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| first run, natural | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| first run, forced | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| rerun, control | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| rerun, natural | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| rerun, forced | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| calibration runs | 5 | 0 | 0 | 0 | 0 | 0 | 0 |

**Nothing reached outside its workspace in any of the 22 runs, no control touched
the skill, and none retried, so the replication result is unchanged and was not
rerun.** The first pass of this audit reported nearly every run as reaching out;
that was a defect in the audit, which matched the combo directory name
`qe-skill-iii` inside every absolute path, and is recorded here because the
corrected check is what the table reports.

The same review fixed nine further defects, each as its own commit: retries now
start from a pristine workspace in both the matrix and the adapter, the forced
preamble stays off distractor cases, two changed-line manifests that listed lines
past the end of their files are corrected and the scorer now warns on that, the
Claude CLI floor is raised to the earliest release the changelog evidences for
`--setting-sources`, the dry run applies the expected-skill override, provenance
records the preamble, the override and the case filter, an unknown informational
event type is logged and counted instead of aborting a paid run, the publication
fields of a gate report are validated, and one list of scratch names is shared by
every tree copy.
