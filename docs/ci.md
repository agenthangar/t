# CI test scheduling and coverage

Routine PRs and pushes to main run one **Essential Linux smoke** job using
[`scripts/smoke.py`](../scripts/smoke.py). Local pre-push validation uses the exact
same explicit pytest node IDs, including parameter IDs. New files, tests and
parameter values do not silently enlarge smoke. Missing, skipped or unexpected
cases fail the smoke gate. The list covers shell startup and Ctrl-C recovery,
session discovery, open/reattach/resume, concurrent ownership, preservation of
uncommitted work, and the remote `zsh -lic` contract.

CLI, history, installer and CI changes all select smoke in routine PR/push CI.
There is no changed-path escalation. Manual **CI** defaults to
`run_full=false`. Set `run_full=true` for complete Linux validation.
`verify_shards=true` also selects it. Nightly and release runs select full Linux
validation for the exact pinned commit, even without a pending release. Full
Linux validation enforces 97% coverage and supplies release evidence.
Gitleaks, pip-audit, CodeQL, shell/CLI contracts and install smoke
remain required. Scorecard and scheduled failure/recovery alerts are unchanged.

## Local publication gate

Commit the intended work, then run from the worktree root:

```sh
python3 -m venv /tmp/t-smoke-venv
/tmp/t-smoke-venv/bin/python -m pip install -r requirements-dev.txt
/tmp/t-smoke-venv/bin/python scripts/local_smoke.py
```

The host needs zsh, tmux, git, a C compiler and fzf. The script calls the same
local snapshot runner backend as `t runners runs start --target local`, explicitly
selects local execution, and runs `scripts/smoke.py` with that Python interpreter.
It uses clean HOME/XDG and the tests' isolated tmux sockets; native desktop tests
remain opt-in. Do not replace this with a full local regression/coverage run.
Run focused policy or regression checks when needed for the specific change.

The runner retains logs and artifacts under its private state directory. The
script prints the run ID, snapshot ID and source commit; inspect with
`t runners runs inspect ID` or `t runners runs logs ID`. Its latest receipt is
stored in the worktree's private Git directory, never committed. Record those
identifiers in the PR validation evidence without publishing private logs/config.

The installed repository hooks (`core.hooksPath=.githooks`, configured by
`install.sh`) check this receipt before branch pushes. Configure that same path
in a fresh development clone before publishing. The hook checks the pushed HEAD,
clean tracked files, snapshot contents and success. It does not run broad tests.
After integrating main, resolving conflicts, amending or changing source, commit
and rerun smoke before pushing. A different commit or file content invalidates
previous evidence, even when an earlier run passed. A failed new run replaces
old success and blocks the push. Push other branches from their own tested
worktrees; branch deletions and tag-only pushes do not publish source changes.

## Full Linux validation

Full validation includes every smoke flow in the complete collection instead of
rerunning the separate smoke job. It keeps the Linux suite serial within four independent
GitHub-hosted runners, with fzf 0.74.5. Each runner collects the full suite,
then selects complete files. `scripts/ci_shards.py` assigns longest estimated
files first to the least-loaded shard; filename and shard index break ties.
New files receive the median known weight. `tests/ci-file-durations.json`
starts with approximate file timings from [the previous full Linux run](https://github.com/agenthangar/t/actions/runs/41232234489)
(fzf 0.74.5, 638.75 seconds). These estimates affect scheduling only.

Every full shard requires real fzf/tmux/zsh before testing and sets
`T_REQUIRE_AWS_CONTRACT=1`, so the AWS contract runs once in its assigned file.
A separate fzf 0.44.1 compatibility job selects the real-fzf compatibility
cases during full validation. It writes the same explicit selected-test manifest.

Artifacts persist for 14 days: full collection, selected files and node IDs,
per-test outcomes and setup/call/teardown duration totals, JUnit, and uniquely
named coverage data. The coverage job rejects missing/duplicate shards,
differing collection or commits, overlap, incomplete outcomes, and absent or
corrupt coverage. It normalizes checkout prefixes, combines measured lines,
and enforces the existing scope/exclusions and 97% minimum only on the union.
The job summary reports timings, test totals, coverage and the 30 slowest tests.

To refresh scheduling weights explicitly, download the four `shard-*` artifacts
from a successful run into separate directories and run:

```sh
gh run download RUN_ID --pattern 'shard-*' --dir /tmp/t-ci-timings
python scripts/ci_shards.py refresh /tmp/t-ci-timings
```

Review and commit the updated timing file. Refresh does not run during CI and
cannot silently modify scheduling in an existing revision. Multiple downloaded
runs may be placed under separate parent directories; refresh takes the median
of observed file totals.

For a same-commit equivalence audit, dispatch `CI` with `verify_shards=true`.
This default-off mode runs the original full Linux suite with current fzf,
then compares the exact inventory, outcomes, measured source files and executed
lines with the combined shards. Missing artifacts or differences fail `CI`.
Use this mode when changing scheduling. The performance target is routine Linux
completion below six minutes, at least 40% faster than the approximately
736-second original CI baseline; compare GitHub job/run timestamps, including
setup and aggregation, rather than only pytest execution time.

### Required design adherence

Before opening a PR that changes visible behavior, shared rendering, design guidance
or its reference, run both design checks on the proposed commit and include the
results in the PR description. Activate the development Python environment,
place each pinned fzf binary on PATH in turn, and run:

```sh
PATH="/path/to/fzf-0.44.1/bin:$PATH" scripts/check_design.sh 0.44.1
PATH="/path/to/fzf-0.74.5/bin:$PATH" scripts/check_design.sh 0.74.5
```

The modern leg installs the pinned Playwright test dependency in a temporary
directory and runs the HTML reference in its managed headless Chromium. Review
the affected rendered states separately. Nightly, release and explicit full
validation use the same `design-adherence` matrix. It validates `ui/layout-contract.json` references and every command/mode's
layout declaration, then runs those rendered fixtures with a no-skips outcome
audit. The full `CI` gate requires both matrix jobs to succeed for the selected commit.
Artifacts named `design-<version>` include JUnit, exact collected/outcome records,
and failure output with rendered frames and coordinate comparisons. Tests use
private tmux sockets and sandbox homes; no native desktop windows are opened.

### Session discovery and transfer regressions

The installed-command smoke case holds the history collector open and checks the
first list and picker against known live conversation details. The full suite
also exercises 61 numbered slots. Run `python3 tests/ssh/move.py` with Docker
to test a transfer between two disposable Linux hosts with real SSH, independent
worktrees and a shared Git origin. Full Linux CI runs this transfer. The CI
workflow's `run_macos=true` and `macos_scope=discovery` inputs select the
hosted macOS discovery checks.

## Targeted macOS validation

Routine PR/push, nightly and release CI run Linux. Hosted macOS checks are
explicitly selected with `run_macos=true` and `macos_scope`. The default scope
is `discovery`. Each run uses one `macos-latest` machine, fzf 0.74.5, and the
exact tests selected by `python3 scripts/smoke.py --macos SCOPE`.

| Scope | Checks |
| --- | --- |
| `discovery` | First-list metadata, 61 live slots, process identity and working-directory matching |
| `terminal` | PTY loss versus detach, tmux client detach, picker suspension and paste restoration |
| `native` | Real IOKit and pmset system/display sleep assertions |

Missing, skipped or unexpected cases fail the selection check. Only `native`
enables `--run-macos-ui`. Run it on a disposable Mac or with explicit authorization.

For example, request hosted terminal checks on a branch with

```sh
gh workflow run ci.yml --ref BRANCH -f run_macos=true -f macos_scope=terminal
```

Run the same headless checks locally with `python3 scripts/smoke.py --macos discovery`
or `python3 scripts/smoke.py --macos terminal`. A macOS selection uses routine
Linux checks unless full Linux validation is also requested.
