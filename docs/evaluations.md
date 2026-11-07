# Workflow evaluations

Use `t eval config` to create, edit, or delete a named configuration. Changes
persist after the review/save screen. `t eval runs start` opens fzf over those
configurations; selecting one immediately starts it. No separate prepare command
or launch review is required. Preparation and progress run in the background.

```
t eval
t eval config
t eval config edit [NAME]
t eval config show [NAME] [--json]
t eval config list [--json]
t eval benchmarks list [--json]
t eval runs start [CONFIG_NAME] [--dry-run] [--json]
t eval runs list [--json]
t eval runs show [ID] [--json]
t eval runs compare [ID] [ID] [--json]
t eval runs resume [ID] [--retry-interrupted]
t eval runs cancel [ID]
```

Missing names/IDs use the shared fzf picker in a terminal. Lists always print;
they never select or start work. Machine output requires explicit targets.
`ls` aliases `list`. Start also accepts `--config FILE` for a configuration JSON
object, and `--frontier-model`, `--local-model`, `--local-provider` and
`--reasoning-effort` overrides. Overrides affect that run's immutable manifest,
not the saved configuration. Dry-run does not install, load models, or start work.

## Configurations

Select the frontier model, exact local artifact/provider, effort, workflow modes,
benchmark profile, task time limit, repetitions, concurrency (1/2/4), and optional
spend guardrail. Automatic effort leaves the model's default in effect. Unknown
effective effort is recorded as unknown. Local model selection reuses the normal
model picker, installation, benchmark metadata and verification. The runner also
reserves memory for the evaluation VM. Local inference requests are serialized
even when multiple benchmark tasks run concurrently.

Modes are Frontier-only, Automatic routing, Required local delegation, Local-only,
and Outage continuation. Required local delegation checks for a completed local
patch and subsequent successful lead inspection/validation commands. Benchmark
correctness and workflow compliance are separate results. Outage continuation
injects a connection failure following successful frontier command execution and
uses t's production supervisor to stop the frontier and start the local lead.
Local-only has no frontier endpoint access and never silently substitutes a model.

## Correctness benchmark

The runner pins [SWE-bench Pro V2](https://github.com/scaleapi/SWE-bench_Pro-os)
at `66f92766bba642462d4bbe5479e83f91f9211862`, verifies published checksums, and
pins amd64 task image digests. Profiles are:

| Profile | Tasks | Default task budget |
| --- | ---: | ---: |
| Quick | 3 fixed tasks | 15 minutes |
| Standard | 11, one per repository | 50 minutes |
| Advanced | Official HARD-51 | 50 minutes |
| Full | 642 | 50 minutes |

Quick/Standard use deterministic SHA-256 ordering with seed 0, independent of
results. Subset scores and changed budgets are **not leaderboard scores**.
The reference solution must pass and a no-op must fail before candidate execution.
The candidate sees public instructions; grading applies only its patch to a fresh
environment and runs the unchanged publisher verifier. Infrastructure failures
remain visible and do not silently shrink the score denominator. Completed runs
show a separate score for each workflow mode, never a pooled benchmark score.

## Runtime and offline preparation

The initial implementation supports macOS with Colima, Docker CLI and Compose,
Buildx, Codex CLI, Python 3.12+, and a ready LM Studio or Ollama model. Install system
dependencies with `brew install colima docker docker-compose docker-buildx`. Start prepares a
dedicated `t-eval` Colima profile and a cached Harbor 0.25.0 virtual environment;
it never switches the user's global Docker context. The official Codex Linux
package is pinned to the installed CLI version, verified against registry integrity,
and runs natively on the VM CPU while benchmark task containers stay x86. Missing dependencies produce
a resumable blocked run. Benchmark downloads, images and packages need internet
until cached; an inference-only offline run still requires those assets.

The local model stays on the host. Containers receive run-scoped gateway leases,
not cloud credentials. Agent-phase egress allows only the gateway's address and
port. Setup and official verification use their published network policies.
Host cloud credentials use OPENAI_API_KEY or the existing Codex login; expired
credentials must be refreshed through the normal login flow. A failed endpoint
or unsupported model is reported, never replaced automatically.

## Measurements and durability

Observed tok/s is output tokens divided by model request time, excluding external
tool execution and local queue time. It includes request/prefill latency and is
not a pure decode benchmark. Frontier and local values use the same definition.
Missing usage remains unavailable. Reasoning tokens are a subset of output tokens,
not charged twice. A dated provider pricing snapshot supplies input, cached-input
and output rates. Subscription inference is labeled API-equivalent, not an invoice.
Local cloud-inference cost is zero; electricity/hardware costs are excluded.
The spend guardrail uses estimates and permits already in-flight requests to finish.
Unknown pricing cannot enforce a positive USD cap.

Manifests record configuration, task IDs, source hash, benchmark revision, image
digests and local artifact identity. State is atomically checkpointed at startup,
model/tool completion, phase changes, trial completion and a five-second heartbeat.
Events are append-only. Completed trials never rerun on resume. An interrupted
trial needs `--retry-interrupted`; its new attempt retains prior evidence and cost.
Each run retains its hashed workflow source, so resuming uses the original code.
Native in-flight generation cannot
be restored; checkpoints preserve the run and completed work, not GPU state.

Configuration: `$XDG_CONFIG_HOME/t/eval.json`; runs: `$XDG_STATE_HOME/t/evals`;
cached assets: `$XDG_CACHE_HOME/t/eval` (standard XDG fallbacks apply).
Real model evaluations are opt-in and are never launched by the ordinary test suite.

Development preview: `python3 scripts/preview_eval.py` opens the actual config
picker with temporary sample configurations and dry-run launch. Add `runs`,
`config` or `benchmarks` to inspect those views. No model calls or real settings
changes occur in this preview.
