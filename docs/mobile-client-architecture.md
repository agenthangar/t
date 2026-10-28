# Native Swift client boundary

Tracked in [issue #110](https://github.com/agenthangar/t/issues/110).

The intended iOS app is a native SwiftUI client of the same host-owned session
core used by the CLI and desktop. Python, zsh, tmux, worktree allocation, and
agent processes run on a laptop or another reachable host. The iOS app renders
state and sends typed, authorized requests; it does not spawn the local helper.

## Available now

The desktop uses a Tauri Rust bridge to start `libexec/t_bridge.py` over newline
JSON. `libexec/t_core.py` provides snapshots, capabilities, prepared operations,
apply/reconciliation, and attachment plans. Rust mediates local PTYs and replaces
raw attachment plans with opaque tokens. CLI and desktop use the same persisted
state and existing POSIX cwd/SID launch locks and owner checks.

[The Swift foundation](../clients/swift/README.md) compiles independently of
Tauri/React and decodes the existing sanitized desktop protocol fixture. It
includes a transport abstraction and executable fixture client. No iOS app,
network control gateway, pairing service, or remote terminal transport exists
yet. A browser preview is a synthetic UI demonstration; viewing it locally or
over Tailscale does not give it access to live sessions.

The private helper wire format is `{version:1,id,method,params}` with a matching
`{version:1,id,result}` or `{version:1,id,error:{code,message,retryable}}` response.
Progress is a separate `{version:1,id,event:"progress",operationId,stage}` frame.
The Swift decoder validates versions and request correlation, accepts null
results and nullable operation commands, and preserves unknown operation states
and provider-specific payloads. This reusable serialization foundation is not a
promise that every private helper method is suitable for remote use.

## Host gateway to implement

The next boundary is an authenticated HTTPS gateway on the session host. Keep
its upstream listener on loopback behind tailnet-only HTTPS serving; do not
enable public Funnel access. Tailnet membership supplies reachability, while
application pairing supplies action authorization. Pair a device using explicit
host approval, issue revocable credentials stored in iOS Keychain, and scope
grants by host/repository/session and read versus control permissions. Validate
permissions for every request and event subscription, including after revocation.

Project a restricted, versioned JSON API from the existing core; do not publish
the stdio helper as an unrestricted HTTP proxy. Use typed allowlisted actions,
validated parameters, bounded requests/responses, redacted diagnostics, and
host-issued opaque context identifiers. Raw argv, env, local filesystem paths,
tmux targets, and client-composed cwd/SID values cannot authorize an action.
Capabilities advertise which typed actions the paired client may use; they must
not offer arbitrary terminal command execution. Endpoint paths remain undecided
and there are no implemented HTTPS endpoints in this change.

Use stable host, session, and conversation identifiers, distinct from display
names and recycled slots. The gateway resolves those identities to current
host-local state and verifies owner revisions before mutations. Preserve the
existing one-live-owner guarantee and lock-protected core behavior. A phone
must never maintain its own owner table or allocate worktrees independently.

Each session observation should carry execution state (`running`, `input`,
`idle`, `stale`, or `unknown`), attachment state separately, observation time,
source host, and a bounded progress summary with its timestamp. An attached
terminal is not evidence that an agent is running; a disconnected client is
not evidence that no agents are active. These normalized fields are future
gateway work: today's helper session rows are heterogeneous and the current
golden snapshot contains no session rows. Preserve unknown states on clients.

Keep durable operation IDs and prepare/review/apply/reconcile semantics. Bind
each reviewed mutation to the paired client's identity, scope, exact arguments,
and current owner revision; consent from another client is not transferable.
Add durable per-client idempotency keys and stored outcomes at the gateway.
On a timeout or reconnect, reconcile the existing operation ID and refresh state;
never automatically replay a mutation whose outcome is uncertain. Existing
local operation files are useful machinery, not a completed remote consent or
idempotency protocol.

## Events and terminal evolution

Use HTTPS request/response for typed reads and mutations and an authenticated
SSE stream for bounded state/progress updates. This is the proposed gateway
design, not an implemented service. Events need monotonically ordered durable
cursors, bounded retention, and an explicit resnapshot response when a cursor
expires. On iOS foreground/reconnect, refresh credentials as needed, reconcile
pending operations, resume from the cursor, and take a fresh snapshot when
required. Treat loss of contact as unknown/stale while retaining the last
observed state and timestamp. Do not promise an always-running background
connection; add push notifications later if the product needs background alerts.

Ship monitoring and selected typed controls first. A later terminal feature can
use a separate authenticated WebSocket channel with short-lived, scoped,
host-issued PTY tokens, bounded output buffering/backpressure, resize/input
limits, and explicit attachment expiry. Closing or losing a terminal connection
detaches the client and preserves the agent/tmux session. Do not send raw local
attachment plans to iOS. The existing Rust PTY implementation remains local and
is not a network terminal server.

## Delivery sequence

1. Keep the Swift fixture proof in use as the local core protocol evolves.
2. Implement and test the restricted host gateway: pairing/revocation, identity
   projection, status timestamps, per-client review/authorization, durable
   idempotency/reconciliation, and cursor/resnapshot behavior. Verify that
   unauthorized or stale-owner requests cannot mutate state.
3. Build a SwiftUI monitor with a URLSession HTTPS/SSE transport and Keychain
   pairing. Exercise reconnects, expired credentials, unknown states, partial
   host failures, and operation uncertainty before adding typed controls.
4. Add optional terminal interaction and then background notifications after
   the monitoring/control boundary is validated.
