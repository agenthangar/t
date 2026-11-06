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
