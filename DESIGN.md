# t design system

`t` has a terminal design system. Its authoritative guidance is the
[command and terminal contract](ui/README.md), including visual styling,
interaction profiles, keyboard controls, terminal sizing, and planning previews.

- [Command inventory](ui/command-contract.json): command modes and their tests.
- [Shared implementations](ui/README.md#shared-implementations): `Style` and
  `_RailUI` in `bin/t`, and the picker profiles in `ui/fzf.sh`.
- [Planning previews](ui/README.md#planning-previews): faithful, isolated prototypes
  of proposed command behavior and the separate preview of implemented commands.

Reuse these conventions and implementations when adding or improving commands.
Workflow configuration and explicit task instructions determine preview timing,
approval, and delivery; this entry point does not create additional approval gates.

## CLI previews

Build every preview against the current worktree's
[command and terminal contract](ui/README.md) and the shared renderer used by the
command. Match its layout, spacing, colors, focus and selection markers, footer
hints, scrolling, and behavior at narrow widths and short heights. Use the same
hotkeys, platform key labels, and interaction rules as the command, including
any proposed changes to those bindings.

Make the preview a complete, keyboard-operable TUI. Start at the real command or
parent menu and support the full affected flow, including movement, selection,
editing, review, confirmation, Back, and cancellation. Keep sibling navigation
working and restore focus and selections when users go back or resize. Cover
relevant empty, error, and retry states. Keep sample data, scenario controls, and
simulated external effects clearly labeled and separate from the terminal UI.

Before sharing, complete the flow using the documented keys at normal and
compact terminal sizes. Verify that each advertised binding works, that the
focused row and controls stay reachable when the viewport changes, and that
review and cancellation behave as shown. Follow the
[planning preview contract](ui/README.md#planning-previews) when a workflow
calls for a planning prototype.

## Vertical spacing

Use spacing to show which controls belong together. Keep a label and its short
description or current value on the same line when there is room. If the text
wraps, put it on the next line without a blank row. Keep related controls and
table rows contiguous.

Give each distinct group one blank terminal row of separation at normal height.
Apply this between picker controls, the legend, the result shortcuts and the
table. In settings lists, use one blank row before a collection-level action
such as **Add custom stage**. Count that row once when it also ends a group.
Use the terminal line height for this spacing in HTML previews.

Base vertical spacing on available height. A narrow terminal wraps labels and
values while keeping group breaks if there is room. On short terminals, reduce
these gaps first so the focused row, its context and primary shortcuts remain
visible together.

## Picker design reference

Use the running `t resume` and the [interactive HTML reference](ui/previews/picker.html)
for the full picker layout. Generate the standalone reference from this checkout:

```sh
python3 scripts/preview_picker.py --output /tmp/t-picker.html
```

Open the generated file for the model library or add `?view=resume` for the
matching resume fixture. The preview bundles public catalog metadata and synthetic
session, installation, and verification state. Native captures and the browser
resume example use the same fixture and render their controls from the same
Python functions. Keep scenario selection outside the terminal frame.

Start with the query, count, and refresh row. Follow with the filter summary
and editor, sort summary and shortcuts, legend, result shortcuts, column
headings, and results in that order. In the full layout, leave exactly one blank
row between refresh and filters, filters and sort, sort and legend, legend and
result shortcuts, and shortcuts and columns. Keep headings immediately above the first result and keep
individual rows contiguous. Omit a section's dedicated gap when the section is
absent. Use one terminal line height for each full-size gap in HTML. On a short
screen, give space back in the resume renderer's priority order before dropping
useful controls. Narrow widths wrap within a section while keeping its breaks
when height permits.

Put the green Ctrl-U refresh action next to refresh status in resume. Active
refresh uses bright cyan. Where there is room, show the previous refresh time
and host outcomes. Current hosts use a green ✓, failed connections a red ×,
and failure reasons amber. Retain a legacy result-count fallback for older fzf.

Place active filter values and their shortcuts on a summary row. Alt-F opens the
inline editor in resume and the model reference. Ctrl-F pauses and restores
filters, Ctrl-X resets filters, Ctrl-S pauses and resumes sorting, and Ctrl-Y
cycles sort choices. Preserve query, focus, and applicable marks through
disclosure and resizing. Keep existing picker-specific hotkeys and pagination.
Use macOS key labels on macOS and the compact labels when space is tight.
Each picker supplies the facets, sort choices, and actions that apply to it.

Order the legend by table column and use matching glyphs, colors, and meanings
in rows. In resume, the groups are Status, PRs, and Agent. Status shows attached
(●), last verified close (↶), work in progress (↻), and verified preview (↗).
Keep the focus pointer, selection ✓, and status markers in distinct cells.
Keep columns aligned when sorting and filtering. On horizontal scroll, headings
and values move together while the query, controls, and marker gutter stay fixed.
Over the table, wheel up pans left and wheel down pans right. Show `<` and `>`
only where content is hidden. Search still matches offscreen text. Return or
Enter, Esc, Tab, and Ctrl-B appear in the resume result shortcut row.

### Model library reference

The full catalog starts alphabetically. Sorting is optional. Known memory
nonfits are hidden initially, and unknown fits remain visible. The model
summaries omit “All models,” “Snapshot order,” and long descriptions of
active filters. Keep Install and Verify in the result shortcuts and keep
the main listing focused on model data.

The state marker distinguishes Available (○), Downloaded (↓), Verified (◇),
and Ready (●). Verified represents a past successful check for that artifact
and runtime. Ready requires valid evidence for the current settings. The
memory column shows only ✓ fits (green), ✕ blocked (red), or ? unknown (muted).
Use matching colors in the legend and review. Give each available benchmark
its own column and retain its provenance. Missing scores render as a dash.

In install and verification review, show the model, runtime, memory, size,
state, and applicable plan. Start on the primary action using the displayed
runtime. Offer Change runtime only for another eligible choice. With blocked
memory, show the reason and omit the install action. Keep a blank row below
Back. Review actions omit Details, Load this pair, and Use as default.
Success shows Ready for offline use, the exact model, its runtime, and the
return action. Failure shows the reason, retained files when applicable,
and Retry when available. The standalone examples cover all model and memory
states, runtime choices, and both outcomes.

### Managed session statusline

Use the two-row managed tmux layout. The first row carries session identity,
conversation title, and the Priority control. The second carries workflow
stages and models, with the active stage styling on the stage/model pair.
In workflow settings, show Standard and Priority swatches once next to the
stage name. Preserve user bindings, custom status content, and pane style
attributes. See the [workflow appearance contract](ui/README.md#workflow-settings-and-resets).

## Required design adherence CI

Run **Design adherence** before opening a design-related PR and record both pinned
fzf results in its description. Nightly CI and release validation run the same
matrix against the pinned commit. See [CI validation](docs/ci.md#required-design-adherence). `ui/layout-contract.json` lists the required rendered fixtures
for shared screen families and custom nested screens. Every command and
conditional mode in the command inventory declares a layout profile; `inherit`
means the mode uses its command's renderer guarantees. Machine-only modes and
external passthrough have explicit exclusions. New custom layouts must register
fixtures instead of claiming an unrelated shared renderer.

Run `python3 scripts/design_check.py check` to validate references and
`python3 scripts/design_check.py run` to exercise them. The gate rejects missing,
deselected, skipped, or failed required cases. Geometry fixtures assert column
coordinates, gutters, responsive stacking and frame budgets; real isolated tmux
captures exercise search/filter/sort/paging and resize. Failures retain frames,
expected/actual coordinates, JUnit output and an outcome manifest in
`artifacts/design/`. Update expectations only after reviewing a deliberate design
change; never regenerate them automatically to make CI pass. This complements
command behavior tests and the full Linux coverage gate.

Use [selective visual validation](docs/validation.md) to choose captures during
iteration while retaining every project-required rendered check at delivery.

Open picker actions include Ctrl-N for a fresh local session. Keep native key
labels, `Tab ✓` spacing, and the filled Filters ON and Sort labels. Place
result shortcuts below the filter and sort controls. Do not add a selectable fake session row for
New. Show the status legend in the bottom footer where supported; older fzf
keeps a compact header legend and short terminals prioritize rows and controls.
