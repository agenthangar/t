# Codex desktop to CLI handoff research

These experiments tested Codex CLI 0.159.0 (bundled with the macOS app) and
0.160.0 (installed separately). Each uses a disposable `HOME`, `CODEX_HOME`,
XDG directories, temporary worktree, and synthetic conversation. They do not
start a model turn or touch a real session. Run them with an installed Codex
binary, for example:

```sh
python3 scripts/poc_desktop_duplicate_owner.py --codex "$(command -v codex)"
python3 scripts/poc_desktop_release.py "$(command -v codex)"
python3 scripts/poc_desktop_control.py --cli "$(command -v codex)"
python3 scripts/poc_desktop_control.py --cli "$(command -v codex)" --remote
python3 scripts/poc_desktop_control.py --cli "$(command -v codex)" --action unsubscribe
T_POC_CODEX_BIN="$(command -v codex)" python3 scripts/poc_desktop_shared_backend.py
python3 scripts/poc_desktop_writer_probe.py --codex "$(command -v codex)"
python3 scripts/poc_desktop_handoff.py --codex "$(command -v codex)"
python3 scripts/poc_desktop_handoff.py --codex "$(command -v codex)" --history-version 0.158.0
python3 scripts/poc_cli_to_desktop.py --codex "$(command -v codex)" --shared-backend
python3 scripts/poc_cli_to_desktop.py --codex "$(command -v codex)" --shared-backend --production-wait
```

The results were consistent across both versions:

| Approach | Observed result |
| --- | --- |
| Two independent stdio app-servers resume one saved thread | The second gets `already has an active writer`. An idle thread still holds the writer lock. |
| `thread/unsubscribe` | Removes only that client's subscription. The thread stays loaded, even after the last subscriber leaves, so a separate backend still cannot resume it immediately. |
| `thread/archive` in the owning backend | Unloads the selected thread immediately. An unrelated loaded thread stays loaded, and the synthetic history survives. |
| `thread/archive` from a different backend | Fails with `already has an active writer`; it cannot force the desktop's backend to release the thread. |
| `thread/unarchive` then `thread/resume` from a second backend | Restores the same ID and history. The old backend is then blocked by the new writer lock. Resuming while still archived fails with an instruction to unarchive first. |
| Plain `codex archive ID` while another stdio backend owns the thread | Exits unsuccessfully and leaves the thread loaded. With an explicit shared Unix app-server, `codex archive ID --remote unix://SOCKET` does unload it. |
| CLI `codex resume --remote unix://SOCKET ID` on a shared server | Renders the saved thread in the terminal while a desktop-like client remains connected to that same backend. Both clients can resume the thread, so sharing one backend alone does not enforce exclusive UI ownership. |
| Standalone `codex resume ID --no-daemon` while another backend owns the thread | Shows an ownership conflict screen without taking the lock. Once the owner exits, pressing `r` resumes the same thread; the CLI PID then holds its writer-lock file. |
| Production handoff through an isolated tmux socket | The real helper blocks while the selected thread is owned, permits transfer after that thread is archived, unarchives the same history, and launches `codex resume ID --no-daemon`. Its live CLI process owns the writer lock, the desktop reservation is released, and a separate backend cannot load the thread. Another thread in the original backend stays loaded throughout. |
| Older saved conversation through the production handoff | A synthetic 0.158.0 rollout retains that version in its first metadata record even after 0.159.0 or 0.160.0 resumes and archives it. The exact SQLite row then marks it archived and points to the archived rollout, with no process holding that file open. The helper safely releases this selected thread while the unrelated desktop thread stays loaded; the later CLI still resumes the same ID and history. |

The lock is `$CODEX_HOME/thread-writer-locks/<thread-id>.lock`, not the rollout
JSONL file. A nonblocking exclusive `flock` fails while the thread is loaded.
Release can delete the lock file entirely; archive did so on both tested
versions. Probes must open only existing files, so checking a released thread
does not accidentally recreate its lock. Unrelated backends do not affect that
lock. The standalone CLI's ownership screen does not have the lock open; after
successful resume, `lsof` identifies the CLI PID on that exact lock file. These
are observed implementation details in the two tested versions, not a public
promise that this lock-file format will remain stable.

The installed desktop app uses a private stdio backend in its normal mode. Its
Archive action routes to `thread/archive`, but we found no supported external
command for one chosen conversation in that private backend. A separate CLI
cannot simply call archive against it. The app's archive path may also queue
cleanup of a Codex-managed worktree; a generic instruction to archive any
desktop chat would therefore be unsafe. The worktree ownership check in `t`
must decide which release instructions are appropriate.

The practical conclusion is to check the exact thread's writer lock and preserve its
reservation until release is verified. A whole-app quit is not intrinsically
required by the app-server protocol: an archive performed by the owning backend
releases just that thread. The current experiments do not establish a safe way
for `t` to trigger that operation in the actual private desktop backend while
keeping the app open. They also do not test an active model turn or the live
desktop UI; the desktop routing claim comes from a read-only audit of the
installed app code. Codex could change these internal behaviors in later builds.

The end-to-end handoff probe uses the production `t` helper and zsh functions,
but constructs its conversations in a temporary Codex home. The synthetic
`thread/inject_items` method does not populate SQLite's first-message search
field, so the probe sets that one synthetic index field before running the
production selection and UI checks. For the older-history variant, it seeds a
synthetic 0.158.0 rollout with a user message and resumes it through the current
app-server before archive. No model inference is requested.

For the reverse CLI-to-desktop direction, a separate disposable probe compared
an inline CLI with an explicit shared Unix app-server on Codex 0.160.1. Killing
an inline CLI released its writer lock immediately, and another backend resumed
the same thread. With the shared server, the server held the lock after the CLI
frontend exited with SIGTERM or a normal Ctrl-D. A separate backend received
`already has an active writer` until the server automatically unloaded the
thread about 60 seconds after frontend exit. A plain `codex archive ID` could
not override that ownership. Calling `thread/archive` on the owning shared
backend released only the selected thread; another loaded thread stayed active,
and an independent backend could unarchive and resume the saved ID. The
explicit Unix server reproduces the ownership lifecycle but is not the app's
private stdio backend or a direct test of Codex's installed daemon launcher.
In the same 0.160.1 shared-backend fixture, production
`preflight_cli_release` established the selected writer-lock contract before
stopping the CLI. Production `wait_cli_released` waited about 60 seconds for that
lock to clear, then an independent backend resumed the same saved ID. The
unrelated control thread remained loaded throughout the wait.

Official reference: [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server)
documents per-connection unsubscribe, its 30-minute unload grace, archive,
unarchive, and loaded-thread queries. [Developer commands](https://learn.chatgpt.com/docs/developer-commands)
documents `codex archive`, `codex unarchive`, and `--remote` for explicit
app-server endpoints. The [desktop command reference](https://learn.chatgpt.com/docs/reference/commands)
lists separate shortcuts for closing a tab and archiving a chat.
