# hello-skill

The smallest complete example. It answers one question the whole project exists
for: **does giving an agent this skill make it do better, or does it do just as
well without it?**

## The idea in plain words

A skill is a short instruction card you hand to an AI agent. This one is about as
small as a card gets (`skill/SKILL.md`):

> When asked to greet someone, write `greeting.txt` containing exactly
> `Hello, <name>!` and nothing else.

To find out whether the card helps, we give the same four small tasks to the agent
twice, once **without** the card and once **with** it, and compare.

| task | what we ask | what a good answer looks like |
|---|---|---|
| `hello-01-ada` | "Say hello to Ada." | `greeting.txt` holds `Hello, Ada!` |
| `hello-02-grace` | "Say hello to Grace." | `greeting.txt` holds `Hello, Grace!` |
| `hello-03-linus` | "Say hello to Linus." | `greeting.txt` holds `Hello, Linus!` |
| `hello-04-distractor` | "What is 2 + 2?" | answers 4, and the greeter card is **not** used |

The fourth task is a trap on purpose. A skill that jumps in where it does not
belong is a bug too, so the gate checks that it stays quiet.

## What happens in this example

```
              same 4 tasks
        ┌────────────┴────────────┐
        ▼                         ▼
   agent WITHOUT card        agent WITH card
   (recorded in replay/       (recorded in replay/
    no-skill/)                 skill/)
        │                         │
        └────────────┬────────────┘
                     ▼
        the gate scores both, compares them,
        and says PASS, REGRESSION or REFUSED
```

Without the card the recorded agent gets the idea but is sloppy: it writes
`Hello Ada` for Ada (no comma, no exclamation mark) and `hello, Grace!` for Grace
(lower case). Only Linus comes out right. With the card all three greetings are
exact and the agent visibly uses the skill. On the distractor neither agent uses
the skill, which is correct.

The gate turns that into numbers and a verdict: here **PASS**, because the
skill arm scores clearly higher and nothing got worse. The 95% interval around
the improvement is printed too, so you can see how sure the gate is.

## Try it (free, takes a second)

```console
$ cd examples/hello-skill
$ wololo-evals-cli run --config apm.yml --replay replay
# Gate verdict: PASS
...
$ echo $?
0
```

`--replay replay` scores the saved runs instead of calling a model, so it costs
nothing and works offline. It writes `eval-gate-report.json` (the file other
tools read, such as the Backstage hub) and `eval-gate-report.md` (the same thing
for people) next to `apm.yml`. Drop `--replay replay` to run live against the
engine named in `apm.yml`. That spends real money and refuses to start without
`max_cost_usd`.

## What is in this folder

```
apm.yml           the eval: block, which says how to evaluate (engine, cap, thresholds)
skill/            the instruction card under test (SKILL.md)
benchmark/        the four tasks (cases.toml, compiled to cases.jsonl) and how each is checked
replay/no-skill/  saved run of the agent without the card, one folder per task
replay/skill/     saved run of the agent with the card
```

## Read this before trusting the PASS

The saved runs are **hand-written**, not captured from a real model. Every
`provenance.json` says `model_id: synthetic:hand-authored`. This example shows
how the gate works and what its output looks like. It does not prove that any
real skill helps. For a real result, run it live or look at a real report such
as the one for `test-writer-pack`, which honestly shows no measured gain.

## What is recorded and what is computed

No score is recorded in the replay files. The gate recomputes every number from
the saved stream and from the files each run left in its `workspace/`.
`tests/test_hello_skill.py` recomputes the task-outcome signals independently
from `greeting.txt` and requires the report to agree.
