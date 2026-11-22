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

## Picker filters and sorting

Interactive session filtering and sorting use the existing picker hotkeys.
For `t open` and `t resume`, extend those controls and preserve every existing
filter and sort choice. Ctrl-F toggles filters, Ctrl-X resets filters, Ctrl-S
toggles sorting, and Ctrl-Y cycles sort choices; use native Mac labels when appropriate.
Slot history retains its pagination keys and uses Ctrl-S/Ctrl-Y for sorting.
Reuse the shared control styles and two-space table gutters. Keep the focused
result visible in short terminals.
In the resume picker, place the green Ctrl-U refresh action beside the refresh
status. Use bright cyan for an active refresh, then show the previous refresh
time and per-host outcomes on separate rows when space permits. Use a green ✓
for current hosts, a red × for failed connections, and amber for their reasons.
Keep the hidden count beside the result count. Show the applied filters and
their shortcuts together, with Alt-F (⌥F on macOS) on its own line to reveal the
filter editor. Present the current sort and its shortcuts using the same
summary and action layout. In the full header, leave blank rows before filters,
sort, the legend, the result hotkeys, and the columns. Compact layouts may
shorten these breaks to keep a focused result visible.

Order the legend by table column: Status, PRs, Agent. Within Status, use the
row order for attached (●), last closed (↶), work in progress (↻), and verified
preview (↗). Use matching colors in the legend and rows. The green fzf check
marks selected rows. Align PR state icons in fixed positions within PRS, and
keep the font-independent agent symbols in their own column. Keep STATUS fixed while headings
and rows pan together. Highlight Return or Enter, Esc, and Tab, and put
Ctrl-B for the last verified close on the result hotkey row.
Do not interpret requests to improve picker filtering or sorting as requests for
new CLI flags unless the user explicitly asks for flags or noninteractive control.

## Required design adherence CI

Every PR runs **Design adherence** on Linux with fzf 0.44.1 and 0.74.5, including
fast validation. `ui/layout-contract.json` lists the required rendered fixtures
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
