# Desktop workspace and CLI parity

Work tracked in [issue #110](https://github.com/agenthangar/t/issues/110).

`t Workspace` is a Tauri/React desktop shell over the existing Python and zsh
session core. `t app` retains its external agent-app behavior. The desktop does
not maintain a separate owner table, allocate independent worktrees, or require
a daemon. CLI and GUI see the same persisted configuration and tmux ownership.

The main workspace contains repositories and hosts, task-readable session rows,
tabbed embedded terminals, a selectable inspector, an adjustable terminal split,
a keyboard command palette, settings, and dependency diagnostics. Empty/loading,
helper disconnect, stale attachment, ended terminal, unreachable SSH host, and
uncertain operation states have explicit recovery actions. Native UI tests remain
opt-in; headless browser tests use a separate synthetic transport.

## Private contract

Every JSON line is versioned: `{version:1,id,method,params}` and
`{version:1,id,result}` or a structured error. Correlated progress notifications
are separate from final replies. Rust starts the bundled helper, validates its
handshake, routes responses, and replaces terminal plans with opaque tokens.

Typed mutations return a durable prepared operation with `command:null`, method,
arguments, and summary. Review displays those arguments. `operations.apply`
checks its persisted state and runs the existing guarded core adapter once.
Session lifecycle results provide exact attach-only plans; multi-issue results
provide one plan per successful independent issue plus actionable partial errors.

Long-tail terminal actions retain their reviewed command, then atomically claim
`operations.started` before PTY spawn. Rust records completion/interruption even
when the renderer vanishes. `operations.reconcile` inspects outcome only. A lost
helper or timeout never triggers mutation replay. UI recovery stores opaque
operation IDs, not commands, transcripts, credentials, or agent prompts.

Local/SSH attach opens only the named tmux target; closing its PTY client never
kills the tmux session or agent. Remote commands use the existing `zsh -lic`
contract and the remote installed core. Setup-form cancellation is distinct from
Ctrl-C input to an attached agent. System update/integration commands retain the
core's installation safeguards; the desktop does not silently replace its own
running bundle.

## Capability routing

The table is grounded in the shared core's current command catalog. Every listed
verb is reachable from the command palette. Typed primary workflows use dedicated
forms; interactive/less common routes use explicit argument input, review, and
a Rust-owned terminal. Unsupported or failed core routes show errors; the UI does
not invent production state or silently substitute successful fixture results.

| CLI family | Current verbs | Desktop route |
| --- | --- | --- |
| issue | list, status, start | Dedicated forms/read results for list, status, start; optional reviewed interactive route for the same verbs and advanced flags |
| session | open, list, history, close, restart, push, pop, resume, cd, move, read, view-plan, paste, search, open-app | Dedicated forms/read results for open, list, close, restart, resume, search; reviewed terminal for other verbs; cd retains an interactive shell; push/pop detach/attach the selected GUI session |
| repo | list, cd, locate, create, clone | Dedicated forms/read results for list; reviewed terminal for other verbs; register existing checkout in sidebar; cd retains an interactive shell |
| share | `t share --to AGENT:ID --file PATH`, targets, inbox, read | Reviewed terminal actions for send, recipient discovery, inbox, and message/attachment reads; send uses standalone `t share` flags |
| cursor | list, resume, send | Reviewed terminal action |
| hosts | list, show, add, edit, remove, default, run | Dedicated forms/read results for list; reviewed terminal for other verbs; add/remove/default typed host form in sidebar |
| agentcore | invoke, stop | Reviewed terminal action |
| config | open, show, edit, setup | Dedicated forms/read results for show; reviewed terminal for other verbs; typed default-agent/interface/branch/worktree settings |
| profile | init, edit, show, apply | Reviewed terminal action |
| policy | check, show, apply | Reviewed terminal action |
| agent | login, install, status, trust | Reviewed terminal action |
| system | integrate, update, diagnose | Reviewed terminal action |
| Internal MCP | `t mcp --install`, `--list-tools`, `--reindex`, `--call TOOL JSON` | Dedicated shared-core read-tool form for find/list/detail; reviewed `--install` terminal route |

Search uses the shared keyword-scored history index with readable excerpts and
an exact-conversation resume action when repository and numbered slot resolve. Unresolved rows offer the existing shared terminal picker with an explicit conversation-selection step. Users can also use the original
interactive CLI search through a terminal command. `t session history` opens the
existing local and retained slot-history browser as a reviewed terminal action.

Issue list/status read the shared GitHub scanner. List scope follows the CLI:
an explicit repository limits the scan; otherwise a registered current checkout
selects that repository, and a location outside registered checkouts scans all
registered repositories. Advanced `--all` and `--state open|closed|all` flags are
available through the reviewed terminal route. Aggregate typed results retain
available issues and display repository failures; an incomplete empty result
reports that no issues could be verified. Selected independent issues may start
together, while ambiguous existing owners require explicit terminal review.
Core safeguards remain
responsible for the one-live-owner guarantee and worktree safety.

Repository creation/clone, session move and attachments/plans, external Codex app,
Cursor, AgentCore, profile/policy, agent status/login/install/trust, and system
maintenance use their current CLI route and prompts in the embedded terminal.
The frontend catalog does not reproduce those business rules. Context sharing
uses standalone `t share --to AGENT:ID --file PATH` flags (or `--retry MESSAGE_ID`).
`share targets`, `share inbox`, and `share read` retain their
existing recipient selection, host options, receipts, and attachment behavior.

## Build and verification

See [desktop/README.md](../desktop/README.md) for pinned runtime sources,
checksums, executable/resource layout, licenses, development commands, and
headless verification. Default Linux CI runs frontend protocol tests, browser
interactions and screenshots, and the production build. Explicitly dispatching
the Desktop workflow with `run_macos: true` runs Rust bridge/PTY tests, unsigned
macOS packaging, and copied-bundle smoke with isolated HOME/XDG and restricted
Finder PATH.
Signing/notarization and native desktop focus-stealing verification are excluded
from routine local runs. This release targets macOS; other Tauri hosts are not
claimed as tested distribution platforms.
