# Command and terminal contract

This is the repository's terminal design system, indexed by [DESIGN.md](../DESIGN.md).

## Planning previews

When a workflow or task calls for a planning preview, use a local interactive HTML
prototype to demonstrate the proposed command flow before application changes.
Follow this terminal contract: shared layout, focus, keyboard controls, spacing,
and narrow-screen behavior. Show affected navigation, selection, review,
cancellation, and relevant empty/error states. Label sample data and simulated
actions. Keep real agent sessions, repository files, configuration, and services
untouched by prototype interactions. Make the preview easy to open locally and
report browser or verification limitations accurately.

Start with orientation: explain in plain language what the preview demonstrates,
which user task it helps with, and what feedback is being requested. Begin at the
real command or existing navigation entry point and let the user follow the path
into the proposed screen. Show the command and navigation path, including where
Back returns. Clearly label proposed entry points that do not exist yet.

Do not drop the user into an unexplained internal screen. Provide a short guided
example using a recognizable user task before offering scenario switches or
advanced controls. Explain sample state and unfamiliar terms where they appear;
keep prototype controls visually separate from the proposed product interface.
The preview must make sense without the planning conversation: the user should
be able to tell what they are looking at, how they got there, and what to try next.

Show the complete parent menu in its real order and grouping. Do not replace
`t config` or another entry point with a shortened excerpt, omit sibling actions,
or add inert "existing setup" placeholders. Parent and sibling navigation must
work with isolated sample state, including selection, editing, review, save,
discard, and returning to the same focus. Simulate external effects explicitly.

After design approval when required by the workflow or task, implement using the
shared terminal components. A planning prototype does not verify the working CLI:
prepare a separate worktree-scoped command preview with isolated sample settings
at the selected delivery checkpoint. Never relink shared installed commands to a
development worktree. Workflow configuration owns preview timing and approval;
this document defines how to make the previews faithful and safe.

## Workflow settings and resets

Group the main workflow editor into Models, Behavior and Manage using shared
RailUI sections. Use this workflow stays above the groups. Keep focus on the
selected row when returning, including after a reset; headings are not selectable.

Every settings page offers Reset to defaults, and individual selectors offer a
resolved default or inheritance choice, including binary toggles. The multiple-model
checklist offers an exclusive Use default preset alongside the exclusive one-model
choice. Reset only settings owned by that page, including hidden fields. Parent
pages include their nested settings: Local preview resets global and both type
preferences; Tests includes runners. Type overrides resume live global inheritance.
Model and permission defaults use the selected native tool; reset never invents an
unverified effective native value. Routing and model resets do not download or run
models, change credentials, or alter readiness requirements.

Reset workflow to defaults restores that tool's built-in policy, preserving the
workflow name, repository assignments, repository design sources, other workflows
and other tool catalogues. Frontend design and Design systems page resets affect
the workflow defaults; only the individual repository source picker resets its
source. Keep legacy choices outside the reset scope semantically unchanged.

All resets share the existing staged review/save/discard transaction, including
completed resets before interruption. Already at defaults is a no-op; browsing and
no-op resets never materialize inherited repository overrides. This extends the
existing workflow wizard/single-selection modes, not the public command surface.

## Repository design-system configuration

Frontend design is a top-level workflow page, above PRs, previews & tests. It
contains Design approach and Design systems. Design systems shows a Default
section (Find design systems: Automatically / Off), then the registered repositories
using that workflow, including inherited assignments. Each repository shows its
resolved source: Auto · DESIGN.md, an explicit path, Off, Auto · Choose a source,
or Auto · No system found. Do not display explanatory paragraphs in these pickers.

The repository source picker uses separate Default, Choose a source, Manage and
Navigation groups. Use workflow default restores live inheritance; explicit paths
and Off override it. Each candidate appears once, with evidence and a selection
mark. Choose file or folder, Create design system, and Scan again are actions.
Scan again explicitly refreshes the invoking worktree and registered checkout,
then inspects the latest default-branch commit advertised by origin. It fetches
objects without moving branches, changing FETCH_HEAD or writing checkout files.
Discover candidates from a bounded snapshot without executing repository code or
following symlinks. Deduplicate paths with the active worktree taking precedence;
label candidates found only in the registered checkout or default branch. Selecting
those paths stays staged and explains that the active worktree must be updated to
use the guidance. Remote failures retain local candidates and show an unavailable
warning. Ordinary browsing and read-only commands never fetch.
Retain missing explicit paths with a visible error; never silently replace them.
Repository source settings survive assignment changes and are tool-scoped.

Automatic discovery prefers safe root DESIGN.md, then one root design-system.md
or design_system.md, then a sole discovered candidate. Multiple remaining
candidates require a choice. No candidates means no system found; creation is
always explicit. Discovery reads repository-owned docs, linked guidance, tokens,
components and Storybook, excluding dependencies, generated directories and
symlinks. Resolve relative paths in the matching active worktree. Lists never
scan or prompt; managed agent instructions describe the same resolution rule.

Every workflow choice has an effective default. Behavior defaults come from
BEHAVIOR_DEFAULTS; type overrides inherit shared preview defaults. Label the
resolved default and reset destination instead of Not set. Native model, effort
and permission choices may inherit tool settings; never invent an unavailable
native value. Reads do not materialize defaults or rewrite saved choices.
Design approach and source use remain active when previews are disabled.

Create design system is an explicit agent-assisted drafting action. Save or
discard pending settings first. Review the task, then start the existing guarded
agent launcher in a new isolated worktree. Draft an entry point, agent-instruction
reference and evidence-backed design/preview guidance without overwriting existing
content or duplicating workflow approval rules. Drafting does not publish changes
or select a source. Review the actual diff in the agent session, then use Scan again
after merging to select the source, or select it locally from the accepted worktree.

Development availability is scoped to the caller's t worktree. With a canonical
Git installation, each `dev/t-*` or `cursor/*` worktree supplies its own executable
and shell helpers automatically, including subdirectories and invocation-local
child helpers. Leaving the worktree restores canonical behavior. A custom binary
earlier on PATH remains authoritative. Development setup (`system update --dev`
or `T_LINK_DEV=1 ./install.sh`) never relinks global commands or writes hooks,
prompts, shell startup, MCP registrations, workflow preferences, or settings.
Normal installs and canonical updates retain their existing integration behavior.
The installed source selector must be upgraded once for older installations;
development setup itself never replaces that shared installation.

`t --version` is a read-only detail display. Preserve the release/revision on the
first line, then show labeled `Source` and `Worktree` paths with their branches.
Source means the code actually running; Worktree means the current directory's
Git root, which may differ. Label packaged sources `release`, detached checkouts
`detached HEAD`, and a non-Git working directory `none (outside Git)`. Escape
control characters in paths and refs, and never prompt or check for updates.
Resolve these details only when version output is requested.

Every public `t` command and conditional mode has an entry in
[`command-contract.json`](command-contract.json). That inventory is the source of
truth for classification and contract tests. Compatibility aliases inherit the
canonical command's entry. `ls` and `list` are interchangeable in every listing
family (including `workflow`); the former `t config workflow` routes remain
compatibility aliases for `t workflow`. `t workflow open NAME` and the shorthand
`t workflow NAME` open a named definition directly in the staged editor; both
accept `--tool`, otherwise using the account tool. Verbs take precedence over
names, so use `open NAME` for collisions. Unknown names and nonterminal opens
fail without writes. `t workflow delete NAME [--tool TOOL] [--replacement NAME]`
is a direct action: require a replacement for a default or any repository
assignment (including excluded ones), preserve exclusions and other catalogues,
and reject deleting the last definition. Editor deletion uses the same rules,
selects a replacement when needed, and stays pending until review/save. Root `t list` aliases `t ls`. Resolve only
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

The Air design applies to every searchable picker and inline menu: airy cyan section
headings and current values, green primary/add actions, amber warnings/removal,
two-space outer margins, a pointer and a dark cyan focus row with light text, and a footer
showing the next action and keys. Focus and marks must remain distinguishable
without color. Headings are cyan, hints muted, and focused text light on dark cyan.
Keep the green checkmark in its own gutter beside the pointer, with spaces between
status icons inside rows. Give controls and results a blank separating row when
height permits. Session pickers list HOTKEYS above FILTERS, with readable shortcuts
and bright underlined labels for the current filter On/Off state, resume sort, and
merged-session toggle when enabled. Session lists highlight selected column filters
and the current sort key. Plain output retains explicit state text.
Use FILTERS only for actual filter controls; other pickers use their
action or resource title. Reduce spacing, borders and margins on short screens
before losing controls or focus, including wide 120×12 terminals.

Display native Mac labels (`⌃T`, `⌃S`, `↩ Return`, `⎋ Esc`, `⇥ Tab`) on macOS;
other hosts use `Ctrl-T`, `Ctrl-S`, `Enter`, `Esc`, `Tab`. Compact headers use the short
Mac symbols or `^T` on other systems. Only labels change: bindings remain the
same control keys, Return, Escape and Tab. Align labels on wide terminals and stack them below 60 columns.
Let label columns use available width before truncating; never cap wide-terminal
labels at 24 characters. Clip text before applying color and count physical rows when repainting; at
80×24, 44×20, and 32×12 interactive screens the focused row and primary action
must remain visible.
Separate a leading unset/default choice (empty key) from the remaining choices
with a nonselectable blank rail row in every shared picker. It adds no option or
explanatory heading. A checklist with an explicit empty choice treats that row as
an exclusive reset: selecting it clears all other marks, selecting a real option
clears it, and unchecking the last real option restores it.
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
| **List** | Print a scoped, aligned table immediately. Name its scope, label columns, and give a clear empty state. No picker, confirmation, or update prompt. `t issue list` is an example. |
| **Detail/status** | Print labeled facts without changing state or prompting. Preserve raw text, paths, and streams when the command explicitly exposes them. |
| **Single selection** | Use a searchable fzf picker or an inline `_RailUI` menu with one visibly focused row. Enter chooses exactly one item; Esc/Ctrl-C cancels. An explicit target bypasses selection. A paginated single picker uses Ctrl-F for the next page, Ctrl-B for the previous page, and Ctrl-R to refresh; those keys belong to pagination rather than the filtered picker's settings control. |
| **Multi-selection** | Space types into search. Searchable pickers use Tab to mark rows and Enter to accept the marks, or the focused row if none are marked. Inline checklists use Space to toggle and Enter to submit only checked items. Show marks and the Enter action independently of color. |
| **Filtered selection** | Show active filters and hidden count in the header. Open, resume and close pickers use Ctrl-F to toggle filters, Ctrl-X to reset, and direct shortcuts to match the highlighted row. No nested filter screens. Ctrl-S toggles sorting; Ctrl-Y cycles sort choices. Filtering retains the search and removes hidden marks. Issue pickers retain Ctrl-A show-all and Ctrl-F defaults. Ctrl-/ toggles full details when rows have previews. |
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

The config hub groups Session defaults, Agents, Workflows, Runners, MCP server, Repositories &
worktrees, Remote hosts, Sleep & connectivity, and Finish. Keep agent settings and local worker/model
setup together, with workflow in its own section; keep repository/worktree choices
separate from
host registration and the default beam host. Settings collections and setup
destinations use a trailing `›`; scalar choices and immediate actions do not.
All actions remain directly reachable, returning preserves row focus, and the
shared viewport keeps the focused row and Save shortcut visible on narrow screens.

Cross-host moves and resumes use guarded t transfer for both Claude and Codex.
Worktree transfers preflight push access before stopping the source, then publish
final edits under a unique `refs/heads/t-transfer/<token>` ref. Destination checks
require snapshot-capable peers before stopping. Landings fetch the exact snapshot
into `refs/t/transfers/<token>`, never overwriting an unrelated ordinary branch or
falling back to main. Conflicting destination slots get fresh branches. Successful
tmux landings remove only the matching remote snapshot with an exact lease; failed
cleanup retains it for recovery. Git failure diagnostics remain visible.
When no current owner is found, move points to `t session resume [repo] [slot]`
for restoring saved work.
Legacy `DEV_HANDOFF` and desktop opening defaults are ignored. Older remote peers
receive explicit t transfer overrides. Codex Desktop launch, handoff, reservation
creation and discovery are removed. Historical interface metadata stays readable.
Old reservation markers protect worktrees from automatic cleanup; exact CLI history
resume checks writer ownership before starting.

Config → MCP server → Sessions server shows read-only Codex enabled state and
Claude user-scope registration. Browsing and Refresh status never start servers or
write settings. Register missing clients now uses an explicit immediate-action
review, blocks while hub edits are pending, preserves existing registrations and
disabled entries, and restores the terminal around the existing installer. It is
separate from config Save. Registration is not proof of an active connection;
reload the client after registering. No remote fan-out occurs from this menu.

`t mcp status [--json]` is read-only client configuration inspection.
`t mcp install` adds missing registrations, preserving existing disabled/custom entries.
`t mcp uninstall` removes only t-owned stdio registrations through client writers,
preserves permissions and custom entries, and persists an automatic-registration
opt-out until explicit install. These commands never select or update remote hosts.
Bare `t mcp` remains the stdio protocol; legacy flags including `--install` remain
compatible. Management verbs reject combinations with protocol flags.

Config proposes unique host short names from SSH targets, including the first
manual addition. Onboarding also generates unique short names for selected peers.
Hosts → host → Short name edits an existing alias while preserving its SSH target
and default-host references. Changes remain staged until review and save.

Config quitting with pending edits offers Keep editing (default), Review and
save, and Discard changes and exit. Review screens keep their primary action
visible while scrolling.
Ctrl+C in `t config` and `t workflow` uses that same unsaved-change choice from
the hub, nested settings menus and the save review. Preserve completed selections
from nested editors before showing the prompt. Keep editing retains all staged
changes; Review and save opens the normal review before writing; only explicit
Discard changes and exit discards them. Interrupting the prompt never implicitly
saves or discards changes. With no edits, Ctrl+C exits immediately with status
130. Restore terminal state on every exit. These rules apply to the staged menu
transaction; external editors and post-save operations retain their own behavior.

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

Config includes Workflows globally and in each repository submenu. Start with a
Codex, Claude, or Cursor tool picker, then show that tool's named workflows,
default and repository assignments. Tool selection is navigation only; it never
changes session launch tool preferences. The full staged document and immutable
baseline span all tool menus, including interrupts, review, save and discard.
Show Save changes once in the persistent footer, with the `s` shortcut; do not
duplicate it as a row in config, the tool chooser or the workflow catalogue.
The per-tool menu chooses a named default, adds or edits workflows, or activates recommended
defaults. Hub and catalogue rows show Global default, Assigned to N repos, or
Not assigned, with Disabled appended only for a disabled definition. Count
non-excluded named assignments; exclude inline copies and implicit global inheritance
from explicit repository counts. An enabled definition is not proof of assignment.
The catalogue uses APPLIES TO instead of ambiguous STATUS on/off and ASSIGNED columns.
Each workflow exposes an overall Workflow switch and an independent
Use multiple models checkbox picker with a leading Use lead model for all stages
choice, then Planning, Implementation and Review. Use the shared empty_choice
multi profile: the one-model row is an exclusive reset, separated by a blank
rail row. It clears all stages; selecting any stage clears it, and clearing the
last stage restores it. Translate the empty-key selection into no active stages.
Space marks choices; Enter and q/Esc Back retain the selection as pending edits. Unchecked Planning/Review use the lead model; Agent review before PR controls
whether the pre-PR review step runs. Unchecked Implementation uses
per-tool session defaults, with default model/effort controls still available
beside selected helpers. Selecting no stages enters single-model mode.
Model catalogues come from the selected tool. The workflow row is Lead model;
its reset choice says Inherit default and shows the top-level tool model.
Lead reasoning effort shows an explicit level or Inherit default with the known
top-level effort. Unset effort inherits top-level effort only when the model is
also inherited; an explicit workflow model leaves effort to the native tool.
Unknown native values say Tool setting; model catalogue metadata alone is not
proof of the effective native configuration. Permissions shows the preset
without repeating the selected tool name and opens its choices directly.

Use Lead model consistently for the workflow's main conversation model: its
selector title, review/save changes, read-only workflow summaries and inherited
stage source labels. Use Lead session when describing the conversation or
execution owner, and Lead reasoning effort for its effort control. The lead
coordinates work and owns the result; it can also do implementation itself.
Planning, Implementation and Review remain stage names; explicitly delegated
models remain workers. Use lead model for all stages describes the stage reset
without implying that separately configured local delegation is disabled.

Lead is a role, not a provider or execution location. Retain Local model in
Inference & routing and Cloud/Local where describing execution; Local only uses
the selected local model as the lead. General tool model settings and model
catalogues retain their unqualified Model labels unless selecting a workflow
lead. This wording change does not rename JSON keys, commands or runtime APIs,
change launch precedence, or switch any active session's model.
Cursor uses model IDs with any supported parameters; do not expose a separate
reasoning-effort flag or unsupported permission presets. Unavailable model
catalogues retain inheritance and custom model entry without invented choices.
Deselecting stages retains their model/effort settings and keeps
permissions, behavior preferences and Markdown active; disabling the workflow
suppresses every workflow setting. Selected stages expose separate
model and effort controls, plus Instructions for ordinary Markdown in VISUAL/EDITOR.
PRs, previews & tests uses shared `_RailUI` submenus for Pull requests, Local
preview and Tests. PR opening (automatic/on instruction), initial
state (ready/draft) and merging (automatic/on instruction) are independent.
When to open PR offers Automatically when ready or Only when I ask.
Agent review before PR is an Enabled/Disabled toggle with no Not set choice. Omission
means Enabled without changing storage on read or an unchanged selection. Enabled requires a diff and
validation review before PR opening; the selected review worker handles it when
enabled, otherwise the main agent does. Disabled suppresses the pre-PR review
pass in both the main conversation and a selected review worker without deleting
its saved model, effort, or stage selection. The menu and effective stage displays
identify it as Skipped before PR, rather than presenting a reviewer as active.
Choosing Enabled restores normal review
guidance. Delivery instructions retain fixes and the selected validation/preview,
PR timing/state/merge requirements. This preference does not bypass repository
review requirements or required approvals. No new command or execution mode is
added; this remains a staged preference in the shared editor.
Local preview has a Global section with Preview timing (Automatically
when ready / Only when I ask), Approval before PR (Wait for my approval /
No approval needed),
followed by a Preview types section containing
Frontend and CLI. Keep these submenu summaries concise instead of repeating
every nested preference. Each submenu starts with Preview (Enabled / Disabled).
Remove the separate Applies to control. Both submenus stay visible and show
their enabled/disabled state; disabled settings remain saved and editable.
Optional frontend_enabled/cli_enabled values override legacy preview_target for
each type independently. Without flags or a legacy target, both are enabled.
Read-only operations do not migrate storage. Each submenu's Preview timing can
Inherit global or override it independently. Show the resolved timing and source
in detailed rows and printed lists: Inherited · value or Override · value.
The Inherit global choice is separated from explicit values and removes only that
override. An absent parent resolves to the built-in default; display its effective value. Changing a parent
updates inheriting children while preserving explicit overrides, even overrides
equal to the parent. Resolved rules apply only to enabled types; with neither
enabled, suppress preview guidance.
Disabled frontend suppresses preview method and browser guidance; design approach and design-system use remain active;
disabled CLI suppresses its method and command-launch guidance. Enabling alone does not request a
preview or change the separate PR-opening preference.
Both submenus inherit Global timing and approval before PR; each field shows
Inherited or Override with its resolved value. Dim the inherited value after
Inherit global/default in both value rows and choices, including focused rows;
clip plain text before applying color and preserve narrow layouts.
Launch has one independent control per type: Frontend Open in browser and CLI
Run command. Do not show a shared Open/run preview row. Legacy preview_launch
remains readable as a fallback; editing either launch control materializes both
resolved choices and removes that legacy key, preserving the other type's choice.
Reading, browsing or canceling does not migrate stored settings.
Launch choices never start a request-only preview before the user requests it.
The nested Frontend submenu contains Implement before approval (Yes / No, with
Use default), Preview method (Fast reload / Build), and Open in browser
(Automatically / Only when I ask). Design approach is on the top-level Frontend
design page (Separate HTML prototype / In the app). The nested CLI
submenu contains Preview method (Install locally / Run from the repo) and
Run command (Use default / Automatically / Only when I ask), with Use default
separated from explicit choices. Open in browser has the same choices.
The optional cli_launch key accepts automatic/on_request and remains saved while
CLI preview is disabled. Automatically launches the supported CLI preview command
only after the selected preview is requested or due and prepared; request-only
timing still takes precedence. Only when I ask prepares the command without
launching it, including with the Run from the repo method. The method determines
the executable source. Interactive commands use a supported interactive terminal;
if unavailable, report the limitation and exact command rather than claiming a
launch. This preference does not run tests or alter PR choices. CLI planning previews
have separate optional timing (Before implementation / Only when requested) and
approval (Required / Not required). Both offer Use default with its effective value; neither changes working-command
preview timing or launch. Required approval applies when a planning preview is due
or requested, allowing prototype work before substantial implementation. Retired
cli_implement_before_approval remains inert. Approval before PR is
independent of frontend implementation approval and applies only to enabled
preview types. Clearing a launch choice restores its effective default.
Menu, list, catalogue and save review reflect the choice and disabled state. Printed
lists use the same indentation and hierarchy; summaries include nested choices.
Returning from a child menu restores the parent focus and retains staged changes.
Disabled summaries show the status without presenting retained settings as active.
Tests contains When to run tests and Test runners; remove the separate Test runners row
from the workflow menu. Nested runner edits share the same staged Save/discard
transaction and preserve behavior changes made before or after them.
Browser opening applies only once a local frontend preview is available; it does
not start previews or change resolved timing or approval. Frontend Implement before
approval: No requires a lightweight frontend preview and user approval before substantial
implementation; work needed for the preview itself is allowed. With automatic
timing, prepare that initial preview before implementation. With request-only
timing, wait for the preview request and then approval. Yes permits implementation
without this gate; PR preferences stay independent. Runtime instructions emit
only resolved timing, PR approval, and launch rules per type and the
frontend-only implementation gate.
Optional frontend_preview_timing/cli_preview_timing override local_preview;
missing timing keys mean inheritance. preview_pr_approval is the shared PR
approval choice; frontend_preview_pr_approval/cli_preview_pr_approval override
it. preview_launch is a legacy shared launch fallback; frontend_browser/cli_launch
are the independent per-type launch choices. frontend_implement_before_approval is a
direct yes/no setting, not an override of a shared gate. Legacy
implement_before_approval falls back to this frontend-only setting; explicit
frontend values win. Older preview_approval/design_approval map inversely there.
Retired cli_implement_before_approval remains valid raw JSON but has no effect on
instructions or summaries. Read-only JSON retains raw keys, and clearing the
frontend setting must not revive a retired alias. CLI When to preview prepares
the working command when ready or on request, never a required pre-implementation
prototype. Test timing
is labeled When to run tests and chooses local during work, local before preview,
local before PR, or On PR CI;
required repository checks always apply. Unset rows and picker choices say
the displayed default. Task, repository and text instructions override
the behavior. Menus show concise choices without routine explanatory preambles.
Use inheritance wording only where values really follow a parent: preview
defaults, main-session settings, native tool defaults, and workflow assignments.
The repository workflow UI is a single selector: Inherit global with its named
source, named workflows, and Disabled. Selection stages the assignment and returns;
cancel or reselecting the current value changes nothing. An existing inline
policy may appear as Current custom workflow so it can be retained or restored.
Do not expose copy, edit, exclusion, or advanced JSON actions in this selector.
The tool hub summarizes Global and the current registered repository's effective
assignment, including linked worktrees. Scope discovery is display-only and does
not implicitly select a repository for edits or read-only workflow commands.
Use Automerge for automatic PR merging. Preview methods use Install locally, Run from the repo,
Fast reload and Build. Test choices name Local or On PR CI explicitly.
Clearing a choice removes its key. Preserve focused rows when returning.
Legacy delivery/preview values are interpreted without rewriting read-only
configuration; editing materializes equivalent separate choices. Working-folder
and generic validation choices are no longer in the menu. Existing unrelated
legacy rules are retained and displayed separately; explicit test timing supersedes
legacy validation. Model-stage rows are hidden while stages are off.
Legacy local_preview=before_pr resolves automatic preview and frontend implementation
approval. Explicit frontend_implement_before_approval wins over the old shared
gate and older preview_approval. Edits materialize independent settings without
changing raw JSON on reads. The combined legacy choice is hidden in menus.
Instead show the selected session tool's default model and reasoning effort.
Optional `session_defaults` stores independent Codex and Claude model/effort maps;
these are active for enabled workflows with Implementation unchecked, including
single-model workflows. Repository overrides
apply to the canonical checkout and its worktrees, including destination paths
for new and resumed CLI sessions. Explicit resume arguments win. An explicit model
without an effort uses that model's default, not an unrelated global effort.
Empty choices fall back to account-level agent defaults. Excluded workflows and
workflows with Implementation selected do not apply these session defaults. Never write scoped settings into
global agent settings or change active turns; indexed Codex child agents retain
their own settings. The UI discovers supported efforts from the selected tool.
Saved enabled workflows with Implementation selected use the implementation model for Codex root
launches and guarded resumes. An automatic implementation effort resolves to medium
for the main conversation; explicit efforts remain explicit. These scoped values
override stale resume checkpoints, including a previous planning model or effort.
Every stage offers Agent selects by task alongside explicit effort levels.
Reasoning pickers use shared nonselectable Automatic and Fixed effort section
headings: Agent selects by task belongs to Automatic; explicit levels belong to
Fixed effort. Preserve the selected effort when reopening the picker. Use the
same labels in menus, choices, save review and printed lists. Selected Implementation with auto
effort delegates a bounded worker at a concrete supported effort;
explicit Implementation effort keeps the work in the main session at that level.
Show the main session's fixed model/effort separately when a worker implements,
and identify worker versus main-session values. Unselected stages inherit the
main session's actual effort, not the worker's task-based setting. The generated
instructions require reporting unavailable delegation instead of pretending the
main session changed effort. No task classifier or active-turn switch is implied.
Unsaved recommended defaults supply no launch overrides. Claude launch settings
are not inferred from Codex stage models. Planning, review and automatic
implementation use bounded delegations only when selected, with explicit model/effort and fresh context when
required by the host. Optional model_stages is a nonempty unique subset of
planning/implementation/review. Without it, legacy separate_stages selects helpers
with implementation still active; omission of both preserves all three stages.
Selecting none sets stages_enabled false and retains the saved selection. List and save review label unselected stages as
using the main model and never present inactive model/effort choices as effective.
Legacy one-level-lower values for Implementation and Review are accepted as auto;
there is no relative effort option or delegation rule. Display and reopen these
saved choices as Agent selects by task, preserving stored values on read or cancel.
Native model/effort defaults are shown as defaults when unspecified, never inferred
from inactive saved stages.
Models load lazily from Codex once per menu session; current, recommended and custom
values remain available offline, with explicit Refresh. Changing a stage model preserves
its effort; changing a session default model clears incompatible effort. Shared-definition edits
affect all repository consumers. Repository menus display their source and offer
named assignment, repository-only customization, explicit exclusion retaining the
source, resuming that retained source, or inheritance. All menu changes remain pending until review and Save;
unchanged selections, canceled editors and discarded changes write nothing.
Save review compares the staged document with its initial snapshot and shows
only changed workflows, assignments and fields. Use readable previous/new values,
changed text lines, and changed runner fields; omit unchanged definitions,
precedence and unchanged instruction text. Show additions, removals, default changes,
exclusions and initial activation explicitly. Ctrl+C uses the same pending-edits
prompt while reviewing these differences.
Workflow verbs and instructions support `--tool codex|claude|cursor`; omitted tool
uses the explicit repository's configured tool or the account default. Lists
remain read-only and identify the selected tool in text and JSON. Runner list,
edit and start support the same selector. Retain root v3 data as Codex and store
independent complete Claude/Cursor catalogues in the optional `tools` map.
Existing shared Claude preferences migrate in memory to an independent catalogue
with native model stages; no read writes settings. The next save freezes that
copy. Native Codex and Claude instructions render their respective catalogues.
Cursor's existing launch/resume routes prepare only its resolved workspace policy
in an always-applied managed rule excluded through local Git metadata; refuse
tracked, unmanaged or symlink destinations. Unconfigured Cursor launches and
live attachments remain unchanged. Outside Git, configured launches apply model
arguments and report the unavailable instruction publication. No Cursor dev slots
or unsupported standalone effort/permission controls are introduced.
Permissions is a separate submenu available with either model mode. Show only
the selected tool's supported presets. Codex and Claude have independent native
presets and a Default · Tool permissions choice that removes the
workflow override. The policy's optional permissions object maps codex to
ask/auto/full/read_only and claude to manual/acceptEdits/auto/bypassPermissions/plan;
unknown tools or values are rejected. Missing permissions preserve native defaults.
The list and save review show saved choices. Permissions apply only on CLI start
or guarded resume/restart, after scope, exclusion and root-session checks. Any
explicit invocation permission option suppresses the workflow permission preset
as a whole. A save never changes native permission config or a live conversation.
The host instruction hierarchy is
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
Text instructions opens a page with Edit text instructions, Clear text instructions
and Back. Clear is available on that page even when empty (Already empty is a no-op);
the parent shortcut also appears when text is present. Removal stays in the
same save transaction. Empty editor contents clear text too. Cancellation/discard
preserves saved instructions, and clearing leaves other workflow settings intact.
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

Guarded dormant Codex resume/restart read the latest
applicable explicit main model/effort checkpoint immediately before constructing
the launch command. Those overrides replace saved thread model/effort, with
explicit invocation overrides taking precedence. Repository exclusions (including
linked worktrees), indexed child agents, changed source guards, and malformed
checkpoints suppress these model overrides. Speed is retained. Permissions are
retained unless the applicable workflow explicitly sets a permission preset;
explicit invocation permission arguments win. A live
attach preserves its existing owner, schedules a bounded service retry, and
explains that isolated sessions require guarded resume/restart. Attaching does
not restart a process or begin a turn.

The installed package includes a pinned, licensed private Tomli fallback for
Python 3.9 hosts. Update/install needs no network package download or system pip
changes to parse native settings and tool inventory. The owning app-server proxy
is a raw WebSocket tunnel: reconciliation performs the HTTP Upgrade handshake
and masked framing, without starting another backend. Only threads loaded by
that service are reachable; isolated `--no-daemon` owners remain pending until a
guarded resume boundary.

Successful workflow mutations publish native instruction files atomically, then
reconcile an account-local revision outside the save lock. Later saves and
SessionStart launch/resume hooks retry pending updates automatically.
`list` and `list --json` report saved propagation status without writing or
connecting. There is no public manual retry action.
Unused named-definition edits do not affect unrelated consumers.

The saved implementation stage selects Codex main launch/resume defaults; planning
and review delegation remains guidance. Explicit Codex model/effort changes in
`t config` publish account-owned native root defaults in the same transaction as
the shell defaults and journal, then separately reconcile loaded root
threads through the running app-server control socket. Pagination is bounded;
stage agents are skipped. The external shell editor has already saved its file;
native/journal publication failures are reported as partial saves without claiming
runtime adoption. Explicit main model/effort deltas also persist through
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

Enter open/Esc · Tab ✓
Ctrl-F filters · Ctrl-A off · Ctrl-L on
Filters ON: unattached · 2 hidden
  SESSION  WORKING ON
▸✓ ○ api-2  Fix login
```

The rows above illustrate hierarchy and controls, not a fixed column schema.
List commands print normally; picker headers and marks appear only for selection.

The open picker browses local and registered remote hosts by default; `--local`
limits discovery to this machine. `-r`/`--remote` retains inclusive browsing.
Fresh sessions and explicit targets retain their existing location routing.
Session lists (`t ls` / `t session list`) also include remote hosts by default;
`-l`/`--local` lists only this machine. Repository scope and `--all` are independent
of host scope. Session lists always print a read-only table, including in terminals.

Session picker and history searches accept full literal phrases, ignoring case. Spaces and letters stay in the query; Tab marks rows. Saved-history search covers all pages and cached metadata. Ctrl-S toggles sorting without requiring terminal Option/Meta configuration; Ctrl-Y cycles sort choices.

The open picker hides attached CLI and loaded desktop sessions by default.
Ctrl-F toggles filters without leaving the list; Ctrl-X resets filters. Ctrl-T cycles state and is highlighted whenever the enabled state filter narrows the view, including the default state. Ctrl-G/O/R matches the highlighted row’s agent/host/repo; repeat the shortcut to clear that filter. Ctrl-D matches interface (CLI or desktop) in open and close. Ctrl-S toggles sorting; Ctrl-Y cycles sort choices. Open and resume default to last turn, newest first, and also offer repository, agent, host and session. Unknown turn timestamps sort last. Close offers repository, agent, host and session. Turning filters or sorting off preserves their choices. Sorting off restores snapshot order.

Open also supports Ctrl-P/B/V/E/W for the highlighted row’s progress/model/session/context/task.
Ctrl-A (off) and Ctrl-L (on) remain compatibility shortcuts.
The compatibility --attached flag starts with filtering off and defaults retained.
There is no new-session shortcut or details preview in open; use `t open --new`.
Press `Ctrl-K` to kill only the focused session, independently of multi-selection marks.
The shortcut authorizes that session like Enter in close, retaining ownership
and ownership safety checks. Keep the picker open and remove only successfully
closed rows from its cached snapshot, without inventory discovery. Preserve query,
filters, and other marks on both supported fzf versions. Failed or canceled closes
keep their row and show the outcome in the picker. Closing the last row leaves an
empty picker; Esc exits without starting a session.
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

After a saved selection, local and cross-host resumes reconcile the latest durable
source workspace association under conversation and workspace claims. It verifies repository,
branch, generation and released ownership, checkpoints eligible outstanding code
with normal Git hooks, and transfers the verified tip privately into a fresh safe
destination. Safety or checkpoint refusals stop launch; unreachable or unsupported
source helpers warn and retain the locally available revision and transcript path.
A valid local source carries its current branch tip and checkpoints eligible tracked
and nonignored untracked changes before allocating a new worktree. Missing or
recycled local workspaces retain historical fallback without copying replacement
files. Local transfers do not require a bundle or inherit the remote size limit.
Lists, discovery and canceled selections never create checkpoints.

Resume never invents a historical UI. Resume uses actual user-turn timestamps,
newest first by default, with unknown times last; live rows have no artificial priority.
Ctrl-Y cycles last turn, repository, agent, host/origin, and session
(natural numeric order). Ctrl-S toggles the selected sort. Text modes ascend with newest turn and
stable identity as tie-breakers. Sorts apply within the private filtered snapshot
and preserve query and identities. Filters, reset and on/off preserve sorting.
Compact headers use ⌃ for Control on macOS and ^ elsewhere. Resume disables fzf relevance sorting
so searching preserves the chosen order. Saved transcript mtime/reopen time still
controls --days eligibility.
Resume rows begin with a cached PR badge: MERGED, OPEN, CLOSED, NONE (no PR
recorded), UNKNOWN, or MIXED. Ctrl-D toggles hiding MERGED rows and advertises the
next action in the header. It uses explicit snapshot metadata, never title text.
Compact headers show `M-M -merged` to hide and `M-M +merged` to show them.
For histories with multiple associated PRs, all must be explicitly merged before
the row is hidden; mixed or unknown states remain visible. Live rows without
structured PR metadata stay UNKNOWN. A merged PR does not imply completed work.
The toggle enables filtering, preserves query and sort, and removes hidden marks.
Ctrl-A disables it with other filters; Ctrl-L restores it. Reset clears it.
For example, run `t session resume --all`, then Ctrl-D to hide merged PR histories.
Every column value comes from raw metadata,
not truncated display text. The progress value exactly follows the ↻ marker:
work continued after a merged PR; its absence is not completion evidence.
A row's ✓ means loaded conversation context, independent of attachment and
current computation; the selection gutter ✓ marks an action target.

Query and visible selection identity survive supported reloads; hidden marks
are removed. Old fzf clears marks on filter actions. These remain filtered/multi
modes, not new commands or discovery operations. Every shortcut and a focused
row remain visible at 80×24, 44×20, and 32×12. Short terminals use a borderless
full-height view, including nested value pickers. Narrow headers use ⌃ on macOS and ^ elsewhere for Ctrl
and show the selected column count; full values are available in Filters. One cyan
summary highlights enabled constraints, with explicit ON/OFF text in monochrome.
Clip plain text before adding ANSI color. Filter metadata comes from the same
private snapshot as the rows. A named repository and prior discovery limits remain in force.

The kill multi-picker's Enter action confirms every marked session (or the
focused row), including live local and remote agents; do not prompt again per
target. Explicit close targets retain live-agent confirmation unless `-y` is
supplied.

Saved Codex resumes also resolve unindexed held sibling locks from
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
| Searchable selection | `_t_fzf_single`, `_t_fzf_multi`, `_t_fzf_filtered`, and `_t_fzf_paginated` in `ui/fzf.sh` | Air theme and platform key labels, marks, header controls, filtering or pagination, preview, cancellation |

Set `T_HOME` to the package root, source `ui/fzf.sh`, and select the profile matching the command mode. Callers
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


`session move` selects current CLI conversations through the shared multi picker
when no exact slot or conversation ID is supplied, including inside an agent.
A named repository or current checkout scopes selection; outside a checkout it
shows all repositories, and `--all` widens the scope. `--remote` adds registered
remote sources. Select sessions first, then one destination host; `--host` supplies
that destination explicitly. Esc/Ctrl-C cancels with 130 before starting work and
an empty inventory is a successful no-op. Without a terminal/fzf, require explicit
targets. Exact slots/IDs discover local and registered remote owners before
destination selection, without requiring `--remote`. `--host local` receives here;
`--from`/`--here` receives retain guarded routes.
The legacy `beam` shortcut retains its implicit-current and default-host behavior.

New worktree folders include the repository basename (`t/t-12`); numeric slot
arguments, tmux identity, JSON slot fields and history associations are unchanged.
`remote open` and `remote resume` require explicit verbs; `remote move` remains
a compatibility alias for `session move --remote`. `move` aliases `session move`.
Host picker headings state the action: Open a session on which host?, Start a
new session on which host? with `--new`, Resume saved work on which host?, and
Move this session to which host?. Resume explains that it browses that host's
saved conversations; the selected host also runs the resumed session. Move
labels the destination and verified source host. For remote-only selections it
offers `local` / `this machine` before SSH hosts; selecting local receives here.
For example, `t session move api 2` discovers its owner and offers destinations,
and `t session move api 2 --host local` explicitly brings that conversation here.
Aliases and SSH targets remain
subordinate to the action heading. Missing
hosts use `_t_fzf_single`, displaying alias and SSH target; Enter chooses exactly
one, including when only one host exists. Esc/Ctrl-C returns 130 without starting
work. Missing selections require a terminal and fzf; scripts name their targets.
An empty current-session inventory returns 0. Remote workers suppress nested
automatic update prompts.

`session move` and its `move` alias use the shared multi-selection picker of current
local CLI owners; `-r`/`--remote` adds registered remote sources. `remote move` is
a compatibility route with remote sources included. Selection always opens without
an exact target, including inside an agent. Tab marks sessions; Enter accepts
marks or the focused row. Include attached and detached owners and exclude
desktop reservations, saved history and unknown IDs. Select sessions first, then
select one destination; explicitly named destinations skip the second picker.
Multiple sessions move sequentially and detached. Stop on the first failure or
cancellation and identify completed, failed and unattempted sessions.
Repository scope accepts matching home-relative checkout/worktree paths across hosts.
When a remote path differs, its source registry must resolve the repository selector
and return the same live owner before it is included. If a named repository is absent
locally, each remote source resolves the selector in its own registry. Ambiguous
selectors or failed scope verification stop selection. A unique repo/slot match
goes directly to the destination-host picker when `--host` is omitted. Duplicate
slot labels retain their host identity. Explicit source plus slot/ID bypasses the
session picker; an explicit target without source resolves a unique snapshot or
requires selection when ambiguous. Destination selection excludes every alias of
every selected source target (including local), and never silently chooses the
default host. Local receives honor `--detach`, preserve the selected conversation
identity through a source-side recheck, and stop on failed stop/push/sync steps.
Recheck host, ID,
path, label, agent and live ownership for every selection after both pickers before
any mutation, then again on each source before stopping it. Failed scans or transfer steps fail closed. `session move --from A
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
A plain explicit slot (`t session resume api 2`, including `t resume api 2`
and the inferred-repository `t resume 2`) opens the internal saved-conversation picker.
It uses the shared paginated multi-picker over retained conversation identities,
including all recorded slot associations. Even one result requires selection;
cancellation returns 130 without launching. Empty history never creates a session.
Without a terminal it lists history without launching. Tab marks conversations on the current page; Enter accepts marks or focus.
Search, page changes, and refresh clear marks. Extra selections open in tabs before
the first attaches. A resume failure exits nonzero without reopening the picker;
batches stop and report any conversations already resumed.
Fresh saved launches wait up to 15 seconds for the exact conversation identity
before attaching or reporting readiness. This covers Codex registering a resumed
conversation in a new worktree. A changed or exited owner still fails immediately;
an identity timeout keeps the launched session and prints `t open api 2` with its
actual destination for inspection.
The filtered resume batch also stops at the first failed or cancelled selection.
Its final summary names the conversation, reason, retry command, ready-session
attach commands, and number not attempted. No automatic attachment hides errors.
Retained local transcripts use the same guarded destination allocator as remote
history. Historical slot numbers are provenance, not reserved destinations:
multiple conversations from one slot get independent safe destinations, and
copies attributed to different slots launch only once. Allocation checks both
local workspace paths and tmux names under every alias for the repository, then
rechecks ownership before restoring a transcript. An unrelated remote slot with
the same number or path does not occupy a local destination; conversation owners
are still checked across hosts. For example, mark two saved conversations from
slot 21 in `t session resume api`; both can resume without replacing a live owner.
Repositories without a GitHub origin retain their exact local registration
identity through selection and launch; an unregistered or changed path is refused.
Paginated history has no default 30-day
cutoff; Ctrl-F/B pages, Ctrl-R refreshes, and Ctrl-/ shows details.
Explicit resume options (`--cli`, `--fg`, `--host`, `--days`, `--live`,
`--remote`) retain the filtered resume picker and their existing semantics,
including the recency limit and nonterminal saved selection. Internal resume
callers retain their existing behavior.
Progress spinner and canceled picker children never run the caller's shell
session/history exit hooks; the caller's own hooks remain unchanged.

`remote <family> <action> [host] [arguments]` mirrors public command families.
The optional host follows the complete action (including nested resources) and
identifies the source/execution machine. `remote session ls` queries all registered
remote hosts; `remote session ls mini` queries only mini. Prefix `remote --host HOST`
disambiguates a host from a repository. Never consume child option values or
payloads as command words. Aliases normalize only at command boundaries.

`remote open/resume/move [host] [repo] [slot]` use the same source-host position.
`remote move mini api 2` selects mini as source, then asks for a destination;
`--to local` receives here. Keep repo-first move/kill owner discovery and the
released move `--from SOURCE --host DEST` spelling as compatibility input.
`remote kill [host] [repo] [slot|all] [-y|--yes]` retains close confirmation and
saved-work preservation. A lone slot still infers the repository. A chosen close
host is final even if its owner disappears; never fall back to another host.

Remote policy is explicit per command and mode: finite query, single-host action,
session orchestration, or unsupported. JSON alone never makes an action read-only.
Finite queries deduplicate SSH targets, retain stable host order and partial results,
and return nonzero for any failure. Single-host output stays unchanged; multi-host
JSON is one object with host-keyed `results` and `errors` (status and message).
Lists/status never prompt or check for updates. Queries do not recursively fan out.
Interactive actions select one host through shared UI; nonterminal actions require
an explicit or verified target. Lifecycle workers retain cross-host ownership checks.

Only commands that normally infer a current repository carry that scope. Map its
verified identity on the source; fail on an unavailable mapping rather than using
an unrelated alias. Never rewrite payload arguments that merely equal a repo name.
Generic workers validate public command paths and protocol before acting, use
quoted `zsh -lic` execution, and preserve return status. Basic unscoped finite lists
have a read-only compatibility path for older peers; other modes require an update.
Caller-shell-only operations, browser launches, private workers, and nested remote
wrappers are rejected before connecting. Underlying command help remains local.

Remote open reads and scopes inventory on the selected host, then uses the shared
local picker and opens extra local tabs before the first attachment. Failed tab
creation reports an exact attach command and a nonzero result. Desktop rows are
excluded from this CLI-only remote picker.

`remote open` selects current sessions on the chosen host and never starts fresh
from an empty picker; `--new` is explicit. `remote resume` selects saved history
on the chosen host using the existing filtered resume picker. `session resume
--host H` sends the selected saved history to H. Live rows attach only
when their owner matches H (including host aliases); a mismatch gives an exact
`session move` instruction before any batch mutation.

Existing `t/12` folders and recorded conversation paths remain valid. Both
layouts appear in list and navigation results. Ambiguous simultaneous layouts
must fail with an inspection/history hint before a slot mutation. Transfers
negotiate layout support before stopping the source; see the compatibility rules
in the main README. These are path changes within existing command modes.

The inventory's `display_schemas` are machine-readable definitions for the
information shared by `t session list` (`t ls`) and the saved-conversation picker behind `t session resume`. Each command binds its `display_schema`; history extends
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

Bare `t session list` (`t ls` / `t list`) includes every repository, even inside
another repository's checkout or worktree, matching bare `t session open`.
An explicit repository scopes the list: `t session list api`; `--all` explicitly
selects the default all-repository view and conflicts with an explicit repository.
Attached, detached and idle sessions remain visible. `-r` adds registered remote
hosts without changing repository scope; unavailable-host warnings preserve
reachable results. For example, run `t ls -r` for all repositories across hosts,
or `t ls api -r` for only api.

`t session list` additionally places LAST TURN in the final column, after WORKING ON.
It shows the latest turn initiation as `YYYY-MM-DD HH:MM:SS TZ` in the viewer's
local timezone, including remote rows; unavailable evidence displays `-`.
Codex uses `task_started` (older transcripts may use `user_message`); Claude
uses an actual user prompt, excluding tool results and injected metadata.
Assistant output, transcript mtime, and index activity never substitute for a
turn start. The optional ninth rich-row field, `last_turn_started_at`, carries
an ISO 8601 UTC timestamp or `-`; legacy rows and the seven-field ownership
protocol remain valid. The table remains read-only. History's DATE
continues to describe retained activity.

`t session list` overrides the shared table's narrow layout: first clip WORKING ON
to the available space, then omit UI and MODEL if the other columns cannot fit.
Compact rows show S (the same status marks without spaces), HOST for remote lists,
SESSION (SLOT at the smallest width), TASK, and LAST TURN. Shorten timestamps to
local `MM-DD HH:MM`, or `HH:MM` when necessary. Count Unicode display columns and
mark clipped values with an ellipsis. Without a usable terminal or fzf, lists stay read-only.
Redirected output retains all columns and complete values: `t ls | cat` or
`t ls -r | cat`. For example, at 32 columns a remote row can look like:

```text
  S   HOST SLOT   TASK LAST TURN
  ●✓↻ mini api-17 Fix… 09:41
```

`t session list` (`t ls`, `t list`) always prints the scoped, read-only session table.
It never opens a picker or offers an automatic update prompt, even with a usable
terminal and fzf available. Remote hosts are included by default; `--local`
restricts discovery. Empty results print the existing empty state. Use
`t session open` for interactive selection.

The filtered multi-selection mode of `t session open` prioritizes session identity
and working-on text. It omits LAST TURN, previews, and the new-session shortcut.
Compact rows use the shared Unicode width helpers; full task text and metadata
remain searchable. Hidden host/session/state identity fields remain unchanged.
Extra tabs target the originating terminal window and commands target only the
new tab. Switching tabs or windows never redirects commands into an existing tab.
Terminal tab creation requires the originating window in front. If focus moves
away, the origin is unavailable, or tab creation fails or is ambiguous, create a
new window with its command atomically, without an activate call. If window
creation also fails, retain the attach-command fallback and expose the automation
error. iTerm uses the tab returned by its creation API.
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
paginated multi-picker: Tab marks exact conversations, Enter accepts marks
or the focused row, and Esc/Ctrl-C cancels. Marks clear on search, page change,
or refresh. Slot and revision choices remain single-selection.
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

Fresh local slots tolerate unavailable remote hosts while checking known owners,
including legacy reservations reported by older peers. Saved CLI resumes warn
and continue checking local and reachable owners.

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
Successful setup (including no-op and dry-run) prints CLI `/hooks` guidance
when t's Codex hook is registered.
This is informational output within the existing wizard/detail modes: no new
picker, app launch, or hook trust mutation. Installation repeats the guidance
for existing registrations; `t system diagnose` stays read-only.
The agent integration helper is `t-session-start`; `claude-stamp-tmux` remains
an equivalent compatibility entry point. Installation migrates standard legacy
hook commands while preserving custom commands and options. This is an external
SessionStart hook, not a new public `t` command or interactive mode.

## Changes since the v0.4.2 visual baseline

Review these behavior changes when applying the v0.4.2 standard. Later changes
should be added here with their disposition before becoming a precedent.

| Change | Disposition |
| --- | --- |
| Shared Air design ([#219](https://github.com/agenthangar/t/issues/219)) | Apply cyan sections, muted hints, explicit focus, separate green check gutters and platform key labels across all fzf profiles and inline flows. |
| Batch `open`, `kill`, and issue starts ([#68](https://github.com/agenthangar/t/pull/68), [#93](https://github.com/agenthangar/t/pull/93)) | Retain multi-selection and one-live-owner checks. |
| Richer rows, interface/model columns, and detail previews ([#85](https://github.com/agenthangar/t/pull/85)) | Retain within the baseline spacing and focus design. |
| Reversible filters and dedicated keys ([#94](https://github.com/agenthangar/t/pull/94), [#102](https://github.com/agenthangar/t/pull/102)) | Adopt the filter controls above. |
| Login terminal I/O and expiry ([#119](https://github.com/agenthangar/t/issues/119)) | Preserve one vendor terminal and restore it on exit; local sign-in links have a short plain-URL fallback. Status shows cached deadlines when available and keeps authenticated state distinct from expiry availability. |
| Login selection and first-Enter execution ([#83](https://github.com/agenthangar/t/pull/83), [#105](https://github.com/agenthangar/t/pull/105)) | Adopt first-Enter execution; reuse the merged implementation. |
| Reviewed Enter-to-run install ([#98](https://github.com/agenthangar/t/pull/98)) | Retain review followed by Enter to run. |
| Read-only issue table ([#108](https://github.com/agenthangar/t/pull/108)) | Apply the list contract. |
| Open GitHub issues in the browser ([#118](https://github.com/agenthangar/t/issues/118)) | Explicit IDs or URLs act directly; omitted IDs use the shared filtered multi picker. |
| Noun/verb help, added command groups, and speed submenu ([#101](https://github.com/agenthangar/t/pull/101)) | Retain and classify each mode in the inventory. |
| Paginated history selection ([#113](https://github.com/agenthangar/t/pull/113)) | Treat as multi-selection with Tab marks, Ctrl-F next, Ctrl-B previous, and Ctrl-R refresh; do not apply the filtered picker's Ctrl-F defaults binding. |
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

The same read-only detail mode includes launch resources: PTY counts/limits,
aggregate user process count, limits of the diagnostic process (not assumed to
be the tmux server's), and bounded recent macOS kernel PTY failure counts.
Show unavailable evidence explicitly; never print raw process arguments or kernel
logs. Explain that a normal snapshot cannot rule out a transient earlier error.
No diagnostic allocates test PTYs, retries an agent launch, or restarts tmux.
Fresh/resumed launches stop their verified placeholder and wait for both process
exit and a dead pane before respawning without force, retaining first-output logs.

The same detail mode reports five recent lifecycle attempts by default;
`--recent N` accepts 1–20. Include recorded failures before pane creation, stages,
operation exit codes, timestamps and bounded diagnostic log excerpts. A completed
launch does not imply the agent later exited successfully. Incomplete receipts
and log-only sessions have unknown outcomes. Use per-attempt log boundaries and
saved excerpts for older attempts; label missing/rotated/unbounded evidence.
The newest legacy session may show a shared log tail, explicitly uncorrelated
with that attempt when no boundary was recorded.
Read at most the newest 100 candidate files per directory from a capped scan,
128 KiB per receipt, and 16 KiB/eight diagnostic lines per displayed log. Report
scan limits and unreadable/malformed records. Strip terminal controls and common
credentials. Do not read transcripts or create state during diagnosis. Shell
effects preserve bounded failure stderr in private lifecycle receipts; interactive
attachment retains its terminal. This extends the existing read-only detail mode.

## Progress enforcement

Run `python3 scripts/lint_progress.py` alongside CLI lint. CI requires every
Python subprocess, network request, and sleep site to use `_spinner` or have a
reviewed reason in `ui/progress-contract.json`. Interactive tools own their
terminal, streamed installers provide their own output, and background/protocol
workers remain quiet. The inventory also protects shared shell scanner and
resume progress seams. Add coverage when introducing another slow shell phase.

For example, `t issue list`, `t session resume`, and `t system update --check`
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


Bare `t workflow` in a terminal opens the shared RailUI workflow editor. Outside
a terminal, or with `--help`, it prints group help without writes. `t workflow
list` stays read-only and never opens the editor. The editor lists named workflows
and repository assignments, stages all edits, and reviews before saving. Its
PRs, previews & tests menu offers concise choices for pull requests, local
preview and tests, with Frontend and CLI settings nested under Local preview.
Text instructions have higher precedence than those
choices. List output presents that precedence before choices and humanizes model
reasoning; the named catalogue avoids repeating complete per-workflow blocks.
The hidden `t config workflows` compatibility route opens the same editor.

In the Use multiple models checklist, Enter and q/Esc Back retain the current
marks as staged policy changes, including the exclusive one-model reset. Back
does not publish; normal review/save/discard applies to the complete transaction.

`t workflow` includes a Codex **Inference & routing** submenu with Disabled,
Manual, Automatic hybrid and Local only modes, explicit local model, context and
task budget. **On cloud connection failure** stages Pause and retry (default), Ask which local
model, or Continue locally with selected model. Only new native CLI launches opt into the
owned runtime supervisor; the native client supplies progress and approvals.
The transition displays the connection-failure reason, exact provider/model and
private checkpoint path, then stays local. It never downloads weights or treats
authentication, quota, HTTP application failures, auxiliary threads or cancellation
as an outage. Unsupported resume/fork/exec/review or remote/profile/OSS/worktree
overrides fail before launch and explain how to select Pause. These are conditional
launch modes, not new public command families. These are staged workflow choices. The shared model picker keeps
Memory fit ✓/✕/? visible on narrow screens. Unknown/non-fitting models are dimmed
and inspectable; disabled Use/Install actions explain why and never execute.
Benchmark suite selection is explicit; missing scores and speed are not invented.
Every model list follows the model comparison summary contract below; do not hide
all benchmark context in model details.
Installation and measurement are explicit immediate actions with model selection;
model download review precedes installation. `t config models show` is read-only;
`install` and `measure` are direct explicit-artifact actions. `--download-only`
does not claim inference readiness. No list/show mode starts a runtime or picker.

## Standalone companion: nosleep

The package installs `nosleep` alongside `t`; it is not a `t` subcommand.
`standalone_commands` in the inventory classifies its monitoring, help,
configuration and hotspot setup modes. The standalone executable loads only its
own runtime, without dotfiles, user startup files or session registration.
The optional zsh adapter shares that runtime and preserves shell-local activity
thresholds and application mappings. Loading either adapter never starts a hold.
The adapter selects its source on each invocation, including the first call in
`cd worktree && nosleep`, matching `t` without waiting for a prompt refresh.
It reloads the runtime on each invocation so uncommitted edits are available.
Standalone and shell entry points retain normal machine configuration across
worktrees; manual previews do not override `XDG_CONFIG_HOME`.

The Battery protection settings section stages an enable switch and **Exit below**
integer percentage (1–100). Defaults are off and 20%. It is independent of activity
and internet checks, with the same review/save/cancel transaction. Read-only
defaults show the policy without probing power state. Runtime status distinguishes
battery, AC, no battery and unknown readings. A valid below-cutoff reading on
battery exits via normal cleanup even with checks disabled or the lid open;
exact-cutoff, AC and unknown readings continue. Startup checks happen before a
sleep hold is taken; subsequent checks run approximately every 10 seconds.

`t config → Nosleep` opens the same `_RailUI` editor as `nosleep config`,
returning to the hub afterward. Save or discard hub edits before opening the
independently saved editor. Blank, nonselectable rail rows separate its sections.
Nearby-network selection includes refresh, the saved network and manual entry.
Explicit **Enable network discovery** builds the local CoreWLAN/CoreLocation app
and requests Location permission; browsing and monitoring never request it.
Password setup stays pending until review, then uses a private Keychain prompt.
`nosleep hotspot setup` remains a compatibility route.

The runtime observes the selected SSID asynchronously even when fallback or
checks are disabled. The dashboard separates fallback policy from association and
availability. Missing permission, radio off, timeouts and stale observations never
claim a network is absent. Observations do not alter the existing outage policy.
`nosleep --help` and `nosleep config --show` never prompt, change power settings,
join networks or write configuration. Existing flags, machine-local config paths
and Keychain identifiers remain compatible. Installation does not grant sudo or
start monitoring. Runtime monitoring and hotspot joins require macOS; help and
configuration remain usable elsewhere. Native macOS assertion tests are opt-in.

Session lifecycle actions share native ownership checks and verified launch readiness.
Moves reserve their destination and validate both stores before stopping the source,
then checkpoint work, verify copied bytes and wait for the exact destination owner.
Reused workspaces, saved resumes and transfers retain unidentified-writer refusals.
When allocation proves a newly created worktree and its device/inode still match,
unresolved sibling locks are recorded in the operation receipt rather than
blocking every workspace. Known same-workspace
writers, tmux/foreground owners, reservations and unsafe ownership stores still
block a new launch. No existing writer is stopped or released by this check.
Failed transfers retain receipts and use `t resume REPO SLOT` for reconciliation;
they never restart an ambiguous source.
`session history`, `remote session history` and `history` remain hidden compatibility
routes for the internal saved-conversation implementation, excluded from menus and
completion. Public instructions use `session resume`.

### Local inference activity

In an owned tmux pane, local lead and worker launches add a compact
`local MODEL · inferring` status segment only during observed model requests.
Loading, work between requests, idle and failure are separate states. Pane ancestry
and pane generation prevent activity from another session being shown. Missing
ownership hides the indicator; records expire after twelve seconds and the bar
refreshes at least every two seconds.
No prompts or credentials are stored. Outside tmux inference is unchanged.

The session name and worktree sit at the left; existing custom tmux content is
retained. Codex's native model, effort and context footer remains unchanged.
Long labels are bounded, and the full artifact remains in local-worker details.
This adds no public command and does not select or install a model.

Development preview: `python3 scripts/preview_local_status.py` from this
worktree in a terminal. It uses an isolated tmux server, sample activity and
temporary HOME/state; it never calls a model or edits real configuration.

### Model comparison summaries

Any page showing a list of models includes succinct, high-level detail and
benchmarks for each model. This applies to native/cloud agent selectors, workflow
stage selectors, local catalogues, setup and usage/monitoring lists. Preserve
existing fields, actions and navigation. Use the shared RailUI/table patterns.

Keep each entry compact: model identity, a short evidence-backed description or
capability summary, relevant availability, and a named benchmark with its score.
Use a compact table like `t ls`: one physical row per model and the same column
positions for every row. Include model identity, size, provider, memory fit,
readiness and benchmark score where available. Put capability/precision in the
model cell or a consistent column; do not add indented metadata lines per model.
Use a nonselectable column header and one shared legend: selected marker, memory
fit, verification state and benchmark provenance. Align headers, model labels,
legend and actions to the same text origin after the focus gutter. Reserve
cyan for headings, focus and meaningful values rather than coloring all metadata.
Show an unavailable score as an em dash with one shared explanation; avoid
repeating a full Benchmark unavailable paragraph under every model. Inheritance
is one compact control with the resolved default when known, not a duplicate
model card. Separate model choices from management actions with shared sections.
Keep sample-data labels and prototype explanations outside the simulated terminal.
For local models retain precision, memory fit and readiness; include size/context
when available. Show measured speed only when observed, with units. Use explicit
Benchmark unavailable and Speed not measured states rather than zero or invented
scores, descriptions or performance claims. An unavailable catalogue must retain
saved/custom model choices and explain which metadata is missing.

Keep suite/version and evaluation provenance visible in the list, using a shared
heading or legend when identical across rows. Label publisher/base-model results
separately from evaluations of the exact local artifact. Different suites,
harnesses, quantizations and hardware are not a common ranking; mark differences
and do not silently sort them as comparable. Details expose sources, dates,
methodology and measurement conditions without making users open each model to
see its basic comparison. Opening a list never triggers inference or measurement.

At narrow widths, keep rows on one line as `t ls` does: shorten model text and
column labels, then omit lower-priority size/provider columns consistently.
Keep benchmark context, memory fit and availability legible at narrow widths;
full values remain available on selection. Never vary columns between model rows;
use the shared viewport to retain focused selection and navigation. Planning
previews cover cloud and local lists, missing scores, unavailable metadata,
different evaluation methods and narrow terminal sizes with labeled sample data.

### Workflow model overview

The existing local-model single-selection mode opens a compact RailUI overview
with model identity, readiness, file size, memory fit, context and the next action.
Use shared nonselectable context rows and the shared viewport; keep the focused
action and footer visible at 80×24, 44×20 and 32×12. Include the same concise
benchmark summary shown in model lists. Benchmarks and Artifact & runtime provide
separate read-only detail views; sources/methodology remains accessible.
Wrap detail text within the rail, including URLs and revisions. Back retains focus.

Downloaded models offer Verify readiness with a review of file reuse and context.
The explicit action reuses complete files, loads the exact artifact, checks its
context and response text, releases owned resources and refreshes the overview.
Use in workflow requires readiness and remains staged; verification is immediate.
A failed check retains files and shows the runtime error; cancellation invalidates
old verification evidence. Special memory-mapped artifacts may be checked when
the publisher's resident estimate and minimum RAM fit; fit remains unknown until
verified. Runtime guardrails are never bypassed. These extend existing wizard,
single-selection, detail and install action modes; no new public command.

### Prompted connection recovery

The existing native Codex inference launch has an `on_cloud_failure=ask`
conditional mode: RailUI single selection for local recovery, passthrough for
native work, and single selection/review for explicit cloud return. Selecting a
ready local model is the continuation action and dismisses the picker directly;
never insert a model-details or confirmation screen. Show provider, readiness
and Memory fit ✓/✕/?; disabled choices explain why they cannot run. Pause/Esc
retains the cloud runtime and suppresses repeat offers for the current turn.
A failed local start keeps recovery choices visible with the error and retained
checkpoint. Choices are session-scoped and never save workflow changes.

Cloud reachability must be stable before the return offer, which appears only at
a safe turn boundary. Never interrupt a tool or pending approval. Stay local/Esc
dismisses that offer; cloud return uses a review and updated bounded checkpoint.
Capture and restore only the verified owning tmux pane without nesting alternate
screen buffers. Outside tmux, report that prompted recovery is unavailable and
retain the native cloud session. Preserve exactly one writer through every exit.
Nonterminal Ask launches preserve native cloud behavior without a picker.

In Ask mode, correlated fresh-start `account/read`/`thread/start` transport errors
and the native routing-discovery bootstrap failure use the same ready-model
selection after verified runtime shutdown. Show Retry cloud and Cancel startup
instead of offering to pause a conversation that does not exist. Selection starts
locally immediately with the original text prompt or an empty conversation.
Incomplete startup input disables local selection. Preserve Pause passthrough;
routing-discovery failure alone never authorizes automatic local fallback.

Development preview: `python3 scripts/preview_connection_recovery.py` uses the
shared RailUI with labeled sample data, no services, and no real configuration.
Add `--startup` to simulate a bootstrap failure and cloud retry.

### Scoped workflow context and job watching

`t workflow context [--tool TOOL] [--repo ALIAS] [--json]` is read-only. It
resolves the current checkout through its common Git directory, including linked
worktrees; `--repo` selects a registered checkout explicitly. It prints shared
rules and one workflow. Global agent files contain a short loader; supported
SessionStart hooks supply the resolved context at start, resume and compaction.
An identity probe failure is an error, not permission to choose another scope.

Installation shows the affected global and repository instruction paths and backup
location, then asks **Back up and rebuild these global and repository instructions?
[y/N]**. Declining, EOF or unattended installation preserves existing instructions.
Only t-owned marked blocks are rebuilt; custom Markdown and hand-written content
remain. Original contents, permissions and hashes are backed up under the user's
state directory. Unchanged files are not backed up or prompted for again.

New Codex presets use Sol / medium for main work and Astra / medium for bounded
review. Model stages and workflow launch defaults have independent inherited,
Standard, Fast and Ultrafast speed choices where advertised by the native tool.
Unavailable saved choices remain saved, with an explicit native-speed fallback.

Workflow Behavior includes **Job watching ›** (Automatically / Only when
requested), independent of test timing and location. **Text instructions ›**
contains Edit, Clear and Back; Clear stages removal and exposes Undo clear.
Normal save/discard semantics apply. Test runners default to a local snapshot,
even with remote targets configured. Empty remote defaults mean local; explicit
remote failures never silently fall back. Native GUI tests remain opt-in.

`t runners runs watch ID --timeout 60 --json` returns a terminal receipt or a
pending wait result. `ID` can also be `OWNER/REPO#PR@HEAD_SHA` to watch GitHub
checks pinned to that exact head. `--next-step TEXT` records the authorized step
waiting on the results. Watch timeouts and interrupted observation do not cancel
jobs. The local-worker MCP offers the equivalent connection-owned `wait_task`.
A detached watcher does not wake an agent: use a bounded wait or a host-supported
notification, then inspect evidence and continue the next authorized step.

`t ls` shows STATUS, UI, MODEL, optional HOST, SESSION, WORKING ON, **WATCHER**,
and LAST TURN. WATCHER is `active watch-ID`, `stale watch-ID`, or `—`, with `+N`
for concurrent watches; full IDs and run references remain in picker details and
structured session metadata. Narrow output preserves session identity, task,
watcher and last turn. Listing reads cached activity only. Statusline fragments
preserve model/token/custom content. Only verified conversation owners receive
activity; completion never implies that an agent reviewed the result.
