# Command and terminal contract

Every public `t` command and conditional mode has an entry in
[`command-contract.json`](command-contract.json). That inventory is the source of
truth for classification and contract tests. Compatibility aliases inherit the
canonical command's entry. Read this guide and the inventory before adding or
changing a command; update both when its behavior changes.

The visual baseline is the Homebrew v0.4.2 release (`40aaea8`): cyan section
headings and current values, green primary/add actions, amber warnings/removal,
two-space outer margins, a pointer and reverse-video focus row, and a footer
showing the next action and keys. Focus and marks must remain distinguishable
without color. Align labels on wide terminals and stack them below 60 columns.
Clip text before applying color and count physical rows when repainting; at
80×24, 44×20, and 32×12 interactive screens the focused row and primary action
must remain visible.
The 60-column breakpoint applies to `_RailUI` inline menus. Printed tables
switch to labeled, wrapped cells when their actual columns no longer fit.

## Command types

The types compose: `t session open` can be a direct action with an explicit
slot, or a filtered multi-selection without one. Choose by behavior rather than
the command name.

| Type | Behavior and appearance |
| --- | --- |
| **List** | Print a scoped, aligned table immediately. Name its scope, label columns, and give a clear empty state. No picker, confirmation, or update prompt. `t session list` and `t issue list` are examples. |
| **Detail/status** | Print labeled facts without changing state or prompting. Preserve raw text, paths, and streams when the command explicitly exposes them. |
| **Single selection** | Use a searchable fzf picker or an inline `_RailUI` menu with one visibly focused row. Enter chooses exactly one item; Esc/Ctrl-C cancels. An explicit target bypasses selection. |
| **Multi-selection** | Searchable pickers use Space/Tab to mark rows and Enter to accept the marks, or the focused row if none are marked. Inline checklists use Space to toggle and Enter to submit only checked items. Show marks and the Enter action independently of color. |
| **Filtered selection** | Show active filters and hidden count in the header. Ctrl-A shows all; Ctrl-F restores defaults. Session pickers add Ctrl-T (attached), Ctrl-L (live), or Ctrl-P (pending) where applicable. Filtering retains the search and removes hidden marks. Apply show-all/default behavior to issue pickers as well. Ctrl-/ toggles full details when rows have previews. |
| **Wizard/review** | Stage choices, then display the resulting changes before applying them. Config saves on Enter; install enters review and then runs on Enter; login runs the selected logins on its first Enter. Setup and repository creation keep their released `y` confirmation. Show the actual action in the footer. |
| **Direct action/passthrough** | An explicit target acts without a needless picker. Retain ownership and worktree safety checks, confirmation before interrupting live work, and the delegated tool's input/output and exit status. |
| **Machine output** | Preserve existing JSON, raw-value, and remote wire formats. Do not add presentation chrome, ANSI color, or prompts. The inventory identifies machine-facing modes; this does not imply universal JSON support. |

Selection cancellation and wizard abort return 130 without starting work.
Navigating back within a wizard is different from aborting it. A completed
empty result or no-op returns 0; a launched tool's exit status is preserved.
In a batch, cancellation stops the remaining actions; already completed actions
are kept and reported. Cancellation never rolls back completed work or attaches
to another target automatically.
With no usable terminal, require explicit input or use the command's documented
read-only or plan output. If raw mode fails but a terminal still exists, use a
plain prompt. Read-only modes must bypass automatic update prompts before the
handler runs; this includes list, detail, status, help, JSON, and other machine
output modes. Noninteractive commands and remote scanners never ask for a
missing repository.

Config quitting with pending edits offers Keep editing (default), Review and
save, and Discard changes and exit. Review screens keep their primary action
visible while scrolling.

### Representative screens

```text
dev sessions   ● attached/loaded · ✓ active context · ↻ still in progress

  STATUS  UI         MODEL    SESSION  WORKING ON
  ○       › cli      ⬡ codex  api-2    Fix login
  ○       ▣ desktop  ✱ opus   web-1    Review UI

  reattach: t open <repo> <slot>

Space/Tab select · Enter open · Ctrl-/ details · Esc cancel
Ctrl-A show all · Ctrl-F defaults · 2 hidden
Ctrl-T attached: hidden
▸ api  2  ⬡ codex  › cli  ○  codex  Fix login
```

The rows above illustrate hierarchy and controls, not a fixed column schema.
List commands print normally; picker headers and marks appear only for selection.

## Shared implementations

| Surface | Implementation | Responsibility |
| --- | --- | --- |
| Inline menus and reviews | `Style` and `_RailUI` in `bin/t` | Layout, scrolling, focus, page, footer, cooked prompt, terminal restoration |
| Searchable selection | `_t_fzf_single`, `_t_fzf_multi`, and `_t_fzf_filtered` in `ui/fzf.sh` | Theme, marks, header controls, filtering, preview, cancellation |

Source `ui/fzf.sh` and select the profile matching the command mode. Callers
provide row data, action text, and previews; they must not replace required
bindings, marks, or legends. The profiles preserve `FZF_DEFAULT_OPTS`, while
scoping the cyan/green theme to `t` pickers. All terminal state changes must
be restored in `finally` or a shell trap, including cancellation and failures.

Use the existing `_RailUI` instance for inline flows. `intro`, `section`,
`row`, `chrome`, and `frame` define the design; `pick` combines them for
ordinary menus. Editable lists use `row` and `frame` with `(line, item_key)`
pairs, with `None` for headings. `page` renders the review. Render notices
with `done` and editable prompts with `cooked_input`.

## Changes since the visual baseline

Review these behavior changes when applying the v0.4.2 standard. Later changes
should be added here with their disposition before becoming a precedent.

| Change | Disposition |
| --- | --- |
| Batch `open`, `kill`, and issue starts ([#68](https://github.com/agenthangar/t/pull/68), [#93](https://github.com/agenthangar/t/pull/93)) | Retain multi-selection and one-live-owner checks. |
| Richer rows, interface/model columns, and detail previews ([#85](https://github.com/agenthangar/t/pull/85)) | Retain within the baseline spacing and focus design. |
| Reversible filters and dedicated keys ([#94](https://github.com/agenthangar/t/pull/94), [#102](https://github.com/agenthangar/t/pull/102)) | Adopt the filter controls above. |
| Login selection and first-Enter execution ([#83](https://github.com/agenthangar/t/pull/83), [#105](https://github.com/agenthangar/t/pull/105)) | Adopt first-Enter execution; reuse the merged implementation. |
| Reviewed Enter-to-run install ([#98](https://github.com/agenthangar/t/pull/98)) | Retain review followed by Enter to run. |
| Read-only issue table ([#108](https://github.com/agenthangar/t/pull/108)) | Apply the list contract. |
| Noun/verb help, added command groups, and speed submenu ([#101](https://github.com/agenthangar/t/pull/101)) | Retain and classify each mode in the inventory. |

## Verification

Run `python3 scripts/lint_cli.py` after command changes. It checks inventory
coverage, parser/help consistency, shared picker use, and agent guidance. Add
fixture-backed cases for each affected mode's direct target, interactive and
noninteractive paths, cancellation, empty state, errors, and machine output as
applicable. Real picker tests must exercise key handling and filter changes on
both fzf 0.44.1 and a pinned current release in CI. Check 80×24, 44×20, and
32×12 views, long rows, monochrome output, and terminal restoration. Keep test
HOME/XDG paths and tmux sockets isolated; native macOS UI tests remain opt-in
for routine local runs.
