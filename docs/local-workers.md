# Local coding workers

`t` can connect a lead Codex conversation to a separate local Codex CLI worker,
using Ollama for inference. The lead keeps its current model and conversation.
The worker receives a bounded assignment and returns findings, a proposed patch,
and observed command results. The lead reviews and validates those results.

## Setup

Open **t config → Local worker & models**, or run:

```sh
t config local
```

The shared settings UI reviews prerequisite installers, the model download and
Codex MCP registration before applying. Its model picker ranks **evaluated fits**
first, then fitting models with no matching evaluation. Other models remain
inspectable and selectable, with their memory or disk limitation shown. Custom
Ollama names remain available. Setup verifies tool support after downloading.

```sh
t config models list
t config models list --json
t config models refresh
t config models list --benchmark 2025-04-25
```

`list` is offline and read-only: it never starts Ollama, refreshes metadata,
changes settings, or downloads weights. `refresh` fetches public metadata only
and atomically updates a user cache. The picker refreshes metadata older than
seven days and has an explicit Refresh choice. Failed refreshes preserve the
last complete cache; a bundled snapshot supports first use offline. Refresh
errors are shown, not silently treated as current results.

Availability comes from [Ollama's library](https://ollama.com/library) and tag
pages: up to 64 tool-capable families and their canonical parameter-size tags.
This discovers new families without a t release, but does not enumerate every
community model or quantization. Published tag download sizes use the upper end
of any range. Tool support still requires setup's actual model metadata check.

Coding scores come from the official
[LiveBench v1 data repository](https://github.com/LiveBench/livebench.github.io/tree/main/public).
Refresh discovers its latest 12 releases. Scores are the arithmetic mean of the
release's own Coding task columns, on a 0–100 scale. The default selects the newest
release containing an evaluated estimated fit; if none exists, it shows the newest
release without a recommendation. `--benchmark DATE` selects another cached
release. All scores in a view belong to that single release; releases and other
benchmarks are never blended. The selected and newest available dates are shown.
This source currently covers v1, not the newer LiveBench 2.0 benchmark.

**Best evaluated fit is not a claim of the best model overall.** Small, newer
models often lack matching scores in this source. They remain unranked rather
than receiving invented scores; a recommendation can therefore have older
benchmark evidence. JSON records the exact evaluated model name and source URL.
Matching uses explicit name/variant aliases, never fuzzy family matching. Hosted,
thinking and instruction variants are base-model evidence: quantized Ollama
weights, Codex's harness and local speed were not measured by these results.

Suitability uses detected RAM, Apple unified memory or the largest NVIDIA GPU,
and free space on the model volume. Unknown accelerators stay unknown; CPU/partial
offload is not assumed fast enough. The estimate reserves max(4 GiB, 20% RAM) for
the OS, 10% accelerator capacity, 20% weight overhead and a fixed 4 GiB allowance
for the worker's 32K context. That allowance is a conservative heuristic, not
architecture-specific KV sizing or an inference guarantee. Disk requires the
estimated missing download plus 2 GiB free; complete installed blobs can be reused.
Lists expose estimates and reasons, and never switch your configured worker.

Metadata is cached in `$XDG_CACHE_HOME/t/local-models.json` (default
`~/.cache/t/local-models.json`). Fetches are bounded by size, request timeout and
a scheduling deadline. They send no local hardware, paths, repository context or
credentials to the metadata sources.

Explicit setup for automation:

```sh
t config local --model gpt-oss:20b --yes
t config local --show
t config local --disable --yes
```

`--show` is read-only and never starts Ollama, downloads a model, registers MCP,
or requests an update. `--disable` prevents new tasks; it keeps model files and
registration. Setup and disabling refuse to change a running worker. Cancel that
task first. Existing cloud-agent model defaults are not changed.

On macOS with Homebrew, setup installs the Ollama formula and the existing Codex
cask when missing. Otherwise it uses the vendor installation scripts shown in the
review. Linux vendor installation may require sudo. There is no worker cloud
login. Failed setup leaves completed installations/downloads in place so rerunning
can continue. Configuration is enabled only after model checks and registration.

Codex owns registration through `codex mcp add t-local-worker`. A conflicting MCP
server with that name is not overwritten. Refresh MCP tools or reopen Codex after
setup; this adapter cannot inject new tools into clients that do not refresh their
tool catalog during a conversation.

## Using the worker

Ask the lead to delegate a bounded investigation or small patch to the local worker.
The dedicated `t-local-worker` MCP server exposes:

| Tool | Input | Result |
| --- | --- | --- |
| `start_task` | Absolute repository-root `workspace`, `assignment`, optional `mode` (`investigate` or `patch`), optional `timeout_seconds` | Task ID and running status |
| `task_status` | `task_id` | Status; when finished, findings, observed command evidence, proposed patch and artifact paths |
| `cancel_task` | `task_id` | Cancellation request; poll until the task reaches a terminal state |

Investigation is the default and runs in Codex's read-only sandbox. Patch tasks
run in its workspace-write sandbox. Neither grants approval bypass or tool network
access. The worker receives the assignment and snapshot, not the lead transcript.
The lead should provide concrete acceptance checks and keep architecture decisions
and ambiguous changes itself. Task execution defaults to five minutes, adjustable
from 10 seconds to 30 minutes; snapshot and server preparation happen before that
execution budget. Failed or timed-out tasks return to the lead without automatic
cloud fallback or retries. Terminal statuses are `completed`, `failed`, `cancelled`,
`timed_out`, and `output_limit`. Completion means the worker process exited
successfully, not that the proposed fix is correct.

There is one worker or setup operation per user at a time, enforced with a file
lock across MCP connections. Tasks belong to the connection that started them.
Closing the MCP connection cancels its work and reaps its child processes. Poll
for progress; the adapter does not promise push notifications or automatic turns
in the lead client.

## Workspace and model boundaries

Each task copies tracked files (including current edits) and nonignored untracked
files into a separate repository with its own Git metadata. It never checks out a
branch, writes the index, or applies a patch in the lead checkout. Ignored files,
including ignored dependencies, are not copied. Submodules and symlinks are
currently rejected; snapshots are limited to 256 MiB. The source should be stable
while the snapshot is being copied. A resulting patch is relative to that snapshot,
so the lead must check it against any subsequent edits before applying it.

Ollama runs only during setup or a task on an ephemeral loopback port, with
`OLLAMA_NO_CLOUD=1`. Existing Ollama servers are not reconfigured or stopped.
Downloads use `~/.ollama/models`; setup can reuse models in that user directory.
Worker HOME, CODEX_HOME, and XDG directories are isolated. Authentication,
proxy variables, lead session identity, user configuration and execution rules
are not inherited. There is no recursive MCP delegation. Installed Codex must
support `exec --ignore-user-config`, `--ignore-rules`, and `--ephemeral`.

The snapshot and isolated HOME are workflow isolation, not a container or a claim
that the model cannot read other sandbox-readable paths. Returned code and findings
enter the lead conversation and therefore may go to its cloud model. This setup
is not appropriate for an entirely local-only repository when the lead is cloud-backed.

Settings live in `$XDG_CONFIG_HOME/t/local-worker.json` (default
`~/.config/t/local-worker.json`). Private task artifacts live in
`$XDG_STATE_HOME/t/local-worker/<task-id>` (default `~/.local/state/t/local-worker`).
Results retain elapsed time, model, findings, command evidence, events, and a full
patch for review. MCP returns bounded excerpts, with a truncation flag for patches.
Artifacts remain after completion for review; remove completed task directories
when no longer needed. They are local data and must not be committed.

Measure accepted changes and time to completion, including review and repair effort.
A local worker is worthwhile only for tasks where delegation improves those results.

References: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Ollama configuration](https://docs.ollama.com/faq).
