# Local inference and coding workers

`t workflow` → select Codex and a workflow → **Inference & routing** selects a
local model and one of four modes. `t config workflow` remains a compatibility
alias. Saving a workflow uses the normal staged review/save transaction.

| Mode | Behavior |
| --- | --- |
| Disabled | Reject new local delegations for that scope. |
| Manual | The lead delegates only when asked. |
| Automatic hybrid | The current cloud lead chooses bounded local subtasks, validates results, and owns final acceptance. |
| Local only | New native Codex CLI launches use the exact selected local model; unavailable inference stops the launch without cloud fallback. |

Automatic routing is agent-driven task selection, not a model proxy. Generated
workflow instructions ask the lead to assess scope and available checks before
each meaningful subtask. Bounded investigations, mechanical changes and small
patches are candidates. Architecture, ambiguous debugging and final acceptance
stay with the lead. The lead announces the model and routing reason, calls MCP,
then receives a proposed patch and observed command evidence. Failed, busy or
incorrect work is reported and continues in the lead. No hidden retry loop or
full conversation transfer occurs. Automatic native CLI launches preapprove only this adapter’s start/status/cancel
tools when setup is enabled; other MCP tools and managed approval constraints
remain unchanged. Existing conversations need refreshed MCP
tools and instructions; saving cannot hot-switch their underlying provider.

Local-only is currently a **fresh native CLI launch** capability. The launcher
isolates Codex HOME/configuration and credentials, pins the OSS provider/model,
and disables cloud model stages, recursive subagents and inherited MCP servers.
It reserves one local inference slot for the lead, so work stays in that lead.
Cloud/local resume is rejected; start a new session. Desktop launch/provider
switching is not supported by this launcher. A local-only workflow is an inference
policy, not an offline-tools or filesystem security boundary.

## When the internet is unavailable

Open `t workflow` → Codex → your workflow → **Inference & routing** →
**On cloud connection failure**. Choose **Pause and retry** (the default),
**Ask which local model**, or **Continue locally with selected model**, then review
and save. This extends the
existing workflow command; it adds no new public command or required flag.
Install and verify the model beforehand with `t config models install MODEL`.
For planned offline work, choose **Local only** and the explicit model directly.

Continue locally applies to **fresh native Codex CLI launches** made through t.
It starts an owned Codex app-server and attaches the native terminal UI through a
private Unix socket. This requires app-server stdio and Unix `--remote` support
(verified with Codex CLI 0.164.0). Approvals pass through unchanged. The supervisor watches
typed transport errors for the primary conversation: it switches when that turn
exhausts connection retries, or after 30 seconds of unsuccessful reconnection.
Useful progress resets that window. It ignores auxiliary title/subagent threads,
authentication/quota failures, HTTP application errors and user interruption.
The default Pause setting preserves Codex's normal retries and error handling.

With **Ask which local model**, a sustained transport failure opens the shared
terminal picker. Installed, ready models show their provider and memory fit;
unavailable choices explain why they cannot run. Selecting a ready model starts
the handoff immediately: there is no model-details or confirmation screen. The
picker dismisses into the native local session. Esc/Cancel or **Pause and retry
cloud** leaves the cloud runtime available and suppresses another prompt for that
turn. Noninteractive launches keep native cloud retries and never prompt.

Before switching, t validates the checkpoint and stops its owned cloud runtime.
The selected provider/model and verified artifact revision are printed as the
**new local-only conversation** starts in the same worktree. Readiness is checked
again at startup; if startup fails, recovery choices remain available with the
error. No weights are downloaded. Choices affect this session, not the saved
workflow. Only one runtime can write to the worktree during either handoff.

In Ask mode, a credential-free HTTPS check against the OpenAI API runs while
local work continues. Three consecutive successful reachability checks, five
seconds apart, trigger one recovery offer at the next safe turn boundary. This
checks connectivity, not authentication, quota, or inference availability. The
picker never interrupts a running tool or pending approval. Choose **Return to
previous cloud model**, **Choose another cloud model** from the native catalogue,
or **Stay local**. Cloud return requires confirmation and transfers an updated
bounded checkpoint including local progress. Stay local dismisses the prompt
for this recovery event; it never switches automatically. Custom cloud endpoints
are not independently health-checked. Local-only launches made directly, and the
existing automatic **Continue locally with selected model** policy, stay local
without this recovery offer.

To try the shared picker without any network/model launches or config changes:

```sh
python3 scripts/preview_connection_recovery.py
```

This worktree-scoped preview uses labeled sample data. It is separate from a
real native CLI launch with the saved Ask policy.

The private checkpoint contains the user requests (up to 32,000 characters) and
eight bounded recent messages, command results, plans or changed-file lists.
It is not the complete conversation or hidden reasoning. The continuation must
inspect current files, preserve existing changes and avoid repeating completed
external actions. Non-text input or oversized task instructions pause recovery
instead of silently dropping context. Checkpoints and runtime diagnostics live in
`$XDG_STATE_HOME/t/local-worker/outage-*` (default `~/.local/state/t/local-worker`).
Directories are private and checkpoint files are mode 0600; remove completed
directories when no longer needed.

Existing cloud sessions, resume/fork, exec/review, Desktop, and custom
remote/profile/OSS/worktree overrides are outside this initial continuation path;
select Pause for those invocations. Saving a setting cannot replace the model in
an already-running conversation. Local launches copy only an existing explicit
trust decision for the exact workspace, never cloud credentials or provider
configuration. Untrusted workspaces still require native trust confirmation.
Downloads, web search, package installation, pushes and other network-dependent
tools may remain unavailable offline even though local inference works.

## Model picker and commands

The picker shows **Memory fit ✓ / ✕ / ?**, one selected benchmark and installation
state. Unsuitable or unknown-fit models are dimmed but remain inspectable.
Use/Install actions are disabled when fit or readiness does not permit them;
Download only remains available when disk space permits. Unknown speed is omitted.
Details include artifact ID, precision, download/runtime estimates, source links,
revision and any measured speed. Install and Measure are immediate explicit actions;
workflow selection remains staged until Save.

```sh
t workflow
t config models list --suite swe-bench-pro
t config models show mlx-community/Qwen3.8-27B-8bit
t config models install mlx-community/Qwen3.8-27B-8bit --yes
t config models measure mlx-community/Qwen3.8-27B-8bit
```

`list` and `show` are read-only and never start inference or open a picker.
`--provider lmstudio|ollama` filters or disambiguates artifacts. All commands
accept `--json` and `--context TOKENS` (default 32768). `refresh` updates the existing public Ollama/LiveBench metadata
cache; curated MLX identities and publisher scores ship with t and are not scraped
or silently replaced. Model installation pins the current Hugging Face commit,
resumes partial downloads and verifies published LFS SHA-256 checksums. A receipt
is written only after every selected file is complete. Downloaded and inference
verified are separate states. The workflow model overview shows status, file size,
memory fit and context beside its next action. Benchmarks and Artifact & runtime
open separate detail views; sources and methodology remain available.

For an existing download, choose **Verify readiness**, or rerun
`t config models install MODEL --context 32768 --yes`. This reuses complete files,
loads the exact artifact at the requested context, tests response text, then releases
the owned model. A failed or cancelled re-verification clears previous readiness;
errors retain the downloaded files. New verification evidence records the machine
and context. Browsing does not verify, load or install anything.

Flash-Next can be explicitly verified on supported hardware with a compatible
LM Studio runtime. Its [publisher model card](https://huggingface.co/nace-ai/Qwen3.8-Flash-Next-MLX-4bit)
describes about 73 GB resident weights and a 32 GB memory-mapped n-gram table on a
128 GB Mac. t uses that resident estimate plus overhead for verification eligibility;
Memory fit remains unknown until successful inference. This estimate does not
prove LM Studio supports the mapping. Runtime architecture and memory-guardrail
failures remain visible; t never overrides those guardrails or marks a failed load
Ready. Update the runtime or choose another artifact when verification fails.

The four curated MLX artifacts are:

- `mlx-community/Qwen3.8-27B-8bit` — default candidate.
- `nace-ai/Qwen3.8-Flash-Next-MLX-4bit` — requires memory-mapping runtime verification; at least 128 GB RAM.
- `mlx-community/Qwen3.5-122B-A10B-4bit`.
- `lmstudio-community/Qwen3-Coder-Next-MLX-6bit`.

SWE-bench Verified bash-only is the conservative initial suite: missing exact
results stay blank. Choose Pro, DeepSWE, GPQA, Terminal-Bench or LiveBench Coding
explicitly. Results from different suites, versions and harnesses are never
combined into an overall ranking. Publisher scores refer to original models,
not the quantized artifact or this Codex worker. Source/harness caveats appear in
details and JSON. SWE-bench is useful coding-agent evidence, not a complete measure
of local quality, speed or reliability. Acceptance rate and review/repair time on
your own tasks are the more useful routing feedback.

LiveBench v1 data comes from its official public repository; `--benchmark DATE`
selects a cached release and requires `--suite livebench-coding` when a suite is
explicit. Ollama availability uses tool-capable library families and canonical
parameter-size tags, not every community artifact. Cached reads send no hardware,
paths or repository information to metadata sources. Refresh failures preserve
the previous cache. MLX fit uses Apple unified RAM minus 20% OS headroom, 20%
weight overhead and a context allowance (4 GiB at 32K, scaled with configured
capacity). This is an estimate, not an architecture-specific KV measurement or
promise under memory pressure. Special runtime requirements remain unknown-fit.

## Setup

Install LM Studio or its headless llmster runtime and the MLX engine on Apple
Silicon. For Ollama, existing setup can install the runtime and Codex using reviewed
vendor commands. Then open **t config → Local worker & models**, or use:

```sh
t config local --model mlx-community/Qwen3.8-27B-8bit --yes
t config local --show
t config local --disable --yes
```

Setup verifies the model, registers `t-local-worker` through `codex mcp add`, and
enables delegation. A conflicting registration is preserved and reported. Refresh
MCP tools or reopen Codex after setup. `--show` is read-only. Disable keeps models
and registration, rejects new tasks, and cannot interrupt a running task. Model
installation alone does not register MCP or change a workflow.

## Measuring speed

`measure` acquires the same exclusive local slot as workers, loads only the
selected artifact, runs one discarded warmup and three fixed short coding prompts,
and saves median/min/max, runtime, artifact revision, hardware and configured
context capacity. It does not score coding quality or run SWE-bench. The context
capacity is 32K by default; the measurement does not prefill 32K tokens.

Ollama supplies decode token counts and decode duration. The MLX path currently
uses output token counts divided by total request time, **including prefill**;
it labels that metric explicitly instead of presenting it as pure decode speed.
Missing token usage fails measurement without fabricating a speed. Details mark
measurements stale when artifact, context or hardware identity differs.
Only an owned model alias is unloaded; a preexisting LM Studio server is retained.
An owned loopback server is stopped on completion or failure.

## Opt-in acceptance test

After installing the 27B artifact, run:

```sh
python3 scripts/test_local_inference_e2e.py --mode hybrid
python3 scripts/test_local_inference_e2e.py --mode local-only
python3 scripts/test_offline_e2e.py
```

These commands use real inference and are never invoked by routine CI. Hybrid
uses the signed-in cloud Codex lead and requires an actual automatic MCP delegation,
verified local model identity, worker patch, observed successful command, unchanged
fixture tests and independently passing acceptance tests. Local-only uses the
native launcher and requires local runtime identity and passing acceptance tests.
Hybrid additionally requires the lead source edit to follow the worker result,
so duplicate work cannot pass as patch handoff.
Each run prints an evidence directory. They use temporary Git repositories and
separate XDG state and never edit the developer's project or workflow configuration.

The offline test is macOS-only and uses a headless terminal, without opening
desktop windows. It denies external connections for the launcher, cloud/local
clients and descendant tools, verifies that denial, and permits loopback access to
the installed LM Studio runtime. The preexisting model daemon is outside that
test sandbox; it does not disable the laptop's network. The outer test sandbox
also bounds writes to fixture/temporary paths; native sandboxing is disabled only
inside this fixture because macOS cannot nest sandbox-exec. Acceptance requires
the original task checkpoint, the announced exact local model, an unchanged test
file, a completed local turn and independently passing tests. It retains terminal,
network-boundary and checkpoint evidence in the printed temporary directory.

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
Results retain provider, requested/resolved model, runtime alias, artifact revision,
identity verification, routing reason, elapsed time, findings, command evidence, events, and a full
patch for review. MCP returns bounded excerpts, with a truncation flag for patches.
Caller-supplied parent session IDs are explicitly unverified and never become ownership evidence.
Artifacts remain after completion for review; remove completed task directories
when no longer needed. They are local data and must not be committed.

Measure accepted changes and time to completion, including review and repair effort.
A local worker is worthwhile only for tasks where delegation improves those results.

References: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Ollama configuration](https://docs.ollama.com/faq).
