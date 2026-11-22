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

## Picker filters and sorting

Interactive session filtering and sorting use the existing picker hotkeys.
For `t open` and `t resume`, extend those controls and preserve every existing
filter and sort choice. Ctrl-F toggles filters, Ctrl-X resets filters, Ctrl-S
toggles sorting, and Ctrl-Y cycles sort choices; use native Mac labels when appropriate.
Slot history retains its pagination keys and uses Ctrl-S/Ctrl-Y for sorting.
Match `t open` visually: reuse its shared sort block, blank rows between header
sections, and two-space table gutters when space permits. Compact layouts may
remove those gaps to keep the focused result visible.
Do not interpret requests to improve picker filtering or sorting as requests for
new CLI flags unless the user explicitly asks for flags or noninteractive control.

The resume table has a horizontal viewport. Over the table, ordinary wheel-up
pans left and wheel-down pans right; horizontal wheels follow their direction.
Keep the query, controls and selection gutter fixed while headings and data move
together. Show `<` and `>` only at edges with hidden content. Panning preserves
query editing, focused identity and marks, and search includes offscreen text.
Wheel events over controls or details retain their native behavior.

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

Open picker actions include Ctrl-N for a fresh local session. Keep native key
labels, `Tab ✓` spacing, the full HOTKEYS/filter/sort grouping, and a filled
current Filters ON and Sort label. Do not add a selectable fake session row for
New. Show the status legend in the bottom footer where supported; older fzf
keeps a compact header legend and short terminals prioritize rows and controls.
