# Initial context-use findings

This delivery implements enabled-workflow guidance, deterministic runner
inspection, a local audit script and selective visual-validation guidance.
Workflow settings and dependent sharing-runtime changes were deferred at the
user's request. No external frontend-design skill was identified or modified.

## Controlled comparison

The old `logs` path and new `inspect` path read the same synthetic retained log:
30,000 progress lines followed by a pytest failure and summary.

| Measurement | Complete retained log | Inspection |
| --- | ---: | ---: |
| Returned characters | 540,147 | 731 |
| Failure and test summary | Present | Present |
| Recorded status / exit code | In separate receipt | Failed / 1 |
| Full retained evidence | Unchanged | Retrieval reference |

The response is 99.86% smaller for this fixture. This establishes a reduction in
returned characters on identical input, not token or monetary savings. Tests also
cover pending, cancellation, missing/empty/unavailable logs, unrecognized output,
storage loss, scan limits, long lines, target selection and machine output.

Bounded-watch exercises verify that a timeout remains pending without cancelling
execution, retaining snapshot identity and the authorized next step. Completed
waits retain that next step too. The watcher already checked the PR head before
reading checks; review found an acceptance race during the subsequent workflow
lookup. It now rechecks the head before returning completion. A changed head
fails verification; unavailable final verification remains pending.
Live CI observation also exposed a deadline probe that could discard the last
useful pending result when its network request timed out. The wait now stops at
the budget boundary without that extra request, retaining observed checks and
identity. A controlled regression verifies one read and preserved pending facts.

A synthetic repeated-read exercise includes an unchanged consecutive read and a
read after an edit. Both are candidates; manual review classifies the former as
reusable evidence and the latter as necessary. The script does not decide read
necessity from repetition alone or maintain a live source-read ledger.

## Initial local sample

Session discovery tools were used first and identities verified before reading
four available local transcripts: the planning conversation and three recent
applicable sessions. The four workspaces cited in the earlier report could not
be resolved through current session discovery. Registered remote discovery was
also unavailable. Those earlier report figures are not independently reproduced.
Personal transcripts, identities, commands and session-specific reports remain
outside Git. Inputs were copied to private frozen snapshots with content hashes
for repeatability; this summary contains aggregate measurements only.

The available sample contained 264 recorded tool-result events and 2,042,678 text
characters, including 42 results above 20,000 characters. Heuristic classification
found 51 status-query candidates, eleven bounded-watch calls and no short shell
sleep calls. Three image attachments were recorded, with no proven identical
inline payload repeats. These measurements describe the available historical
sample; they do not demonstrate adoption of the new guidance.

Manual review of three repeated-read candidates found reads of a changing runner
log, not redundant reads of unchanged source. Repetition alone therefore does not
prove waste. Usage snapshots were available, but their cumulative/per-message
semantics differ and they were not summed. Workflow revision was not recorded in
these transcript inputs. No inspection-followed-by-retrieval candidates appeared
in the pre-change sample; adequacy of excerpts under broader use is unestablished.
During controlled validation, an initial excerpt omitted the coverage total and
required a bounded follow-up read. Extraction now recognizes coverage totals as
summaries too; complete retained logs remain available for further detail.

The isolated HTML planning preview was served locally. Browser access was
unavailable on both attempts, so the requested component/responsive screenshot
exercise could not be visually verified. This is an explicit validation limit;
terminal geometry and required design fixtures remain separate checks. No
screenshot savings or demonstrably redundant captures are claimed.
The required terminal design suite passed 146 cases locally; CLI, progress and
layout inventory checks passed. The two new modules passed their focused tests
with 100% line coverage.

## Interpretation

**Implemented:** guidance and tooling described above, with workflow settings
untouched. **Observed effective:** bounded output for the controlled failing-log
fixture. **Not yet established:** sustained real-session reductions, broad
adoption, fewer redundant screenshots, lower token consumption or monetary
savings. Repeat the [private audit procedure](context-use.md) after real use;
no monitoring service is installed by this delivery.
