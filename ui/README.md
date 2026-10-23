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
| **Single selection** | Use a searchable fzf picker or an inline `_RailUI` menu with one visibly focused row. Enter chooses exactly one item; Esc/Ctrl-C cancels. An explicit target bypasses selection. A paginated single picker uses Ctrl-F for the next page, Ctrl-B for the previous page, and Ctrl-R to refresh; those keys belong to pagination rather than the filtered picker's defaults control. |
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

Enter open · Esc · Space/Tab mark ✓ · Ctrl-/ details
Ctrl-A show all · Ctrl-F defaults · 2 hidden
Alt-N new · Ctrl-T attached: hidden
▸ api  2  ⬡ codex  › cli  ○  codex  Fix login
```

The rows above illustrate hierarchy and controls, not a fixed column schema.
List commands print normally; picker headers and marks appear only for selection.

The open picker adds Alt-N to start its existing `--new` flow with the original
repository and opening flags. It discards marks and works with no matching rows.
Ctrl-N remains navigation. The shared filtered open profile returns 3 for this
explicit action; cancellation continues to return 130 without starting work.

## Shared implementations

| Surface | Implementation | Responsibility |
| --- | --- | --- |
| Inline menus and reviews | `Style` and `_RailUI` in `bin/t` | Layout, scrolling, focus, page, footer, cooked prompt, terminal restoration |
| Searchable selection | `_t_fzf_single`, `_t_fzf_multi`, `_t_fzf_filtered`, and `_t_fzf_paginated` in `ui/fzf.sh` | Theme, marks, header controls, filtering or pagination, preview, cancellation |

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

## Session information and history

Desktop handoff refusals identify a verified conflicting conversation and give
release instructions with the exact retry command before stopping the selected
CLI. Keep missing-preview notices distinct from ownership failures. Retrying
after the conflicting owner releases must preserve the selected conversation,
recorded worktree and history, and unrelated writers.

`session resume --app` retains the saved-conversation picker and accepts exactly
one Codex row. Selection carries the immutable ID and recorded worktree through
the existing guarded desktop handoff; live slots never substitute their current
conversation. Remote owners, incompatible flags, unsupported agents, and multiple
marks fail before launching. `--cli` explicitly selects the ordinary resume route.

New worktree folders include the repository basename (`t/t-12`); numeric slot
arguments, tmux identity, JSON slot fields and history associations are unchanged.
Existing `t/12` folders and recorded conversation paths remain valid. Both
layouts appear in list and navigation results. Ambiguous simultaneous layouts
must fail with an inspection/history hint before a slot mutation. Transfers
negotiate layout support before stopping the source; see the compatibility rules
in the main README. These are path changes within existing command modes.

The inventory's `display_schemas` are machine-readable definitions for the
information shared by `t session list` (`t ls`) and `t session history`
(`t history`). Each command binds its `display_schema`; history extends
`session_information` with recorded PR evidence and historical details. CLI lint
checks these definitions and their safety policies. Behavior tests check actual
renderer projections, table output, details, and JSON against the definitions.

| Column | Meaning |
| --- | --- |
| STATUS | Reported ownership status, active-context evidence, and the still-in-progress mark. A retained conversation can be saved, archived, or missing without a live owner. |
| UI | The recorded or reported CLI, desktop, or foreground interface. Interface and agent are independent facts. |
| MODEL | The agent glyph and exact recorded or current-owner model. `-` means the model is unknown. |
| HOST | The reported owner or retained association host. Local `t ls` can omit this column; history includes it. |
| SESSION | The repository/slot or foreground-session label. It is distinct from the immutable conversation ID. |
| WORKING ON | The current owner's working-on text, recorded summary, or retained conversation title. History includes up to three associated PR references and states, with an additional count when needed. |

`t session list` additionally places LAST TURN in the final column, after WORKING ON.
It shows the latest turn initiation as `YYYY-MM-DD HH:MM:SS TZ` in the viewer's
local timezone, including remote rows; unavailable evidence displays `-`.
Codex uses `task_started` (older transcripts may use `user_message`); Claude
uses an actual user prompt, excluding tool results and injected metadata.
Assistant output, transcript mtime, and index activity never substitute for a
turn start. The optional ninth rich-row field, `last_turn_started_at`, carries
an ISO 8601 UTC timestamp or `-`; legacy rows and the seven-field ownership
protocol remain valid. This remains a read-only list mode. History's DATE
continues to describe retained activity.

Printed history appends DATE, BRANCH, PRS, and the full conversation ID after these
shared columns, retaining
historical details when no preview is available. Wide tables align these columns. When the actual columns do not fit, printed
lists switch to labeled, wrapped cells and preserve complete values. Redirected
output remains untruncated. History picker rows stay on one physical line;
compact layouts put status and meaningful working-on text first. Date, branch,
and technical identity must not displace that text. Verify the 80×24, 44×20, and
32×12 layouts and count Unicode display columns rather than string length.

Unrecorded historical interface and active-context facts remain unknown. Never
infer a CLI interface or a model from `codex`/`claude`, and never turn a saved
conversation into an attached or active one from its branch, current worktree,
or PR state. Only an exact current owner supplies current ownership facts.
Recorded model and summary provenance remain available in the full details.
Presentation enrichment does not change conversation ownership or resume checks.

The preview includes the complete working-on text, status/context, UI, agent and
model, host/session, date, repository and full conversation ID, every workspace
association and branch/commit/evidence, all associated PRs and their states,
incidental mentions labeled separately, and transcript availability. Show it by
default at 70 or more columns and 22 or more rows, in a bottom preview occupying
35% of the full-height picker; Ctrl-/ toggles it at every size. Use the shared
paginated single picker: Enter selects one exact conversation, Esc/Ctrl-C cancels,
Ctrl-F/Ctrl-B changes pages, and Ctrl-R explicitly refreshes metadata. Search runs
against all retained records before slicing a page. A single search result still
waits for Enter, and returned query/page/snapshot identity must be validated before
resuming. Browsing uses cached provider metadata; refresh is explicit.

History JSON retains its version 1 envelope (`version`, `repo`, `slot`, `page`,
`limit`, `total`, and `conversations`) and existing conversation fields, including
full `sid`, associations, transcript sources, PR evidence, mentions, and owners.
Recorded `model`/`summary`/`interface`, their provenance, and the derived `display`
projection are additive metadata. They must not replace raw identity or evidence.
JSON and non-TTY listings never select or resume; JSON has no ANSI styling,
presentation headers, or prompts.

Fresh local opens may continue after incomplete remote discovery when the local
slot is unused. Known remote owners still block or route to that host. Existing
workspaces and saved-conversation handoffs keep strict ownership checks. All Codex
CLI starts and resumes use an invocation-local backend (`--no-daemon`).
These are direct-action behaviors within the existing open/resume modes.

Issue starts warn and skip unavailable registered hosts and unconfigured optional
cloud providers. Their saved CLI resume route retains local checks and known owners
while tolerating unavailable hosts; standalone resume and desktop handoffs stay strict.
A unique explicitly referenced or issue-linked GitHub PR is a direct action: recover
its published commit in a fresh local worktree and reuse the PR. Ambiguous PRs and
branches use the existing single-owner picker; branch recovery does not require a PR.
Recheck ownership and published state before launch. Read-only status/JSON preserves
incomplete results, including additive `skipped: true` on warning-only sources.

## Changes since the visual baseline

Review these behavior changes when applying the v0.4.2 standard. Later changes
should be added here with their disposition before becoming a precedent.

| Change | Disposition |
| --- | --- |
| Batch `open`, `kill`, and issue starts ([#68](https://github.com/agenthangar/t/pull/68), [#93](https://github.com/agenthangar/t/pull/93)) | Retain multi-selection and one-live-owner checks. |
| Richer rows, interface/model columns, and detail previews ([#85](https://github.com/agenthangar/t/pull/85)) | Retain within the baseline spacing and focus design. |
| Reversible filters and dedicated keys ([#94](https://github.com/agenthangar/t/pull/94), [#102](https://github.com/agenthangar/t/pull/102)) | Adopt the filter controls above. |
| Login terminal I/O and expiry ([#119](https://github.com/agenthangar/t/issues/119)) | Preserve one vendor terminal and restore it on exit; local sign-in links have a short plain-URL fallback. Status shows cached deadlines when available and keeps authenticated state distinct from expiry availability. |
| Login selection and first-Enter execution ([#83](https://github.com/agenthangar/t/pull/83), [#105](https://github.com/agenthangar/t/pull/105)) | Adopt first-Enter execution; reuse the merged implementation. |
| Reviewed Enter-to-run install ([#98](https://github.com/agenthangar/t/pull/98)) | Retain review followed by Enter to run. |
| Read-only issue table ([#108](https://github.com/agenthangar/t/pull/108)) | Apply the list contract. |
| Open GitHub issues in the browser ([#118](https://github.com/agenthangar/t/issues/118)) | Explicit IDs or URLs act directly; omitted IDs use the shared filtered multi picker. |
| Noun/verb help, added command groups, and speed submenu ([#101](https://github.com/agenthangar/t/pull/101)) | Retain and classify each mode in the inventory. |
| Paginated history selection ([#113](https://github.com/agenthangar/t/pull/113)) | Treat as single selection with Ctrl-F next, Ctrl-B previous, and Ctrl-R refresh; do not apply the filtered picker's Ctrl-F defaults binding. |
| Conversation context sharing ([#112](https://github.com/agenthangar/t/pull/112), [#122](https://github.com/agenthangar/t/issues/122)) | Bare `t share` in a terminal is a source, recipient, context, and launch review wizard; after review it enters the new session. A known source and agent-only `--to` can launch without a terminal and leave the new session detached; `--json` returns the result without prompting or entering. An exact destination with `--file` still sends directly. `targets` and `inbox` are read-only lists; `read` is detail or raw attachment output. Cancellation leaves sessions and inboxes untouched. |

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
