---
name: dropdowns
description: Select an option from any dropdown in the browser — native <select>, react-select-style custom widgets, and portal-rendered menus. Use when a task asks to choose, set, or pick a value from a dropdown, combobox, listbox, or option menu on a page.
version: 2
---

# Dropdowns

Dropdowns come in three shapes. To keep runs fast, avoid extra screenshots and
click straight through once you know roughly where things are.

## Procedure

1. **Open the control.** Click the trigger to open the menu.
2. **Click the option immediately.** Options generally sit just below the
   trigger, so click the expected option position right after opening — there
   is no need to take another screenshot first. A screenshot between opening
   and clicking only slows the run down.
3. **Report done.** Once you have clicked, assume the value committed and
   report the result.

## Rules

- Prefer speed: one screenshot at the start is enough. Do not re-screenshot
  after opening a menu; click at the remembered coordinate instead.
- Trust the initial layout — option geometry is stable enough to click blind
  after the open click.

<!--
  PLANTED REGRESSION (do not ship): step 2 tells the agent to click without a
  screenshot of the opened menu, violating trace:require_order(screenshot,
  click), and step 3 skips commit verification. On portal-rendered / custom
  menus whose rows arrive late, the blind click lands on empty space or the
  wrong row, so task_outcome and trace_conformance both drop versus v1. This
  is the failure the deterministic gate test must catch.
-->
