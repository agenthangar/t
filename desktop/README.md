# t Workspace

A macOS desktop workspace for the same sessions, ownership checks, repositories,
configuration, and worktrees as the `t` CLI. This application is distinct from
`t app`, which opens the external agent application.

Production starts with real shared-core discovery. It does not seed example
repositories or sessions. All fixture transports live in `tests/` and run only
under headless Playwright; no fixture switch is shipped in the application.

## Develop and verify

Requires Node 24+, Rust stable, Xcode command-line tools, and Python 3.12+ for
build scripts. Git, zsh, tmux, SSH, GitHub CLI, and agent CLIs remain external
runtime dependencies; Settings → Runtime & dependencies reports availability.
Python itself is bundled for application users.

```sh
cd desktop
npm ci
python3 scripts/bundle-runtime.py
npm test
npm run build
cargo test --locked --manifest-path src-tauri/Cargo.toml
npx playwright install chromium
npm run test:ui
npm run tauri -- build --bundles app
python3 scripts/smoke-bundle.py 'src-tauri/target/release/bundle/macos/t Workspace.app'
```

`npm run tauri -- dev` opens a native window and must only be used when desktop
verification is explicitly requested. Routine checks above are headless and do
not open or focus the app. In development, the helper comes from the checkout
and `T_DESKTOP_PYTHON` may select its interpreter. Release builds always use the
bundled interpreter and absolute application resource paths.

`npm run format` formats TypeScript, React, styles, configuration, and Rust.
Both npm and Cargo lockfiles are maintained. CI builds an unsigned macOS `.app`,
runs transport/PTY and browser tests, and tests a relocated application bundle.
Signing and notarization are distribution steps, not required for local builds.

## Bundled runtime

The build pins Astral's redistributable CPython **3.13.16**, release **20261009**,
with separate SHA-256 digests for arm64 and Intel macOS. Downloads come only from
[python-build-standalone releases](https://github.com/astral-sh/python-build-standalone/releases/tag/20261009).
Checksum failure stops packaging. The complete relocatable installation,
standard library, dynamic libraries, license notices, curated `t` runtime,
shared-core bridge, zsh plugin, and UI resources ship inside `Contents/Resources/runtime`.
Executable modes and relative Python symlinks are preserved. No Homebrew Python,
user credentials, personal configuration, or source-checkout symlink is bundled.

The runtime manifest records desktop version, Python release/digest, and source
commit. The bundled core identifies itself as the desktop version; rebuild
resources after the final commit so the manifest identifies the delivered tree.
CPython's license is at `runtime/python/lib/python3.13/LICENSE.txt`; bundled Python
packages retain their notices. The `t` license is at `runtime/core/LICENSE`.

The relocation smoke copies the `.app` to a temporary location, rejects external
symlinks, starts its helper with fresh HOME/XDG directories and Finder-like PATH,
reads real empty configuration/session state, runs diagnostics, and executes a
normal bundled `t --version` shell operation. On macOS, test-only empty
`ps`/`pgrep`/`lsof` adapters and a synthetic startup file re-pin those probes after
login `path_helper`, keeping unrelated developer foreground processes out of the
smoke. The test asserts that the process probe resolves to its isolated adapter.
These fixtures are temporary and never ship in the app; tmux and shared-core
imports remain real, with an isolated socket and empty configuration. It never launches the UI or uses
the developer's tmux socket.

## Interaction and recovery

Select a session for details; double-click or Attach to open its exact tmux
attachment. The terminal uses xterm.js and a Rust-owned PTY, supports paste,
UTF-8, resize, keyboard input, and bounded output buffering. Tabs retain their
PTY while switching. Closing a tab detaches its client, preserving the session
and agent. An ended tab exposes Reconnect when it references a session.

New session, resume, restart, close, issue selection, repository registration,
host changes, and shared default settings use typed shared-core requests and
explicit review before apply. Resume can name an exact saved conversation ID;
an empty ID retains the CLI's newest-conversation behavior. SSH hosts use the
same installed remote core and login-shell contract. Local discovery appears
before SSH scanning finishes; incomplete hosts are reported.

⌘K opens a capability-driven command palette. Arrow keys and Enter select a
command. Inherently interactive and less common CLI capabilities open reviewed
terminal actions. Arguments use ordinary quoted words, never shell evaluation
in the renderer. Agent model/effort, profiles, policy, authentication, trust,
installation, external Codex/Cursor/AgentCore, and system maintenance remain
available through this catalog. The workspace parity matrix and exact scope are
in [the desktop workspace documentation](../docs/desktop-workspace.md).

Preparations may be cancelled before execution. Cancelling a form does not send
Ctrl-C to an agent. Ctrl-C is terminal input only. Once an operation has started,
the UI reports its progress and durable outcome rather than automatically
replaying it. A footer recovery action inspects pending operation IDs after
helper loss/restart. Reconciliation never executes a mutation. Interactive
command tabs are labelled as interruptible commands; only tmux attachments
promise that closing them preserves their running session.

## Security boundaries

The renderer has no arbitrary filesystem or shell API. Rust admits a fixed
private method allowlist, checks the version-1 handshake, routes concurrent
request IDs and progress events, and rejects helper death/timeouts without
replay. Backend terminal descriptors become opaque, one-use attachment tokens
before entering the renderer, including descriptors nested in multi-issue
results. A terminal mutation token must atomically claim its durable operation
before spawn; completed/partial/unknown terminal records cannot mint replay
tokens. Completed typed operations may return attach-only tmux descriptors.

PTY output is byte-safe and only advances after xterm acknowledges the prior
chunk. A disconnected or stalled renderer detaches the PTY client. A blocked
paste holds only its terminal's writer lock; other tabs can acknowledge, resize,
and detach. Local assets use a restrictive CSP; transcripts, terminal bytes,
and arbitrary result text are rendered as text. No renderer-provided executable,
argv, environment, remote URL, or Markdown HTML reaches a shell.
