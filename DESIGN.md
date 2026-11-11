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
Match `t open` visually: reuse its shared sort block, blank rows between header
sections, and two-space table gutters when space permits. Compact layouts may
remove those gaps to keep the focused result visible.
Do not interpret requests to improve picker filtering or sorting as requests for
new CLI flags unless the user explicitly asks for flags or noninteractive control.
