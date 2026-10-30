# Command and terminal contract

Every public `t` command and conditional mode has an entry in
[`command-contract.json`](command-contract.json). That inventory is the source of
truth for classification and contract tests. Compatibility aliases inherit the
canonical command's entry. `ls` and `list` are interchangeable in every listing
family (including `workflow`); the former `t config workflow` routes remain
compatibility aliases for `t workflow`. Root `t list` aliases `t ls`. Resolve only
command words, preserving option values, repository names, and delegated commands.
The old `t repos` family is removed; use `t repo` verbs. Existing repository selectors
accept registered aliases, checkout basenames, and unambiguous local origin repository
names (case-sensitive, read from local Git metadata without network access), with exact
aliases taking precedence. Ambiguous names fail before selection or mutation.
Remote opens resolve against the selected host’s registrations. Remote resumes
resolve local aliases/names through verified repository identity; unknown
target-only selectors remain literal.
Repository creation names, paths and delegated payloads remain literal. Read this
guide and the inventory before adding or changing a command; update both when its behavior changes.

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

Terminal titles use short public command labels while a command runs and clear
on return, including failures and cancellation. Remote open/resume adds the host
and public action before SSH starts. Existing session/tmux labels remain useful.
Titles exclude delegated payloads, worker code and environment assignments; control
characters are stripped and labels are bounded. Redirected output, dumb terminals,
internal workers and JSON/raw/dump/statusline modes emit no title escapes. Terminal
profiles may still append foreground arguments independently of OSC titles; remote
foreground workers use single-quoted outer commands to avoid escaped-space titles.

## Command types

The types compose: `t session open` can be a direct action with an explicit
slot, or a filtered multi-selection without one. Choose by behavior rather than
the command name.

| Type | Behavior and appearance |
| --- | --- |
| **List** | Print a scoped, aligned table immediately. Name its scope, label columns, and give a clear empty state. No picker, confirmation, or update prompt. `t session list` and `t issue list` are examples. |
| **Detail/status** | Print labeled facts without changing state or prompting. Preserve raw text, paths, and streams when the command explicitly exposes them. |
| **Single selection** | Use a searchable fzf picker or an inline `_RailUI` menu with one visibly focused row. Enter chooses exactly one item; Esc/Ctrl-C cancels. An explicit target bypasses selection. A paginated single picker uses Ctrl-F for the next page, Ctrl-B for the previous page, and Ctrl-R to refresh; those keys belong to pagination rather than the filtered picker's settings control. |
| **Multi-selection** | Searchable pickers use Space/Tab to mark rows and Enter to accept the marks, or the focused row if none are marked. Inline checklists use Space to toggle and Enter to submit only checked items. Show marks and the Enter action independently of color. |
| **Filtered selection** | Show active filters and hidden count in the header. Session pickers use Ctrl-F for staged filter settings, Ctrl-A to turn filters off, and Ctrl-L to restore saved choices. State and column choices live in settings. Filtering retains the search and removes hidden marks. Issue pickers retain Ctrl-A show-all and Ctrl-F defaults. Ctrl-/ toggles full details when rows have previews. |
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

The config hub groups Session defaults, Agents, Workflows, Runners, Repositories &
worktrees, Remote hosts, and Finish. Keep agent settings and local worker/model
setup together, with workflow in its own section; keep repository/worktree choices
separate from
host registration and the default beam host. Settings collections and setup
destinations use a trailing `›`; scalar choices and immediate actions do not.
All actions remain directly reachable, returning preserves row focus, and the
shared viewport keeps the focused row and Save shortcut visible on narrow screens.

Per-agent settings expose Cross-host handoff with staged review/save. Effective
defaults are `DEV_HANDOFF[claude]=t` and `DEV_HANDOFF[codex]=native`; Codex can
explicitly select `t`. Claude native is unsupported and rejected. `t config show`
is read-only and prints the mechanism and app requirement. Native Codex transfer
is a manual desktop Hand off action, not an automated CLI transfer: cross-host
resume/move prints actionable steps and returns nonzero before stopping an owner,
checkpointing, copying transcripts or allocating a destination. Local resume and
remote attachment retain their existing behavior. Relays preserve the initiating
host's policy and require a peer that understands it.

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

Config includes Workflows globally and in each repository submenu. The global
menu chooses a named default, adds or edits shared workflows, or activates recommended
defaults. Each workflow exposes an overall Workflow switch and an independent
Model stages switch. Disabling stages retains their model/effort settings and keeps
behavior preferences and Markdown active; disabling the workflow suppresses all
three. Active stages expose Planning, Implementation and Review with separate
model and effort controls, plus Instructions for ordinary Markdown in VISUAL/EDITOR.
Behavior preferences uses a shared `_RailUI` submenu for Worktree (isolated/current),
Local preview (UI changes/on request), Validation (relevant/full documented checks),
and Delivery (review/PR/authorized auto-merge). Every setting offers No preference,
which removes its key; no behavior is enabled by default. Model-stage rows are hidden
while stages are off and their stored values are preserved.
Models load lazily from Codex once per menu session; current, recommended and custom
values remain available offline, with explicit Refresh. Changing a model preserves
its effort and never changes launch defaults. Shared-definition editing explains
its affected repository consumers. Repository menus display their source and offer
named assignment, repository-only customization, explicit exclusion retaining the
source, resuming that retained source, or inheritance. All menu changes remain pending until review and Save;
unchanged selections, canceled editors and discarded changes write nothing.
The menu and save review explain precedence: the host instruction hierarchy is
unchanged; explicit task and repository rules remain authoritative; one selected
repository workflow replaces the global default; exclusion suppresses the entire
workflow. Within a workflow, custom Markdown overrides selected behavior preferences,
which override generic model-stage guidance. Do not parse prose for contradictions.

`t workflow add NAME [--from NAME]` copies without selecting, `use NAME
[--repo ALIAS]` selects and clears exclusion, `exclude --repo ALIAS` preserves its
source but suppresses the entire workflow, and `inherit --repo ALIAS` removes the
override. These direct actions work without a terminal. `list [--repo ALIAS] [--json]`
is a read-only stage/model/effort table with source, exclusion, model-stage state,
behavior preferences, custom-text presence, supplied issue-rule inclusion and precedence. `list --all [--json]`
is the named catalogue and usage; reject --all with --repo. `edit [NAME] [--repo ALIAS]`
is the advanced JSON editor; reject NAME with --repo. Omitted selectors target the
current global default. Repository `null` resets inheritance.

`t workflow instructions show [NAME] [--repo ALIAS]` prints only raw Markdown,
without prompts, model discovery or writes. `instructions edit` uses a private .md
file in VISUAL/EDITOR, preserves formatting and editor status, and rejects embedded
managed-block markers. Repository edits create an inline policy only after an actual
change and preserve exclusion; inspection includes the stored prose when excluded.
The issue-before-code rule is seeded once as editable Markdown in schema v3.
Reading v1/v2 migrates in memory without writes; installation or explicit save
persists v3 and replaces the legacy public-issues block transactionally. Existing
custom prose is preserved, and later installs never restore a deliberately edited
or removed rule. Fresh installs save editable defaults.
Excluded or disabled scopes suppress their prose and preferences in generated native
instructions. Disabling model stages alone explicitly suppresses t model delegation
while retaining preferences and prose. The JSON policy optionally accepts
`stages_enabled` (default true) and `behaviors` (default absent); older v1/v2
documents remain untouched on reads and migrate when saved or synchronized.

Guarded dormant Codex resume/restart and app-to-CLI handoff read the latest
applicable explicit main model/effort checkpoint immediately before constructing
the launch command. Those overrides replace saved thread model/effort, with
explicit invocation overrides taking precedence. Repository exclusions (including
linked worktrees), indexed child agents, changed source guards, and malformed
checkpoints suppress these overrides. Speed and permissions are retained. A live
attach preserves its existing owner, schedules a bounded service retry, and
explains that isolated sessions require guarded resume/restart. Attaching does
not restart a process or begin a turn. Native desktop URL reopen has no verified
model override; its runtime/instruction refresh remains unverified.

The installed package includes a pinned, licensed private Tomli fallback for
Python 3.9 hosts. Update/install needs no network package download or system pip
changes to parse native settings and tool inventory. The owning app-server proxy
is a raw WebSocket tunnel: reconciliation performs the HTTP Upgrade handshake
and masked framing, without starting another backend. Only threads loaded by
that service are reachable; isolated `--no-daemon` owners remain pending until a
guarded resume boundary.

Successful workflow mutations publish native instruction files atomically, then
reconcile an account-local revision outside the save lock. `t workflow sync`
retries pending work; later saves and SessionStart launch/resume hooks also retry.
`list` and `list --json` report saved propagation status without writing or
connecting. Explicit sync also adopts saved preferences that predate propagation.
Unused named-definition edits do not affect unrelated consumers.

Workflow stage models remain guidance and never select a main-session model.
Explicit Codex model/effort changes in `t config` separately reconcile loaded root
threads through the running app-server control socket. Pagination is bounded;
stage agents are skipped. Explicit main model/effort deltas also persist through
the receiving host's authoritative launch-config writer and account-owned native
Codex root defaults. Unsupported TOML/ownership or unavailable writers leave
those defaults explicitly pending. No other native fields are changed. Runtime coverage is limited to the connected owning
service; standalone, unloaded, and other-runtime sessions remain unverified. Only changed model/effort fields are sent. Accepted
updates affect subsequent turns; current turns, native instruction reload, and
speed inheritance remain unverified. Speed, permissions, collaboration mode,
conversation messages and transcripts are untouched. Clearing a launch default
requires agent reload and is reported pending.

Registered SSH hosts receive bounded nonrecursive `zsh -lic` reconciliation.
Pending hosts and scope batches advance fairly across retries; identity discovery
also keeps a durable cursor. Repository scopes cross hosts only as verified GitHub origin identities, resolved
against each host's registrations; local absolute paths are never copied as scope
keys. Unreachable hosts, unsupported runtimes, ambiguous/unmapped identities,
and attempt limits remain explicitly pending. The private revision journal stores
configuration and acknowledgement metadata, never transcripts. No launchd or
personal dotfile integration is required for the installed SessionStart hook.

Global is the default scope; --repo explicitly selects a registered alias with no
checkout inference or picker. Bare subgroups show help. Hidden config show/edit
--workflow compatibility forms retain their behavior. Bare config edit retains its
local-shell editor. Saving updates account-local Codex and Claude instructions with
preflight and concurrent-edit checks, never tracked repository instructions. v1
policies migrate in memory without writing on reads or installer synchronization.

Test runners appear within each workflow policy. Their JSON subeditor exposes targets,
default targets, timeout/queue deadlines and artifact paths; Show configured targets
prints a reviewable summary. Menu changes stay staged until existing Review and Save.
`t runners list [NAME] [--repo ALIAS] [--json]` is a local read-only
list; `edit` opens the terminal editor, and `setup --github-repo OWNER/REPO [--create]`
explicitly bootstraps a dedicated private broker. Configuration save never provisions.

`t runners runs start [--repo ALIAS] -- COMMAND ...` is an explicit direct
action using the registered current checkout or linked worktree; --repo must match.
Repeatable --target, --os and --capability select configured targets without a picker.
The argv after -- belongs to the test command. JSON remains machine output and does
not make start read-only. Runs list/status/logs read only local durable receipts,
without provider probes, cleanup, update prompts or selection. Artifacts/cancel/cleanup
are explicit actions. One immutable current-file snapshot serves concurrent targets;
Ctrl-C requests cancellation and returns 130, preserving honest unknown outcomes if
provider termination cannot be confirmed. See [test runners](../docs/test-runners.md).

### Representative screens

```text
dev sessions   ● attached/loaded · ✓ active context · ↻ still in progress

  STATUS  UI         MODEL    SESSION  WORKING ON
  ○       › cli      ⬡ codex  api-2    Fix login
  ○       ▣ desktop  ✱ opus   web-1    Review UI

  reattach: t open <repo> <slot>

Enter open/Esc · Space/Tab ✓
Ctrl-F filters · Ctrl-A off · Ctrl-L on
Filters ON: unattached · 2 hidden
  SESSION  WORKING ON
▸✓ ○ api-2  Fix login
```

The rows above illustrate hierarchy and controls, not a fixed column schema.
List commands print normally; picker headers and marks appear only for selection.

The open picker hides attached CLI and loaded desktop sessions by default.
Ctrl-F opens the shared single-picker Filters settings. Choose State for attached,
unattached or all attachment states, or choose a column for searchable exact values.
Open includes agent, progress, interface, model, host, repo, session, context and task;
resume includes agent, host/origin and repo; close includes agent, interface, host
and repo. Resume State includes saved-only/live; close State independently includes
attached and pending work. An explicit All choice clears one column.
Reset filters to defaults stages the original state and clears column choices.
Apply filters commits staged choices and enables them; Esc discards settings changes.
Esc in a nested value/state picker keeps its existing choice.

Ctrl-A turns filters off without changing saved choices; Ctrl-L restores those
choices. Both are idempotent. Opening settings while off retains the saved values.
The compatibility --attached flag starts with filtering off and defaults retained.
There is no new-session shortcut or details preview in open; use `t open --new`.
A plain green ✓ selection marker stays readable on the dark cyan focus row;
never paint a reversed-color block behind it. The marker occupies its own gutter
beside the focus pointer, separate from status marks in the row.

All-repository resume discovery (`t session resume --all`, or outside a checkout)
includes local registrations, repositories registered only on reachable hosts,
and orphan Codex rollouts with exact Git repository metadata. Discovery transfers
metadata only; canceling the picker never clones, registers, or downloads a
transcript. Selecting a saved conversation can clone and register its missing
local repository through the normal checkout safety checks. Repository aliases
and paths may differ across hosts. GitHub transfers require authenticated matching
repository IDs; names and directory basenames alone never establish equivalence.
`t remote resume --all --host HOST` explicitly keeps this scope on the chosen host.
An explicit or inferred locally registered repository resolves there by identity.

After a cross-host saved selection, resume reconciles the latest durable source
workspace association under conversation and workspace claims. It verifies repository,
branch, generation and released ownership, checkpoints eligible outstanding code
with normal Git hooks, and transfers the verified tip privately into a fresh safe
destination. Safety or checkpoint refusals stop launch; unreachable or unsupported
source helpers warn and retain the locally available revision and transcript path.
Lists, discovery and canceled selections never create checkpoints.

Resume never invents a historical UI. Resume uses actual user-turn timestamps,
newest first by default, with unknown times last; live rows have no artificial priority.
Alt-T selects last turn, Alt-R repository, Alt-G agent, Alt-O host/origin, and
Alt-V session (natural numeric order). Text modes ascend with newest turn and
stable identity as tie-breakers. Sorts apply within the private filtered snapshot
and preserve query and identities. Filter settings, reset and on/off preserve sorting.
Compact headers use M for Alt and ^ for Ctrl. Resume disables fzf relevance sorting
so searching preserves the chosen order. Saved transcript mtime/reopen time still
controls --days eligibility.
Every column value comes from raw metadata,
not truncated display text. The progress value exactly follows the ↻ marker:
work continued after a merged PR; its absence is not completion evidence.
A row's ✓ means loaded conversation context, independent of attachment and
current computation; the selection gutter ✓ marks an action target.

Query and visible selection identity survive supported reloads; hidden marks
are removed. Old fzf clears marks on filter actions. These remain filtered/multi
modes, not new commands or discovery operations. Every shortcut and a focused
row remain visible at 80×24, 44×20, and 32×12. Short terminals use a borderless
full-height view, including nested value pickers. Narrow headers use ^ for Ctrl
and show the selected column count; full values are available in Filters. One cyan
summary highlights enabled constraints, with explicit ON/OFF text in monochrome.
Clip plain text before adding ANSI color. Filter metadata comes from the same
private snapshot as the rows. A named repository and prior discovery limits remain in force.

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

Saved Codex resume and handoffs also resolve unindexed held sibling locks from
validated `session_meta` identity in active and archived rollouts. If that identity
is not indexed or discoverable there, bounded open-file inspection of the exact
lock holders can resolve it from their open rollout metadata. Process working
directories and other conversations on a shared backend cannot establish it. A verified
different workspace does not block resume; selected and same-workspace writers
still block, including equivalent real paths and any matching workspace among
multiple metadata records. Unknown, invalid or unreadable ownership evidence
prevents resume. Ownership refusals print a concise
diagnostic naming the unresolved conversation and verified holder PIDs, when
available, without exposing process arguments. Finish and exit that CLI, or finish
the turn and close its owning app; allow the shared backend to release its writer,
then retry resume. Do not delete writer locks or edit ownership databases. Refusals
include retry guidance without a traceback or success descriptor.

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
`remote open`, `remote move`, and `remote resume` require explicit verbs.
Host picker headings state the action: Open a session on which host?, Start a
new session on which host? with `--new`, Resume saved work on which host?, and
Move this session to which host?. Resume explains that it browses that host's
saved conversations; the selected host also runs the resumed session. Move
labels the destination and current source host. Aliases and SSH targets remain
subordinate to the action heading. Missing
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

`session resume` refreshes saved metadata on registered hosts by repository identity
on every scan; `--remote` remains a compatibility option. The picker includes
canonical checkouts, swept Codex rollouts and renamed roots whose recorded Git
origin exactly matches the configured repository. Only the selected transcript
is downloaded and verified. A different live conversation in the historical
slot does not hide saved work: inactive selections receive a safely allocated
workspace when their original workspace cannot be reused. Exact live IDs attach
to their existing owner. Local paths and remote aliases need not match.
An explicit slot (`t session resume api 2`, including the `t resume` alias)
scopes the shared filtered multi-picker to all recorded associations with that
slot, including conversations whose latest association is elsewhere. In a
terminal it always waits for selection, even with one row, and initially shows
live and saved rows; Ctrl-F restores inactive defaults. Cancellation returns 130
without attaching, launching, or allocating a workspace. Without a terminal,
an explicit slot retains newest-saved selection. Existing `--days` limits apply.
Progress spinner and canceled picker children never run the caller's shell
session/history exit hooks; the caller's own hooks remain unchanged.

`remote open [host] [repo] [slot]` and `remote resume [host] [repo] [slot]`
use host-first targets. Explicit `[repo] [slot] --host HOST` retains compatibility;
ambiguous legacy repository-only targets fail with guidance before SSH.
`remote move` retains its source/destination flags and repo/slot positionals.
Remote open reads and scopes inventory on the selected host, then uses the shared
local picker and opens extra local tabs before the first attachment. Failed tab
creation reports an exact attach command and a nonzero result. Desktop rows are
excluded from this CLI-only remote picker.

`remote open` selects current sessions on the chosen host and never starts fresh
from an empty picker; `--new` is explicit. `remote resume` selects saved history
on the chosen host using the existing filtered resume picker. `session resume
--host H` sends the selected saved history to H. Live rows attach only
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

Automatic fresh-slot allocation skips known remote desktop reservations from
one discovery snapshot; explicit slot requests still refuse conflicting owners.
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
local and reachable sessions remain listed. Failed remote scans include the exit
status and up to 1024 bytes of terminal-safe SSH stderr; exit 255 reports an SSH
scan failure rather than asserting that the host is offline. Successful scans
suppress SSH stderr, and scanner stdin is disconnected. Redirecting stdout alone keeps
warnings amber when stderr is still a capable terminal.
Picker helpers return without running the caller's
shell exit hooks or saving its history. Zsh picker profiles wait for fzf in a
noninteractive worker, retain ownership through child stop/resume, and clean up
their private files after completion or cancellation. All Codex
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

### Local model setup

`t config local` and the settings hub's **Local worker & models** entry use the
shared Rail UI to review prerequisite installation, model downloads, and Codex MCP
registration. Pending hub edits must be saved or discarded before opening setup.
`t config local --model NAME --yes` applies explicit setup without prompting;
`--disable --yes` disables delegation while retaining models and registration.
`t config local --show` is a read-only detail mode: no server startup, download,
registration, picker, or automatic update prompt. Cancelling review returns 130.
Completed installation steps remain after a later failure so setup can be retried.

## Help style

Every public command, resource group, conditional mode, and compatibility alias
uses the shared quick-reference layout. `-h` and `--help` are equivalent and
side-effect free. Start with usage and a summary of at most 60 words. Show
arguments, public options, and at most three practical examples; each argument
or option explanation has at most 24 words. Keep defaults, repository inference,
selection behavior, and ownership requirements visible where they affect use.
Use uppercase section headings and at least two spaces of indentation for rows.
All help headings use bold cyan in a color-capable stdout terminal, including
root, group, command, detailed, agent-support, and alias-reference help. Only
heading labels are styled; an inline `usage:` label resets before the command.
Body text and commands stay plain. Color is disabled when stdout is redirected,
`NO_COLOR` is present (even empty), `TERM=dumb`, or `CLICOLOR=0`; `CLICOLOR=1`
does not force color into redirected output. Wrap plain text before styling:
removing ANSI escapes must reproduce plain output exactly, including newlines.

Quick help fits within 60 physical lines at 80 columns; resume fits within 45.
Cap output at 80 columns and wrap at 44 and 32 columns without breaking command
names, flags, paths, or other tokens. A single unbreakable token may exceed the
terminal width. Never truncate help or hide usable options to meet a budget.
Each page points to `t help <command>` for further information. Keep longer
explanations in explicit paragraphs and topical sections in `help_details`,
using the existing parser and renderer. Detailed topics and the agent-support
and alias-reference topics have no quick-help line limit.

The executable budgets live in `command-contract.json` under `help_style`.
`scripts/lint_cli.py` renders root, group, nested native parser, canonical,
mode-specific, alias, detailed, and special-topic help at all three widths.
CI checks exact bold cyan heading styling, plain body text, all color opt-outs,
and identical visible text and layout with color enabled. CI rejects oversized summaries,
option text, pages, missing public options, and undiscoverable commands. Add or
update help regression tests with each public command change.

Resume discovery checks effective GitHub authentication once per participating
machine with a bounded, noninteractive probe. Confirmed invalid or missing
credentials produce one warning per host before selection, including empty and
automatically selected results. A compact warning remains above the resume
picker controls through filtering, sorting, show-all, and reset. Available saved
conversations remain usable. Run `gh auth login --hostname github.com` on the
affected host, then retry. Remote resume names the selected SSH target. Network
failures, missing `gh`, permission errors and rate limits do not imply invalid
credentials. Private machine protocols retain their original output unless
diagnostics are explicitly requested.

The internal `t mcp` machine interface discovers saved Codex and Claude sessions.
Its explicit `--install` action registers installed clients additively, preserving
existing entries; `--call`, `--list-tools`, and stdio never install clients.

`t runners` is the test-runner noun family: `list`, `edit`, `setup`, and nested
`runs start/list/status/logs/artifacts/cancel/cleanup`. Bare families print help.
The old `config workflow runners` and `config workflow runs` routes remain hidden
compatibility aliases, as do `workflow runners` and `workflow runs`. Config → Test runners chooses the current global default,
a named workflow, or a registered repository explicitly; it shares staged workflow
configuration and the existing review/save/discard flow, with no separate runner store.
