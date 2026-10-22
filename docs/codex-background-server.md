# Codex background-server compatibility

Investigation for [#74](https://github.com/agenthangar/t/issues/74), October 11, 2026.

## Confirmed cause

Before this fix, a fresh `t open <repo> --new` could launch an older `codex` from `PATH` while an
independently updated shared daemon is already running. Their effective feature
defaults can differ even when neither invocation supplies feature overrides.

The inspected CLI was **0.160.0** and the running daemon executable was **0.162.1**.
The CLI's `codex features list` reported `api_key_model_discovery=false`; a read-only
`experimentalFeature/list` request to the daemon reported `true`. The default
changed from false in [0.160.0's feature definitions](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/features/src/lib.rs)
to true in [0.162.1's feature definitions](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/features/src/lib.rs).
This establishes a current, reproducible cause; it does not establish the versions
or complete settings of the original reported incident.

In [0.160.0's startup check](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/tui/src/daemon_startup.rs),
the client compares these four effective flags with the server's configured
feature readback:

- `api_key_model_discovery`
- `code_mode_host`
- `auth_elicitation`
- `mcp_oauth_refresh_coordination`

The first disagreement produces `This session requires <feature> to be
enabled/disabled`. The [recovery screen](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/tui/src/daemon_recovery.rs)
uses **Background server has incompatible feature settings**, a different heading
from **Cannot use the background server / Experimental feature request failed**.
`t` previously recognized only the latter and the stopped/unreachable-server menus.
The earlier #57 RPC failure and deleted-working-directory failure are separate
failure mechanisms.

## How t selects the settings

The zsh public entry point maps `--new` to a fresh slot and launches the configured
agent in that slot's worktree. For Codex, `_dev_agent_new_cmd` emits `codex`, the
configured `DEV_MODEL[codex]`, `DEV_EFFORT[codex]`, and `DEV_FAST[codex]` defaults,
and `--no-daemon`. The foreground path does the same. Each fresh session and resumed CLI conversation uses a
private backend, so it cannot conflict with the running shared server's settings. It does not request any of the four
shared flags above. Codex reads its own configuration and version-specific defaults.

In 0.160.0, reasoning-effort and service-tier `-c` overrides (including t's fast-mode
launch) select embedded operation through Codex's `config_exclusion`; they do not
cause this shared-feature mismatch. A model selection alone does not bypass the
compatibility check. These are version-specific implementation details, not an
assumption about every Codex release.

## Recovery

For existing sessions that already reached a startup error, choose **Run without daemon this time** in t's recovery panel (or Codex's own menu,
including foreground launches). t saves the pane and relaunches only the verified
client with `--no-daemon`, preserving the fresh-session defaults or exact existing
conversation. It rechecks the process and visible failure before stopping anything.
The shared daemon, its configuration, and unrelated sessions remain running.

Do not choose Codex's **Restart with these settings** merely to open another t
slot: that option changes shared settings and can interrupt other clients. Align
CLI and daemon versions during planned maintenance to avoid repeated mismatches.
`t` does not force shared flags, change the default server policy, or restart the
daemon automatically.

## Disposable reproduction

With Codex 0.160.0, zsh, and tmux installed:

```sh
python3 scripts/poc_codex_compatibility.py --codex /path/to/codex
```

The probe uses a temporary HOME, CODEX_HOME, XDG directories, and a dedicated tmux
socket. It restores its isolated PATH in login shells, including on macOS. No
credentials, real transcripts, or model turns are used; the synthetic model
provider points at an unused loopback port. It starts and cleans up only its own
server and clients. The attachment UI is skipped so the check can run unattended.

1. Start an isolated app-server with `api_key_model_discovery=true` and a matching
   raw Codex peer on that server. The real `t open demo --new` becomes ready using
   a private backend and leaves the shared server's loaded-thread list unchanged.
2. Change only the disposable client config to `false`. The server's command-line
   override remains `true`; a second `t open demo --new` becomes ready without
   presenting the incompatibility menu or requiring recovery.
3. Verify that the peer pane and server process remain alive, the shared
   loaded-thread list and feature setting are unchanged, and the existing RPC
   connection still answers requests.

This tests prevention with a real feature conflict on one binary version without
requiring a particular update service or installing an older daemon. The earlier
version/default comparison independently explains how the same disagreement can
arise without explicit overrides. Existing recovery coverage remains for older
sessions and other shared-server failures.

## Regression coverage and empty desktop slots

The tests added after #78 now pass without expected-failure markers.
`test_public_open_new_reaches_ready_with_existing_server` exercises public shell
launches with real isolated tmux and a synthetic Codex compatibility boundary.
It covers matching/conflicting settings and tmux/foreground launches, checking the
selected model, working directory, unchanged configuration, and surviving peer
and simulated server. The disposable probe above separately checks real Codex.

The separate `t kill` symptom came from treating any running desktop frontend as
an owner of an empty reserved worktree. Empty slots can now release their t
reservation while Codex/ChatGPT stays open. They must have no saved conversation,
no live writer belonging to the worktree, and no foreground CLI. An unknown
writer, unreadable index, changed reservation, or a conversation appearing during
cleanup still blocks release. The shell rechecks these facts before cleanup and
before removing the reservation. The worktree remains on disk; no new agent is
started, no saved thread is reopened, and no desktop window is closed.

Saved, unarchived conversations retain the existing archive/quit requirement.
Tests cover both frontend names, parked and absent tmux sessions, an unrelated
held writer lock, and a conversation appearing during cleanup.

Run the focused regressions with:

```sh
python3 -m pytest -q \
  tests/test_app_close.py tests/test_desktop_close.py tests/test_recovery_tmux.py \
  -k 'blank_workspace_with_unrelated or empty_desktop or public_open_new'
```

The follow-up for [#133](https://github.com/agenthangar/t/issues/133) also applies
`--no-daemon` to ordinary tmux resumes, foreground resumes/adoption, and transfer
landings. Previously those paths could reconnect to the shared server even though
fresh launches and recovery already used a private backend.
