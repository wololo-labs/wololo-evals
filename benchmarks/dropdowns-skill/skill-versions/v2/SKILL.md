# Dropdowns

Dropdowns come in four shapes and each commits its value differently: a native
`<select>`, an ARIA combobox / listbox custom widget, a portal-rendered menu
appended to `<body>`, and a searchable/virtualized menu whose rows page in.
Classify the control first, then take the cheapest path that genuinely commits
the value. Option geometry frequently paints late, so re-measure after opening
before you click.

## Classify before you act

Inspect the trigger's DOM once and route by type, in this order:

1. **Native `<select>`** — a real `<select>` element. Take the fast path below;
   do not try to screenshot-and-click the OS-drawn option list (the browser
   renders it outside the page, so there is nothing on the page to click).
2. **ARIA combobox / listbox** — `role="combobox"` / `role="listbox"` with the
   option rows rendered in-page (react-select-style). Drive it as a user would.
3. **Portal-rendered menu** — the trigger's `aria-controls` / menu is appended
   to `<body>` rather than nested under the trigger, and rows arrive on a delay.
   Open, wait for rows, then click.
4. **Searchable / virtualized** — a combobox that filters or windows its rows.
   Type to narrow, then treat the filtered list as an in-page listbox.

## Native `<select>` fast path

For a real `<select>`, set the value and dispatch the events the page listens
for, rather than pixel-clicking an OS menu you cannot see:

```js
const el = document.querySelector(selector);
el.value = optionValue;                 // or set el.selectedIndex
el.dispatchEvent(new Event('input',  { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
```

`change` is what a native `<select>` commits on, so this exercises the same code
path a human selection would. Then confirm the commit (below). This is only for
native `<select>`; for custom widgets, drive the real control — scripting a
custom widget's hidden state fakes the selection without exercising it.

## Custom / portal / searchable path

1. **Screenshot first** to locate the trigger; read the coordinate off the
   current pixels, never a remembered layout.
2. **Open the control** by clicking the trigger.
3. **Re-screenshot before choosing.** Custom, portal, and virtualized menus
   insert (or page in) their option rows on a delay, so the geometry you need
   does not exist at the instant of the open click. If the fresh screenshot
   still shows a loading/empty row, wait briefly and screenshot again. For a
   searchable menu, type to filter first, then re-screenshot the narrowed list.
4. **Click the option** at the coordinate read from that fresh screenshot.

## Confirm the committed value — always

Whatever the path, finish by taking a screenshot (or reading the bound result
element / the control's committed value) and checking that the page now shows
the option you intended. A click that landed on empty space or a stale row, or
a `change` event the page ignored, is only visible here. Do not report done
until the committed value is confirmed.
