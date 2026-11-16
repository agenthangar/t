# Session discovery cache

Session lists, open/resume pickers and sessions MCP read one local cache API:
`request(operation, payload)` in `libexec/t_session_cache.py`. The service owns
RAM snapshots, refresh scheduling, incremental indexes and SQLite persistence.
Command reads never scan transcripts, contact peers, or open the cache database.
Live ownership checks before session actions remain uncached.

## Immediate browsing and recovery

Open and resume show retained results immediately, including while activity is
refreshing or another peer has failed. The picker freezes its snapshot: filtering,
sorting and paging never reorder the selected conversation in response to a
background refresh. Incomplete views retain their warning and require selection.

A cold interactive open/resume uses the shared wait rail. Return skips waiting;
Esc/Ctrl-C cancels observation with status 130 without cancelling shared work.
The first usable result opens selection automatically. A settled failure offers
Retry, Browse retained results when available, or Cancel. Skipping with no usable
results says “No sessions available yet”; incomplete emptiness never launches a
new session. Explicit Retry clears only failed targets' cooldowns.

A missing socket or service loss during waiting starts one elected replacement
candidate per four-second handoff window. The lifetime lock still permits only
one owner. Replacement restores persistence; an old daemon that loses its socket
exits and removes only its own endpoint. Repeated bounded connection failures
return to the recovery rail instead of leaving “retry shortly” as the only action.

Ordinary lists, MCP, JSON and redirected output remain immediate and noninteractive.
`t session list --wait` / `t ls --wait` retain their explicit 30-second observation
limit and never prompt. A failed observer retains its last available snapshot.

Before remote-inclusive open browsing, a known OS network denial or missing
IPv4 and IPv6 routes returns an immediate error. Use `t open --local` to browse
local sessions offline. This caller-local check uses no DNS, sends no packets,
and never resets or marks shared host caches failed. A usable route is not
proof of internet access; ordinary discovery remains responsible for peer failures.

## Retention and observation

Inventory reconciliation has a **24-hour TTL**. Expiry schedules background work;
it never discards a successful snapshot. A failed refresh retains the original
observation times. No TTL is accepted as proof that a conversation has no owner.

A namespace used within the last hour receives background checks every five
seconds locally and thirty seconds per updated remote host. These intervals run
from completion, with one collector per target. Polling stops after an hour with
no requests and resumes on use. Aliases share target failure state and workers.
The daemon starts on demand without launchd, shell hooks or personal configuration.

Attach events schedule a live-status observation without invalidating saved
history. Lifecycle events retain inventory, mark affected hosts dirty and prevent
superseded workers from publishing. Verified close/handoff rows still overlay
live rows immediately; deferred launch publication never resurrects a closed slot.

Local cold starts publish live rows before collecting history. Subsequent history
collection walks each source tree once, keeps an inode/size/mtime manifest and
parses/merges only changed conversations. Unchanged transcripts are not reopened
for repository identification. Codex index metadata is read once per observation
so renames and workspace changes remain visible without transcript writes. Append-only indexes read only new complete lines;
replacement, truncation and parser-version changes retain their existing safeguards.
Removed source files disappear from the browsing catalog without deleting durable
history. MCP metadata and resume projections reuse unchanged conversation records.
Display collection uses cached repository proofs and never waits for GitHub.

## Remote protocol and failures

Updated peers expose a private versioned generation/delta exchange over the existing
`ssh … zsh -lic` contract. The remote daemon serves local metadata from RAM; no
prompt bodies or imported peer evidence are exported. An unchanged generation
returns health metadata only. A changed generation returns changed records and
removals; an unknown generation receives a complete snapshot. The service retains
eight export generations in RAM. Only changed records are imported and projected
into resume candidates. Older peers use the previous collector protocol and are
polled at most hourly while active.

Freshness includes observation time, error category, retry time and stage timings.
Transport/authentication failures retain the one-hour automatic cooldown. Collector
and protocol failures retry after 5, 15, 60, then 300 seconds while active. Success
clears the failure. An online collector exception is reported as “refresh failed”,
not as an unreachable host. `t hosts reset HOST` remains the explicit scoped reset.

Complete collector errors are retained in private bounded logs under
`$XDG_CACHE_HOME/t/discovery/diagnostics/`. Each source/target keeps its most recent
64 KiB; stdout and transcript payloads are never logged. The public error summary
preserves the cause rather than truncating away the end of a traceback. Freshness
provides the diagnostic path; logs must not be published with private context.

## Persistence and development isolation

The service asynchronously persists restart snapshots to
`$XDG_CACHE_HOME/t/discovery/cache.sqlite3`, defaulting to
`~/.cache/t/discovery/cache.sqlite3`. SQLite WAL sidecars may be adjacent. Directories
are private (0700); the database, diagnostic logs and socket are private (0600).
Only the service reads or writes cache storage. Disk restoration is outside the
request loop. Persistence failure does not prevent RAM reads.

Version 2 imports version 1 snapshots without deleting durable conversation
history in `$XDG_STATE_HOME/t/history/v1/history.sqlite3`. Original timestamps and
host cooldowns survive restoration; in-flight markers do not.

Development sources retain their own service and snapshot namespace. A cold Git
worktree may seed from its canonical checkout's compatible persisted namespace,
with matching user/agent-store/environment identity. Repository context remains
part of each entry key. Canonical never imports from a development worktree, and
no client selects another source's executable. Old worktrees must integrate the
fix before they can run its new recovery behavior; installing main does not
silently rewrite another session's branch.

## Validation

Tests use synthetic conversations, isolated HOME/XDG directories and private tmux
sockets. They cover daily reconciliation, active leases, one-record updates,
generation deltas, mixed versions, error categories, restart recovery, socket loss,
concurrent callers, terminal restoration and live ownership safeguards. Native
macOS UI tests remain opt-in. Required Linux design and coverage gates remain intact.
