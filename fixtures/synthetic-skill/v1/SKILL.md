---
name: dropdowns
description: Select an option from any dropdown in the browser — native <select>, react-select-style custom widgets, and portal-rendered menus. Use when a task asks to choose, set, or pick a value from a dropdown, combobox, listbox, or option menu on a page.
version: 1
---

# Dropdowns

Dropdowns come in three shapes and each commits its value differently. Do not
assume geometry: option rows frequently paint late, so always re-measure after
opening.

## Procedure

1. **Screenshot first.** Take a screenshot to locate the trigger before you
   touch anything. Read the target coordinate off the current pixels, never
   off a remembered layout.
2. **Open the control.** Click the trigger (`click_at_xy`) to open the menu.
3. **Re-screenshot before choosing.** Take a *second* screenshot after the
   menu opens. Portal-rendered and custom menus insert their option rows on a
   delay, so the geometry you need does not exist at the instant of the open
   click. Only read the option coordinate from this fresh screenshot.
4. **Click the option.** Click the option row at the coordinate you just read.
   Every committing click must be preceded by a screenshot of the state you
   are clicking into.
5. **Verify the commit.** Screenshot once more (or read the result element) to
   confirm the page reflects the chosen value before reporting done.

## Rules

- Never set a value by scripting the DOM (`js()` with `.value = …` or
  `dispatchEvent`). That fakes the selection without exercising the real
  control and is forbidden — drive the widget the way a user would, with a
  screenshot-verified click.
- Re-screenshot after every open. Late-arriving option geometry is the single
  most common cause of a mis-click.
- If the second screenshot still shows a loading row, wait briefly and
  screenshot again before clicking.
