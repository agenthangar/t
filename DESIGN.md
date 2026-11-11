# t design system

`t` has a terminal design system. Its authoritative guidance is the
[command and terminal contract](ui/README.md), including visual styling,
interaction profiles, keyboard controls, terminal sizing, and planning previews.

- [Command inventory](ui/command-contract.json): command modes and their tests.
- [Shared implementations](ui/README.md#shared-implementations): `Style` and
  `_RailUI` in `bin/t`, and the picker profiles in `ui/fzf.sh`.
- [Planning previews](ui/README.md#planning-previews): faithful, isolated prototypes
  of proposed command behavior and the separate preview of implemented commands.
- [Terminal screen gallery](ui/previews/terminal-gallery.html): one-page planning
  reference with shared screen families and labeled sample states.

## Planning preview format for every session

When a planning preview is requested or due, present all affected screens together
on one self-contained HTML page. Use a scrollable gallery with a linked index;
do not hide screens or essential states behind a screen selector, tabs, or a
carousel. Each screen remains visible while its own sample interactions work.

For changes to the shared terminal design, include every shared screen family,
affected custom screens, and the relevant scoped, narrow, short, empty, error,
review, cancellation, and result states. Label each example's command or navigation
entry point, viewport, sample data, and whether it is existing or proposed.
Arguments that only narrow a command's scope must demonstrate the same screen.
Distinguish representative renderer examples from complete application menus.

Use shared fixed column tracks for HTML headings and rows, including separate
pointer and mark gutters. Unicode, focus styling, search, filtering, and sorting
must not move columns. Verify actual rendered cell positions and overflow;
character counts or source inspection alone are not visual verification. Keep
browser verification limitations explicit. The implemented CLI must separately
be checked in a real terminal.

Reuse these conventions and implementations when adding or improving commands.
Workflow configuration and explicit task instructions determine preview timing,
approval, and delivery; this entry point does not create additional approval gates.
