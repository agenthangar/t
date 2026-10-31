# Session lifecycle validation

Session opening, saved-conversation resume, and desktop transfers must retain
the selected conversation and worktree. A launch request is not proof that an
agent became usable, and a successful macOS `open` request is not proof that
Codex displayed the intended thread.

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
Handoff operations using diagnostic collection and close retain their existing
policy for unassociated locks: these alone do not establish worktree ownership.
Unreadable indexes, corrupt metadata, and unsafe lock files remain errors. Known
same-worktree writers block close before and after cleanup. t's operation locks
remain separate from Codex's own writer claim.

Fresh and resumed Codex CLI launches use `--no-daemon` for invocation-local
backends. This prevents new sharing; it cannot release an existing shared writer.
A backend may retain the selected writer after its CLI exits; handoff waits up to
75 seconds, then leaves the worktree reserved and prints a retry command.

The [background-server regression cases](codex-background-server.md) also cover
fresh-start prevention and empty desktop-slot close. Empty reservations can be
released while the app stays open after worktree checks; known same-worktree
writers still block close. Saved, unarchived conversations retain the release requirement.

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
| Fresh `--app` or configured app mode | Reserved worktree, Codex-mode workspace route, no terminal agent |
| App → CLI → app | Same thread and worktree at each transition; correct reservation changes |
| CLI → app → CLI | Original history survives, and only the verified source client is stopped |
| Other held locks absent from the index | Resolve exact rollout identity, including verified holders’ open files; unresolved saved resume gives targeted recovery and known worktree conflicts take priority |
| Failed transfer or backend timeout | Worktree, edits, preview server, and reservation remain protected |
| Close with unassociated writers | Preserve the existing close policy; known same-worktree writers retain the reservation |

The native macOS test uses a disposable receiver app to verify window creation
and exact deep-link delivery. It does not prove Codex's UI behavior. The
[disposable backend probes](desktop-handoff-research.md) exercise installed
Codex binaries without model turns or real conversations.

Run the required suite with `python3 -m pytest --cov --cov-fail-under=97`, Bash
shellcheck, and `zsh -n` for shell changes. Compile `bin/t` with its output under
`/tmp`, never in the checkout.

## Real desktop acceptance

Use a disposable repository registered only in a sandbox t configuration, an
isolated tmux socket, and two conversations created specifically for this check.
Never use an existing work conversation as the test target. Do not copy auth
state or personal transcripts into fixtures or commits.

1. Run `t session open demo --app`. Verify that the new window is in Codex mode
   and names the exact test worktree. Repeat with configured app opening mode.
2. Send a harmless marker message. Record the thread ID, then reopen the slot
   and verify the same message and conversation are displayed.
3. Finish the turn and release that test conversation following t's instructions.
   Run `t session open demo <slot> --cli`; verify the marker history and send a
   second harmless marker from the CLI.
4. From a separate terminal run `t session open demo <slot> --app`. Verify both
   markers in that same desktop thread and confirm the CLI writer has released.
5. Start the second conversation with
   `t session open demo --codex --new --cli`, send its own marker, and repeat
   CLI → app → CLI. Keep an unrelated desktop conversation open throughout.
6. Confirm test edits and any test preview process survived each handoff, then
   close only the disposable sessions through the normal release checks.

Record the t commit, CLI version, desktop version, and result for each direction.
If desktop automation is unavailable, record that gate as unverified; neither a
receiver test nor an accepted OS request substitutes for the visible check.

## Validation recorded on October 11, 2026

The disposable production probes passed with Codex CLI **0.160.0**:

- Desktop-backend → CLI: the selected loaded writer initially blocked transfer;
  archiving released it, production unarchive preserved its history, and the
  exact resumed CLI acquired the writer before its reservation was removed.
  A retry recognized that CLI, another backend could not take its writer, and
  the unrelated control conversation remained loaded.
- CLI → independent backend with an explicit shared server: frontend exit did
  not immediately release the writer. Production preflight and waiting handled
  the roughly 60-second release, then the destination resumed the same ID.
  The control conversation stayed loaded. No model inference was requested.

The live UI gate was not run. The computer-use tool refused access to the
installed Codex desktop app (version **26.930.31730**) for safety reasons.
The native receiver test and backend probes do not establish that this desktop
build visibly opens the intended thread or accepts a message after transfer.
