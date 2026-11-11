# Session discovery cache

Session lists, open/resume pickers, and sessions MCP discovery share a local
service. Every cached response is served from decoded objects in RAM. Callers
use the single `request(operation, payload)` entry point in
`libexec/t_session_cache.py`, through a private Unix socket. Adapters only
project those responses into existing CLI and MCP formats.

The service starts on demand, without launchd or shell configuration. It owns
expiry, refresh workers, invalidation, memory, and persistence. Commands do not
query SQLite or retain independent process-local discovery caches. Live
ownership checks before session actions remain uncached.
Fresh allocation does not rebuild display discovery: it asks current peers only
for retained worktree reservation markers, falling back to the existing live
inventory on older hosts with desktop integrations. Local foreground preflight
reads identity and cwd without transcript titles. The selected workspace still
passes live local ownership and native writer checks before launch.

## Disk persistence

The service asynchronously persists transactional restart snapshots to
`$XDG_CACHE_HOME/t/discovery/cache.sqlite3`, defaulting to
`~/.cache/t/discovery/cache.sqlite3`. SQLite's WAL and shared-memory sidecars may
be adjacent. Only the service accesses these files. Directories are private
(0700); the database, sidecars, and socket are private (0600).

Disk is read during service startup/recovery, never to answer a cache lookup.
While restoration is pending, requests return a refreshing status. Persisted
records keep their original timestamps. An abrupt crash can lose updates that
have not yet reached the asynchronous writer; discovery will rebuild them.
Persistence errors are returned in freshness diagnostics while RAM serving
continues. Durable conversation history remains independently retained in
`$XDG_STATE_HOME/t/history/v1/history.sqlite3` and is never deleted on expiry.

Legacy candidate and transcript indexes are imported on first restoration.
Collectors subsequently access index artifacts through the same cache API.
Worktree source and agent-store namespaces are isolated; previewing a worktree
never replaces the installed command's service or snapshot namespace.

## Freshness

Successful local/remote session snapshots expire after one hour. Failed host
attempts are retried on the first discovery request after one hour. Browsing
returns existing records immediately and refreshes expired or missing entries
in the background. A failed refresh retains the last known sessions. Open
pickers keep their current list; the next invocation sees refreshed results.

To wait explicitly, use `t session list --wait` (or `t ls --wait`). It observes
the shared refresh for at most 30 seconds, then prints the existing table and
freshness information. Fresh caches return immediately. Failure or timeout
retains available rows; losing the service while waiting preserves the caller's
last snapshot. Ctrl-C cancels only this command with status 130. It never cancels
shared workers, changes expiry policy, or bypasses live ownership checks.
Ordinary lists and MCP remain immediate. Redirected `--wait` is bounded and
silent until the result, with no spinner or prompt.

```sh
t ls                         # Continue immediately with available sessions
t session list --wait        # Wait for local and registered hosts
t session list api --local --wait
```

A cold cache reports that discovery is refreshing, rather than claiming there
are no sessions. Human output reports cache age and unavailable hosts; MCP and
history JSON include additive `freshness` metadata. Transport failures do not
trigger repeated SSH fallback attempts.

Start, resume, open/attach, close, restart and move invalidate the affected host
through the same API. Verified lifecycle records can update its live rows
immediately. In-flight refreshes from an earlier generation cannot undo that
invalidation. Independently operated remote hosts are observed at the next
eligible refresh; there is no background hourly polling while idle.

## Validation

Tests use synthetic data, private HOME/XDG directories and isolated sockets.
They cover RAM-only reads, SQLite restart recovery, expiry boundaries, failure
cooldown, concurrency, lifecycle invalidation, and collector safety. Native
macOS desktop tests remain opt-in.

An isolated working-command preview is available from this worktree:

```sh
python3 scripts/preview_session_cache.py --scenario warm
python3 scripts/preview_session_cache.py --scenario expired
python3 scripts/preview_session_cache.py --scenario offline
python3 scripts/preview_session_cache.py --scenario cold
python3 scripts/preview_session_cache.py --scenario expired --wait
```

These use the real session-list renderer and private socket service with synthetic
records, temporary HOME/XDG paths, and no SSH or agent activity. The preview's
service exits when the command returns.
