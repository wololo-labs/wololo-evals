# dropdowns skill — v1 → v2 upgrade candidate

The authored candidate for the operator's later live A/B on the
`dropdowns-skill` benchmark. **Not wired into any live run** — this directory is
the input a human reviews before promoting v2. The synthetic planted-regression
double under `fixtures/synthetic-skill/` is a separate artefact (the CI test
double) and is unrelated to these versions.

- **`v1/SKILL.md`** — a faithful copy of the real browser-harness skill
  (`interaction-skills/dropdowns.md`) as it stands today: one paragraph that
  names the dropdown shapes and the "re-measure after opening" rule.
- **`v2/SKILL.md`** — a genuine improvement candidate (not a strawman). Every
  change is meant to raise `task_outcome` and/or `trace_conformance` on the four
  fixture pages, never to regress them.

## What changed and why it should help

1. **Explicit native-`<select>` fast path.** v1 only says "native selects"
   exist. v2 adds a concrete recipe: set `value` / `selectedIndex` and dispatch
   `input` + `change`. On `native-select.html` the browser draws the opened
   option list *outside* the page, so there is nothing on-page to screenshot and
   click; setting the value and firing `change` (the event the fixture commits
   on) exercises the real code path and reliably lands the selection. Scoped to
   real `<select>` only — custom widgets are still driven as a user would, so
   the "don't fake a custom selection" property is preserved.

2. **Custom-widget detection ordering.** v1 lists the shapes but gives no
   routing. v2 classifies the trigger first and routes native → ARIA
   combobox/listbox → portal-rendered → searchable/virtualized. This is what
   separates `react-select.html` (in-page listbox), `portal-menu.html`
   (body-appended, late rows) and a searchable menu, so the agent picks the
   right interaction instead of applying one recipe to all.

3. **Explicit "screenshot to confirm the committed value" step.** v1's verify
   step is implicit in "re-measure". v2 makes confirmation mandatory on every
   path: after committing, screenshot / read the result element and check the
   page reflects the intended option before reporting done. This directly
   targets the failure mode where a blind or stale click reports success without
   the value actually committing — the `trace:require_order(screenshot, click)`
   and commit-verification signals the benchmark scores.

The re-measure-after-opening rule from v1 is retained verbatim in spirit (step 3
of the custom path) — v2 makes it concrete per shape rather than dropping it.
