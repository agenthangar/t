# Session lifecycle validation

Session opening, saved-conversation resume, and cross-host transfers must retain
the selected conversation and worktree. A launch request is not proof that an
agent became usable.

## Ownership policy

Opening, saved-history resume, and handoff checks protect the selected
conversation's writer and reject known active conversations sharing its worktree.
They inspect all held sibling locks, including entries after an unindexed lock.
Readable indexes and validated rollout `session_meta` identify exact conversations
and absolute workspaces. A still-unassociated lock can also be resolved through
its verified holders' open rollout files, reading only the first metadata record.
Any matching real workspace path blocks, including conflicting associations.
Process working directories, executable names, other threads on a shared backend,
and `--no-daemon` do not prove that a held writer belongs elsewhere.

Saved resume refuses unresolved writers with the conversation UUID and verified
holder PIDs when available. Finish and exit the identified CLI, or finish the turn
and close the owning app; allow a shared backend to release its writer, then retry.
Never delete locks, edit the state database, or archive an unidentified thread.
Unreadable indexes, corrupt metadata, and unsafe lock files remain errors.
If SQLite cannot open a read-only WAL index without its sidecars, retry using
`mode=rw` with `PRAGMA query_only=ON`. This lets SQLite initialize its sidecars
under normal locking without changing application records or bypassing ownership.

Fresh and resumed Codex CLI launches use `--no-daemon` for invocation-local
backends. This prevents new sharing; it cannot release an existing shared writer.
A backend may retain the selected writer after its CLI exits; saved resume
refuses until that writer is released.

## Automated acceptance

All executable fixtures use disposable HOME/XDG directories and dedicated tmux
sockets. Agent stores, Git repositories, and conversations are synthetic. Copies
of measured binaries must be launched without inherited `COV_CORE_*` variables.

| Scenario | Required outcome |
| --- | --- |
| Codex/Claude fresh CLI start | Requested agent starts in the selected worktree with the configured defaults |
| Live reattach | Existing owner is reused, with no second launch |
| Saved resume | Same conversation ID and worktree; no fallback to a new conversation |
| Concurrent local tmux requests/aliases | One launch; failed session creation cannot inject into an existing pane |
| Remote unavailable | Unverified revival stops with host-specific guidance; verified local attach still works |
| Explicit `--here --new` | Fresh local conversation remains available |
| Other held locks absent from the index | Resolve exact rollout identity, including verified holders’ open files; unresolved saved resume gives targeted recovery and known worktree conflicts take priority |
| Failed transfer or backend timeout | Worktree, edits, preview server, and reservation remain protected |

Run the required suite with `python3 -m pytest --cov --cov-fail-under=97`, Bash
shellcheck, and `zsh -n` for shell changes. Compile `bin/t` with its output under
`/tmp`, never in the checkout.
