# Command and terminal contract

Every public `t` command and conditional mode has an entry in
[`command-contract.json`](command-contract.json). That inventory is the source of
truth for classification and contract tests. Compatibility aliases inherit the
canonical command's entry. `ls` and `list` are interchangeable in every listing
family (including `config workflow`); root `t list` aliases `t ls`. Resolve only
command words, preserving option values, repository names, and delegated commands.
The old `t repos` family is removed; use `t repo` verbs. Read this guide and the inventory before adding or
changing a command; update both when its behavior changes.

The visual baseline is the Homebrew v0.4.2 release (`40aaea8`): cyan section
headings and current values, green primary/add actions, amber warnings/removal,
two-space outer margins, a pointer and a dark cyan focus row with light text, and a footer
showing the next action and keys. Focus and marks must remain distinguishable
without color. Align labels on wide terminals and stack them below 60 columns.
Clip text before applying color and count physical rows when repainting; at
80×24, 44×20, and 32×12 interactive screens the focused row and primary action
must remain visible.
The 60-column breakpoint applies to `_RailUI` inline menus. Most printed tables
switch to labeled, wrapped cells when their actual columns no longer fit.
Session lists instead keep one physical row per session, as described below.

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
| **Direct action/passthrough** | An explicit target acts without a needless picker. Retain ownership and worktree safety checks, confirmation before interrupting live work with an explicit target, and the delegated tool's input/output and exit status. |
| **Machine output** | Preserve existing JSON, raw-value, and remote wire formats. Do not add presentation chrome, ANSI color, or prompts. The inventory identifies machine-facing modes; this does not imply universal JSON support. |

Selection cancellation and wizard abort return 130 without starting work.
Navigating back within a wizard is different from aborting it. A completed
empty result or no-op returns 0; a launched tool's exit status is preserved.
In a batch, cancellation stops the remaining actions; already completed actions
are kept and reported. Cancellation never rolls back completed work or attaches
to another target automatically.
`t system update --all` is a direct batch action: update locally, then registered
SSH targets in alias order (duplicate targets run once), streaming each updater's
output. Report each result and continue after failures, including unreachable
hosts; return nonzero if any fail. Ctrl-C stops the batch with 130. No registered
hosts means a local-only update. Remote updates never fan out recursively.
With no usable terminal, require explicit input or use the command's documented
read-only or plan output. If raw mode fails but a terminal still exists, use a
plain prompt. Read-only modes must bypass automatic update prompts before the
handler runs; this includes list, detail, status, help, JSON, and other machine
output modes. Noninteractive commands and remote scanners never ask for a
missing repository.

Bare `t` in a terminal is a first-run wizard or, for configured accounts, a
repository/session entry screen. The wizard uses `_RailUI` and cooked prompts if
raw mode is unavailable. `t config onboard` explicitly repeats discovery. Choose
agents, local checkouts, GitHub organizations/repos, and optional Tailscale peers;
review commands and configuration changes before applying. Agent sign-in is a
separate optional checklist after default-agent selection, with no initial marks.
Only selected, unauthenticated agents are eligible; installing an agent does not
require signing in. Agent selection uses compact `logged in` labels for authenticated
agents, visible even at 32 columns. The login checklist shows selected authenticated
agents as non-selectable context, and review repeats their status. With no eligible
logins, continue directly to the next applicable step. Back preserves eligible login marks and removes selections
for agents no longer chosen. Review lists only selected sign-ins and points to
`t login` for deferred sign-in. GitHub CLI installation
and sign-in are separate explicit choices before authenticated discovery. Clone
and remote-host selections start empty. Within setup, q/Esc goes back to the
previous applicable choice screen, preserving current marks even before Enter;
empty and inapplicable steps are skipped. Review n/Esc returns to the last
applicable choice; review q, welcome q/Esc, and Ctrl-C cancel. Revisiting choices
retains discovery results and does not repeat completed GitHub install/sign-in.
Selections follow repository paths, GitHub names, and host targets; changing
organizations removes unavailable clone selections. A Tailscale peer is a candidate, not proof
of SSH access. After reviewed registration is saved, offer Verify SSH now or Later
for added or changed targets. Restore the terminal before handing native host
fingerprint and authentication prompts to OpenSSH. Reuse existing guarded
session launch and registration paths. Cancellation returns 130 and preserves any
already completed actions; completion is recorded only after successful setup.
Bare nonterminal `t` prints help without loading configuration or doing discovery;
nonterminal `t config onboard` prints the terminal requirement and returns 1.
Explicit commands never trigger onboarding. These modes are recorded in the
inventory's `entrypoints.bare_t` and `commands.config onboard` entries.
Agent login and update steps resolve the executable again when they run, including
native installations in `~/.local/bin`. Missing executables report a step failure
and leave setup resumable; they never bypass the reviewed installation choices.
Terminal session launch and resume use the native-directory fallback while preserving
PATH and shell-wrapper precedence, argument quoting, and ownership checks.
Fresh and resumed tmux panes start without echoing internal launch commands or
exit instructions. Agent output is logged from startup; failures retain diagnostics.

Config proposes unique host short names from SSH targets, including the first
manual addition. Onboarding also generates unique short names for selected peers.
Hosts → host → Short name edits an existing alias while preserving its SSH target
and default-host references. Changes remain staged until review and save.

Config quitting with pending edits offers Keep editing (default), Review and
save, and Discard changes and exit. Review screens keep their primary action
visible while scrolling.

Config → Hosts → Add host runs fresh Tailscale discovery on each entry and
offers an explicit Refresh action. The shared single picker shows online Unix
peers plus manual SSH entry and Back. Match registered and pending targets against
validated peer names and IP addresses, including `user@host` registrations; retain
distinct peers with the same short hostname and propose unused aliases. Unavailable
Tailscale leaves manual entry usable. Selection stages changes until config's
Save review; discovery never connects to a peer. Explicit `t hosts add <alias>
<target>` and read-only commands do not discover or prompt.

The save review mentions optional SSH verification. After saving, Verify SSH now
checks each distinct added or changed target with OpenSSH's native fingerprint
confirmation; Later connects to none. Alias renames, default-only changes,
unchanged targets, and discarded edits never trigger verification. Refused or
unavailable targets produce amber warnings and keep their registrations, then
the next verification runs. Ctrl-C stops with 130 and reports that configuration
was already saved. Native SSH owns the restored terminal for each check; no
fingerprint is automatically accepted. Onboarding uses the same reviewed offer.

Config includes Agent workflow globally and in each repository submenu. Changes to
stage models, efforts, enabled state and inheritance remain pending until the
shared review and save. Use recommended defaults explicitly activates an unsaved
account. The terminal editor is `t config workflow edit [--repo ALIAS]`; it edits
only the selected JSON scope and validates before publication. Repository `null`
resets inheritance. Preserve editor exit status; unchanged and invalid edits write
nothing. `t config workflow list [--repo ALIAS]` is a read-only stage/model/effort table with scope, enabled state, and inheritance
source, including recommended defaults for unsaved accounts. Add `--json` for
machine output. Global is the default; `--repo` explicitly selects a registered
alias, without checkout inference or a picker. List never starts an editor or
prompts for updates. Bare `t config workflow` shows subgroup help. Hidden
`config show/edit --workflow` compatibility forms retain their existing behavior.
Bare config edit retains its local-shell editor. Edit reusable instructions uses
the existing profile editor. Saving a workflow updates account-local native Codex
and Claude instructions with preflight and concurrent-edit checks; it never writes
tracked repository instructions.

### Representative screens

```text
dev sessions   ● attached/loaded · ✓ active context · ↻ still in progress

  STATUS  UI         MODEL    SESSION  WORKING ON
  ○       › cli      ⬡ codex  api-2    Fix login
  ○       ▣ desktop  ✱ opus   web-1    Review UI

  reattach: t open <repo> <slot>

Enter open · Esc · Space/Tab mark ✓
Ctrl-T attached · Ctrl-U unattached · Ctrl-F defaults · Ctrl-A all
Ctrl-G agent · Ctrl-P progress · Ctrl-D UI · Ctrl-B model
Ctrl-O host · Ctrl-R repo · Ctrl-V session · Ctrl-E context · Ctrl-W task
View: unattached (default) · 2 hidden
  SESSION  WORKING ON
▸✓ ○ api-2  Fix login
```

The rows above illustrate hierarchy and controls, not a fixed column schema.
List commands print normally; picker headers and marks appear only for selection.

The open picker hides attached CLI and loaded desktop sessions by default.
Ctrl-T selects attached-only; Ctrl-U selects unattached-only. Both preserve
column filters. Ctrl-F restores defaults; Ctrl-A shows all.
The compatibility --attached flag starts with all rows. There is no
new-session shortcut or details preview in this picker; use `t open --new`.
A plain green ✓ selection marker stays readable on the dark cyan focus row;
never paint a reversed-color block behind it. The marker occupies its own gutter
beside the focus pointer, separate from status marks in the row.
The open picker exposes searchable value selection for each column: Ctrl-G
agent, Ctrl-P progress, Ctrl-D interface, Ctrl-B model, Ctrl-O host, Ctrl-R repo,
Ctrl-V session/slot, Ctrl-E context, and Ctrl-W task. Use the shared single-picker
profile; Enter applies, Esc keeps the filter, and an explicit All option clears
one column. Column filters combine with attachment views. Ctrl-A clears all
filters and shows every snapshot row; Ctrl-F clears all filters and restores
unattached. Ctrl-T and Ctrl-U switch attachment without clearing column filters.

Resume and close retain cycling Ctrl-G agent, Ctrl-O host/origin, Ctrl-R repo,
and (close only) Ctrl-D interface, with their existing state toggles. Resume
never invents a historical UI. Resume uses actual user-turn timestamps, newest
first by default, with unknown times last; live rows have no artificial priority.
Alt-T selects last turn, Alt-R repository, Alt-G agent, Alt-O host/origin, and
Alt-V session (natural numeric order). Text modes ascend with newest turn and
stable identity as tie-breakers. Sorts apply within the private filtered snapshot
and preserve query and identities. Ctrl-A clears filters and keeps the sort;
Ctrl-F resets filters and last-turn sorting. Compact headers use M for Alt and ^
for Ctrl. Resume disables fzf relevance sorting so searching preserves the chosen
order. Saved transcript mtime/reopen time still controls --days eligibility.
Every column value comes from raw metadata,
not truncated display text. The progress value exactly follows the ↻ marker:
work continued after a merged PR; its absence is not completion evidence.
A row's ✓ means loaded conversation context, independent of attachment and
current computation; the selection gutter ✓ marks an action target.

Query and visible selection identity survive supported reloads; hidden marks
are removed. Old fzf clears marks on filter actions. These remain filtered/multi
modes, not new commands or discovery operations. Every shortcut and a focused
row remain visible at 80×24, 44×20, and 32×12. Short terminals use a borderless
full-height view, including nested value pickers. Open's narrow header uses ^
for Ctrl and shows the active filter count; full values are available in the
column pickers. Filter metadata comes from the same private snapshot as the
rows. A named repository and prior discovery limits remain in force.

The kill multi-picker's Enter action confirms every marked session (or the
focused row), including live local and remote agents; do not prompt again per
target. Explicit close targets retain live-agent confirmation unless `-y` is
supplied. Closing an unloaded desktop slot preserves history and its worktree;
a running app or missing archived rollout alone does not block cleanup. Verify
writer ownership before and after cleanup, and retain the reservation on failure.
Close scopes sibling locks through readable conversation indexes and, for unindexed
threads, validated rollout identity metadata. A lock with no workspace association
does not veto every close. Selected and known same-worktree writers still block;
shared backend processes and lock files remain untouched. These are safety rules
within the existing close modes, with no new prompt or command.

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

Saved candidate discovery uses a disposable source-validated cache and one snapshot
per resume invocation. File identity, size, nanosecond timestamps, and SQLite WAL
changes invalidate cached metadata. Live ownership is never taken from this cache.
Terminal commands may print plain stage progress to stderr before slow discovery;
redirected output, JSON, and remote scanner protocols remain free of progress.
This adds no command, picker, prompt, or conditional selection mode.

Quiet blocking work (provider calls, repository/setup probes, updates, AgentCore,
sharing, and session/history discovery) uses a delayed
spinner on stderr. Python uses the shared `libexec/t_progress.py` `_spinner`
context/decorator; shell scans use `_t_with_progress`
with terminal-boundary opt-in. Nested scans share one animation. Clear the line
in a finally/always block on success, failure, and cancellation, before rendering
results or opening a picker. Never animate over prompts, passthrough output,
redirected output, JSON, MCP, or remote protocols; dumb terminals remain plain.

`session goal REPO SLOT GOAL [--json]` is an explicit direct action. It requires
both targets, never infers or picks, and submits a new objective to a verified
local Claude/Codex tmux conversation. JSON formats the delivery receipt and is
still a mutation. Goal does not imply interruption, adoption, or completion.
MCP uses the same operation; its session command gateway returns a terminal
handoff descriptor for modes that need interactive control. Detached MCP opens
and resumes retain the shared lifecycle checks and never attach a terminal.


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

`session move` sends a current local CLI conversation through a shared single
picker when no slot or conversation ID is supplied. It uses the session-list
inventory and formatter, including attached and detached Claude/Codex sessions;
saved history, unknown IDs, and desktop reservations are not candidates. A named
repository or the current checkout scopes the picker; outside a checkout it
shows all repositories, and `--all` widens the scope. Enter selects one exact
conversation, worktree, and agent; recheck that identity before transfer.
Esc/Ctrl-C cancels with 130 and an empty inventory is a successful no-op.
Without a terminal/fzf, require an explicit target. Inside an agent, the implicit
current conversation remains a direct action; `--pick` opens selection instead.
Explicit slot/ID sends and `--from`/`--here` receives retain their guarded routes.

New worktree folders include the repository basename (`t/t-12`); numeric slot
arguments, tmux identity, JSON slot fields and history associations are unchanged.
`remote open`, `remote move`, and `remote resume` require explicit verbs. Missing
hosts use `_t_fzf_single`, displaying alias and SSH target; Enter chooses exactly
one, including when only one host exists. Esc/Ctrl-C returns 130 without starting
work. Missing selections require a terminal and fzf; scripts name their targets.
An empty current-session inventory returns 0. Remote workers suppress nested
automatic update prompts.

`remote move` uses a single shared current-session picker over local and registered
remote CLI owners, including attached sessions and excluding desktop/unknown IDs.
Repository scope uses home-relative checkout/worktree paths across hosts. Duplicate
slot labels retain their host identity. Explicit source plus slot/ID bypasses the
session picker; an explicit target without source resolves a unique snapshot or
requires selection when ambiguous. Destination selection excludes every alias of
the source target, and never silently chooses the default host. Recheck host, ID,
path, label, agent and live ownership after both selections and on the source before
stopping it. Failed scans or transfer steps fail closed. `session move --from A
--host B` delegates the whole guarded send to A over `zsh -lic`, using controller
resolved SSH targets. The controller never briefly resumes the conversation.

`remote open` selects current sessions on the chosen host and never starts fresh
from an empty picker; `--new` is explicit. `remote resume` selects saved history
on the chosen host using the existing filtered resume picker. `session resume
--host H` instead sends locally discovered history to H. Live rows attach only
when their owner matches H (including host aliases); a mismatch gives an exact
`remote move` instruction before any batch mutation. Desktop resume stays local.

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

`t session list` overrides the shared table's narrow layout: first clip WORKING ON
to the available space, then omit UI and MODEL if the other columns cannot fit.
Compact rows show S (the same status marks without spaces), HOST for remote lists,
SESSION (SLOT at the smallest width), TASK, and LAST TURN. Shorten timestamps to
local `MM-DD HH:MM`, or `HH:MM` when necessary. Count Unicode display columns and
mark clipped values with an ellipsis. Lists stay read-only and never open a picker.
Redirected output retains all columns and complete values: `t ls | cat` or
`t ls -r | cat`. For example, at 32 columns a remote row can look like:

```text
  S   HOST SLOT   TASK LAST TURN
  ●✓↻ mini api-17 Fix… 09:41
```

The filtered multi-selection mode of `t session open` prioritizes session identity
and working-on text. It omits LAST TURN, previews, and the new-session shortcut.
Compact rows use the shared Unicode width helpers; full task text and metadata
remain searchable. Hidden host/session/state identity fields remain unchanged.
Explicit targets still bypass selection. Open and close pickers label displayed
columns in a non-selectable header preserved across filter changes. Narrow
filtered views use full height and smaller margins to retain controls and rows.

Printed history appends DATE, BRANCH, PRS, and the full conversation ID after these
shared columns, retaining
historical details when no preview is available. Wide tables align these columns. When the actual columns do not fit, printed
history lists switch to labeled, wrapped cells and preserve complete values. Redirected
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
slot is unused. Reopening a locally reserved desktop workspace also tolerates
unavailable hosts after checking local ownership. Desktop claims scan remote
owners once; known remote owners still block, and local terminal writers still
prevent reopening. Open, close, resume, and remote move source pickers retain reachable rows when a
registered host is unavailable. Saved CLI and desktop resumes warn that skipped
ownership could not be verified, then continue through local locks and known-owner
checks on reachable hosts. Failed local scans remain fatal. Other unreserved
workspace claims and live transfers retain strict ownership checks.
Warnings are amber on capable stderr terminals and plain with NO_COLOR, dumb
terminals, or redirected stderr; scanner and machine stdout stay unchanged.
This includes unavailable-host warnings from `t session list -r` (`t ls -r`):
local and reachable sessions remain listed. Redirecting stdout alone keeps
warnings amber when stderr is still a capable terminal.
Picker helpers return without running the caller's
shell exit hooks or saving its history. All Codex
CLI starts and resumes use an invocation-local backend (`--no-daemon`).
These are direct-action behaviors within the existing open/resume modes.

Issue starts warn and skip unavailable registered hosts and unconfigured optional
cloud providers. Saved resume routes retain local checks and known owners while
tolerating unavailable hosts, including standalone CLI and desktop resumes.
A unique explicitly referenced or issue-linked GitHub PR is a direct action: recover
its published commit in a fresh local worktree and reuse the PR. Ambiguous PRs and
branches use the existing single-owner picker; branch recovery does not require a PR.
Recheck ownership and published state before launch. Read-only status/JSON preserves
incomplete results, including additive `skipped: true` on warning-only sources.

Issue discovery separates incidental transcript mentions from issue owners. A
unique actionable local workspace continues directly; local candidates never
enter the external-agent takeover prompt. Legacy saved workspaces can use issue
commit evidence and the normal guarded resume picker. Existing-work selection
uses the shared single picker with a private details preview showing session,
host, agent/model/interface, working-on text, conversation ID, last turn, branch,
worktree, association evidence and continuation commands. Multiple real owners
still require selection. Status/JSON remains read-only, with additive `mentions`,
`owners` and `association` fields; incidental mentions never become launch blockers.

`t repo add [path] [--alias NAME]` is a direct local registration action. Without
a path it resolves the current checkout, including from a subdirectory. It never
opens a picker; `--dry-run` is read-only detail output. Repeated registration is
a no-op. Linked/session worktrees and conflicting aliases or checkout directory
names are rejected. The config repository menu asks for a path (default
`~/config`), suggests its directory name as the alias, and retains review before
save. `t config setup [dirs ...]` defaults to scanning `~/config`.
Successful setup (including no-op and dry-run) prints desktop **Review hooks →
Allow selected** and CLI `/hooks` guidance when t's Codex hook is registered.
This is informational output within the existing wizard/detail modes: no new
picker, app launch, or hook trust mutation. Installation repeats the guidance
for existing registrations; `t system diagnose` stays read-only.
The agent integration helper is `t-session-start`; `claude-stamp-tmux` remains
an equivalent compatibility entry point. Installation migrates standard legacy
hook commands while preserving custom commands and options. This is an external
SessionStart hook, not a new public `t` command or interactive mode.

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

`t system diagnose` (`t doctor`) is read-only: it reads startup wiring without
executing the shell rc, does not fetch Git refs, and never opens settings or update
prompts. Its GitHub CLI version probe disables telemetry and update checks so it
does not create GitHub CLI state. `--repair` is a direct action that appends the
managed zsh startup block and
repairs a missing command link, preserving user settings and security choices. It
prints an executable source command; other findings keep their explicit remedies.
Missing shell setup is a preselected row in `t agent install`, including when no
agent changes are needed. It appears in the review and `-y` plan; deselection,
status, dry-run, cancellation, and noninteractive plans do not modify it.

## Progress enforcement

Run `python3 scripts/lint_progress.py` alongside CLI lint. CI requires every
Python subprocess, network request, and sleep site to use `_spinner` or have a
reviewed reason in `ui/progress-contract.json`. Interactive tools own their
terminal, streamed installers provide their own output, and background/protocol
workers remain quiet. The inventory also protects shared shell scanner and
resume progress seams. Add coverage when introducing another slow shell phase.

For example, `t issue list`, `t history --refresh`, and `t system update --check`
show a delayed wheel during quiet discovery in a terminal; their redirected
output stays clean. Spinners clear before results, errors, or selection.

Interactive bare `t` optionally displays cached installed-tool advisories when
`T_VULNERABILITY_CHECK=1`, then schedules a detached refresh. This informational
startup mode never prompts or remediates. Nonterminal bare `t` and explicit
commands remain unchanged. See [coverage and privacy](../docs/vulnerabilities.md).

`t system advisories status` (also the default mode) is read-only and prints
cached repository-tool coverage and worker state. `enable --interval-hours N`
and `disable` are explicit direct actions for the per-user periodic worker,
without pickers, OS service registration, remediation or update prompts.
