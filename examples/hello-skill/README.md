# hello-skill

The smallest complete `eval.yaml` setup: a one-file skill, a four-case
benchmark, and a recorded run of each arm so it works offline.

```console
$ cd examples/hello-skill
$ wololo-evals-cli run --config eval.yaml --replay replay
# Gate verdict: PASS
...
$ echo $?
0
```

It writes `eval-gate-report.json` and `eval-gate-report.md` next to
`eval.yaml`. Drop `--replay replay` to run live against the engine in
`eval.yaml`; that spends money and refuses without `max_cost_usd`.

```
skill/            the asset under test (SKILL.md)
benchmark/        cases.toml -> cases.jsonl (three greetings, one distractor)
replay/no-skill/  recorded control arm, one dir per case
replay/skill/     recorded skill arm
```

## What is recorded and what is computed

The recorded streams are hand-authored to the Claude Code `stream-json` shape,
not captured from a model run, and every `provenance.json` says
`model_id: synthetic:hand-authored`. They are answers to the trajectory
questions (did the skill activate, what did the agent write).

No score is recorded. The gate recomputes every number from the stream and
from the files each run left in its `workspace/`. `tests/test_hello_skill.py`
recomputes the task-outcome signals independently from `greeting.txt` and
requires the report to agree.
