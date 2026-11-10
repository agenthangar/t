# Shared session lifecycle

Tracking: [#374](https://github.com/agenthangar/t/issues/374),
[#375](https://github.com/agenthangar/t/issues/375), and
[#382](https://github.com/agenthangar/t/issues/382).

This is one coordinated change. SQLite access, ownership checks, launch
verification, transfer recovery and public resume guidance ship together.
The approved CLI planning preview covered open, resume, move, close and list,
including refusal and recovery states. The public command families remain intact.

## Findings and decisions

A checkpointed Codex WAL database can be readable and writable while a read-only
SQLite connection fails with `SQLITE_CANTOPEN` because its WAL/SHM sidecars are
absent. The move rollout verifier and ownership reader previously handled that
condition differently. A disposable reproduction confirmed the failure and the
existing-file fallback; no live database was repaired during investigation.

`libexec/t_codex_store.py` now supplies the shared reader. Action preflight retries
only CANTOPEN with `mode=rw` and `query_only=ON`. It never creates a database,
uses normal SQLite locking, and closes each connection. Metadata-only readers
opt out of sidecar initialization. Permission errors, corruption, persistent
CANTOPEN and rollout files shorter than their projection checkpoints remain
failures, rather than becoming empty history.

The resume incident involved a different held writer whose thread had no index
row, saved rollout or open rollout file identifying its workspace. A shared
backend's process cwd cannot establish the workspace of an individual thread.
Reused-workspace, saved-resume and transfer guards remain conservative. The
worktree allocator can attest a newly created path with its device/inode identity;
launch verifies that identity before collecting unresolved sibling IDs in its
receipt instead of treating them as a conflict everywhere. Reused, restored or
debris paths never receive this exception. Known same-workspace writers and the
shell's locked owner/reservation checks remain enforced. Diagnostics identify
the unresolved conversation and holder when evidence permits, explain release, and
use public `t resume` recovery. The selected lock, sibling locks, transcripts and
shared backends are never removed or killed to bypass an unknown writer.

The review also found independent send/receive ordering, a launch-success check
that accepted a living pane without a loaded conversation, self-move ordering,
mtime-based transcript synchronization, unsafe ordinary-index checkpoints,
missing lost-reply recovery, and close events recorded before confirmed release.
Two additional old paths could resume despite an old process remaining alive:
`pop`, and wrapperless `push`. Both now refuse that overlap.

Final review also reproduced session-name recycling during close and transfer
stop. Both paths now retain the immutable tmux ID from before ownership inspection
through the stop effect. A replacement using the same name is never a stop target.
The regression covers recycling during inspection and immediately before stop,
with an unchanged-owner control; both race cases failed before each correction.

The additional saved-resume report exposed a mismatch between the picker and
source reconciliation. Two repository names can have the same verified GitHub
repository ID, agent and conversation ID. The picker already merged their
records, while reconciliation required exactly one raw record. The shared
`t_resume_source.merge_records` function now merges their associations and
transcript sources in both paths after repository equivalence is verified.
The latest durable association still determines the selected source. Equal-time
associations that disagree on host, workspace, generation, branch or revision
are refused explicitly. Source evidence is read again after acquiring the
ownership claim, and unavailable or mismatched repository proof never authorizes
a merge. No stored history or lock is deleted to make the operation succeed.

Validation also reproduced a pre-existing recovery-menu failure on tmux 3.8:
`display-menu` now returns before dismissal. Cleanup no longer treats that return
as a close receipt. Dismiss explicitly releases the notice; Escape on newer tmux
leaves ownership unresolved until the user explicitly reopens recovery. Older
tmux keeps its close-receipt behavior. Native terminal tests cover both dismissal
and explicit reopen without sending recovery input into the agent.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| `t_codex_store.py` | SQLite access and rollout checkpoint integrity |
| `t_codex_ownership.py` | Selected native lock, sibling workspace evidence, unidentified writer refusal |
| `t_lifecycle.py` | Shared launch policy, claims, durable operation receipts, move/restore phases, verified import, retry reconciliation |
| `zsh/lifecycle.zsh` | Versioned adapter for terminal, process, tmux, SSH, Git/worktree and copy effects |
| `t_history.py` | Internal saved-conversation discovery, exact source/repository/generation selection; shared claims and writer publication guard |
| `t_resume_source.py` | Protected source checkpoints and source reconciliation |

The shell retains selection and confirmations. Open and resume call the same
launch engine; restart retains its pane/draft/old-client checks and calls the
engine for replacement. Close records completion after process exit and native
writer release. Foreground resumes use shared ownership preflight; pop closes
through the engine first. Push uses its post-exit continuation or refuses when
no safe continuation exists.

Send, receive, remote relay and saved remote restore share a transfer controller.
Public `history` spelling remains a hidden compatibility route. Help, completion
and recovery directions expose `resume`. Read-only list/status behavior remains
read-only. An explicit resume of an interrupted transfer reconciles that operation
before opening saved-conversation selection.

## Transfer ordering

1. Verify the exact source conversation, agent and workspace. Capture repository,
   branch, revision and workspace generation evidence.
2. Negotiate the destination protocol. Reserve a fresh destination slot and
   validate snapshot publication and private-file/index safety before source stop.
   Reserve the source workspace against another t launch while transfer is pending.
3. Stage identity metadata and check destination native ownership before stopping
   the source. An unsupported peer, unreadable store or unknown writer fails here.
4. Revalidate and stop only the selected source owner. Verify process exit and
   native writer release. A shared backend is never a signal target.
5. Recheck that no source owner has appeared and that workspace generation and
   branch still match. Checkpoint through the protected index and publish an
   immutable snapshot ref with an expected-absence lease.
6. Copy the selected transcript and its auxiliary files into private staging.
   Verify identities, lengths and SHA-256 hashes. Preserve divergent destination
   data. Publish under the native writer lock where applicable.
7. Materialize the reserved destination from the exact snapshot. Launch with the
   shared preflight/claim path and wait for the same immutable tmux owner, actual
   agent process, selected conversation identity and Codex writer readiness.
8. Clean up only the matching snapshot and reservations. An already deleted ref
   is a successful cleanup retry; a changed ref is retained. Cleanup failure is
   reported separately from a ready conversation.

A self-move runs in a detached worker outside the source process group. It reports
that the move has started and the private progress-log location, rather than
claiming destination readiness before verification. Foreground transfer mode
uses the verified destination and then the guarded pop path to enter its CLI.

## Recovery and safety

Operation IDs and private JSON records persist intent before effects and receipts
after effects. Records are read again under their operation claim. The peer
persists its own receipts, so a lost SSH acknowledgement does not authorize a
second launch or snapshot publication. A pending launch reconciles the exact
existing owner. An ambiguous outcome never authorizes restarting the source.

`t resume REPO SLOT` on the initiating host retries an interrupted transfer.
A failure after source stop retains source work, snapshots and staged files.
Source/workspace reservations remain until the verified operation finishes.
Multiple matching interrupted operations require inspection rather than choosing
one by recency. Destination divergence and unknown ownership remain refusals.

The new mutating peer protocol requires the coordinated version on both hosts.
Transfers require an isolated source worktree and an allocatable destination
worktree. Existing low-level legacy transport readers remain compatibility
implementation details; public transfers do not choose the old mtime policy.

## Validation record

Before implementation, the public launch/attach/resume/list/close lifecycle suite
passed **19 tests**, using real isolated tmux servers, Git repositories and native
fake agents. Existing transfer/snapshot/routing suites passed **210 tests**.

The initial bug baseline had **five expected failures**: four portable injections
of SQLite CANTOPEN and the public-resume diagnostic assertion. Six directly
related refusal/control checks passed. These regressions are required to pass
with the implementation while preserving the refusal assertions.

The added two-host tests use real Git, tmux and rsync with separate HOME/XDG/Codex
stores and SSH transport simulation. The old-code run passed three of four move
cases and exposed another Codex send transcript-layout defect; this was retained
as a regression, not hidden by weakening its transcript assertion. The new path
passed all four cases. Additional tests inject disconnects before source stop,
after source stop and after a successful launch whose reply is lost, then retry
through public resume and assert one owner and preserved edits.

A final-manifest regression also proved that auxiliary files created while the
source was live must have their staging directories refreshed after shutdown.
That test failed before the fix and passes after it. Direct real-SQLite tests
cover projection lengths, legacy schemas, corruption and query-only reads.

Shell argv/selection tests now stop at the lifecycle adapter boundary; policy
fault ordering moved to engine tests, and true process readiness is covered by
public command tests. Test agents used for real process verification are native
executables, not Python scripts accepted by weakening production detection.
Installer fixtures include the new runtime dependencies.

The full acceptance run passed **6,544 tests**, with **one skipped** and
**97.05% coverage**, in 20 minutes 36 seconds. It includes the same 19 public
lifecycle baseline cases and all 11 new transfer/recovery/restart/pop E2E cases.
CLI and progress contract lint, Bash shellcheck, changed zsh syntax, compilation
to `/tmp`, and `git diff --check` passed. Required Linux CI validates the combined
PR commit. Native macOS UI tests remain opt-in.
After the final immutable-owner correction, the affected engine, kill, ownership,
snapshot and public lifecycle suites passed **343 tests**, including all six
recycled-name cases and all 30 public lifecycle E2E cases. Combined coverage
remained **97.05%**; contract lint, shellcheck, syntax and compilation checks were
rechecked successfully. The #382 follow-up has its own validation below.
The user approved the planning preview and subsequently authorized shipping
the complete implementation.

For #382, both public `t resume` reproductions (Codex and Claude) failed with the
reported source-identity error before the correction and passed afterwards,
preserving dirty work and exactly one resumed owner. These use real Git, history
storage, native agent processes and tmux, with simulated GitHub rename proofs and
picker input. All 88 source-reconciliation tests passed, including equivalent
records, conflicting latest associations, missing/mismatched repository proof
and evidence changing while waiting for the ownership claim.

The follow-up full run collected **6,571 tests**: **6,568 passed**, **one skipped**,
and two existing tmux resume-startup cases failed when tmux returned
`respawn pane failed: fork failed: Device not configured`. These failures happened
before source reconciliation. Both cases passed on an initial targeted rerun,
and the complete **32-test tmux recovery suite passed** afterwards with coverage
appended. No production code or assertions were changed to bypass those fork
errors. Total coverage is **97.09%**. All 32 public lifecycle E2E cases (19 original
and 13 transfer/saved-resume cases) passed in the full run. CLI/progress lint,
Bash shellcheck, changed-shell syntax, compilation to `/tmp` and diff checks
passed. Required Linux CI and deployment are tracked in PR #388.
