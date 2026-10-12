# Codex background-server compatibility

Investigation for [#74](https://github.com/agenthangar/t/issues/74), October 11, 2026.

## Confirmed cause

A fresh `t open <repo> --new` can launch an older `codex` from `PATH` while an
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
agent in that slot's worktree. For Codex, `_dev_agent_new_cmd` emits `codex`, plus
only configured `DEV_MODEL[codex]`, `DEV_EFFORT[codex]`, and `DEV_FAST[codex]`
defaults. The foreground path does the same. It does not request any of the four
shared flags above. Codex reads its own configuration and version-specific defaults.

In 0.160.0, reasoning-effort and service-tier `-c` overrides (including t's fast-mode
launch) select embedded operation through Codex's `config_exclusion`; they do not
cause this shared-feature mismatch. A model selection alone does not bypass the
compatibility check. These are version-specific implementation details, not an
assumption about every Codex release.

## Recovery

Choose **Run without daemon this time** in t's recovery panel (or Codex's own menu,
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
   client config. The real `t open demo --new` creates a session on that server.
2. Change only the disposable client config to `false`. The server's command-line
   override remains `true`; a second `t open demo --new` shows the mismatch.
3. Run t's guarded session-only recovery. The new standalone client becomes ready;
   the original pane PID and server process remain alive, the shared loaded-thread
   list is unchanged, and the existing RPC connection still answers requests.

This reproduces the flag conflict on one binary version without depending on a
particular update service or installing an older daemon. The local version/default
comparison above independently explains how the same disagreement arises without
explicit overrides. Regression tests cover the public `t open --new` arguments,
managed and unmanaged mismatch menus, quoted/partial screen rejection, verified
ownership and callbacks, and real isolated-tmux recovery.

## Remaining regression contracts

The follow-up report covered both the startup menu and an empty desktop-slot
close refusal. Recovery coverage in #78 does not establish that a
fresh launch avoids the menu. The desired launch contract now has a strict
expected-failure test: `test_public_open_new_reaches_ready_with_existing_server`.
It invokes `t open demo --new` through the real shell and an isolated tmux server,
with a synthetic Codex client modeling the documented feature disagreement. The
compatible control passes; the incompatible case reaches the menu. It also checks
the selected model, working directory, unchanged configuration, and surviving
peer pane and simulated server process. This deterministic test is not a new
measurement of the user's installed Codex versions or effective flags.

The separate empty-slot `t kill` regression is fixed at both the Python close
entry point and the shell's final verifier. Explicit close releases a never-started
slot after verifying its conversation index and writer locks, even while other
Codex/ChatGPT sessions run. It preserves the worktree; the unused blank composer
tab can be closed separately. Saved conversations still require archive or
frontend exit. The tests cover both frontend names, a live writer in another
worktree, and parked tmux sessions. A conversation appearing during cleanup keeps
the reservation intact.

To exercise the close regressions and expose the remaining startup failure
(alongside the passing compatible-launch control):

```sh
python3 -m pytest --runxfail -q \
  tests/test_app_close.py tests/test_desktop_close.py tests/test_recovery_tmux.py \
  -k 'blank_workspace_with_unrelated or empty_desktop_close_with_unrelated or public_open_new'
```

The startup failure remains a strict expected failure in normal runs. Only the
specific known menu is accepted as an expected failure; fixture and safety
assertion failures remain failures. The empty-slot close tests now pass normally.
