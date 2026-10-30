# Test current work on remote runners

`t workflow runs start` sends one immutable snapshot of current files to
configured GitHub-hosted Linux/macOS runners and registered SSH hosts. It waits
for every selected target and retains a local receipt, combined logs and declared
artifacts. Source edits do not need a commit or push. This is test execution;
agent sessions and their one-live-owner rules are independent.

GitHub Actions provisions and disposes of the managed machine. SSH uses an
existing host with Python 3.9 or later and `zsh`; it creates a private temporary
workspace without changing that host's checkout, agents, tmux or login settings.
Linux and macOS support `x64` and `arm64` where the selected backend provides
them. Windows execution is not supported in this version: the process-group,
file-mode and SSH shell protocol is POSIX. Native desktop tests remain separately
opt-in; choosing macOS does not authorize or prove an interactive GUI session.

## Configure

Use **t config open → Agent workflow → Edit default workflow → Test runners**,
or the explicit JSON editor:

```sh
t workflow runners edit
t workflow runners edit careful
t workflow runners edit --repo api
t workflow runners list --repo api --json
```

The global default applies outside explicit repository overrides. A named
workflow shares its entire runner policy with its consumers. `--repo` explicitly
selects a registered repository; configuration commands never infer a checkout
or open a repository picker. Repository exclusions disable execution guidance
and starts. Config-menu edits remain staged until Review and Save; saving does
not contact a host or provision a service.

Example runners JSON (replace connection values with your own):

```json
{
  "default_targets": ["linux", "mini"],
  "timeout_seconds": 900,
  "queue_timeout_seconds": 300,
  "artifacts": ["test-results"],
  "targets": {
    "linux": {
      "provider": "github",
      "repository": "OWNER/private-test-runners",
      "os": "linux",
      "arch": "x64",
      "label": "ubuntu-24.04",
      "capabilities": ["tests"]
    },
    "mini": {
      "provider": "ssh",
      "host": "mini",
      "os": "macos",
      "arch": "arm64",
      "capabilities": ["tests"]
    }
  }
}
```

SSH `host` names a `t hosts list` registration. Its configured SSH target is
resolved at execution. GitHub labels must match their OS and architecture:
`ubuntu-24.04`/`ubuntu-22.04` use Linux x64, `macos-15`/`macos-14` use macOS
arm64, and `macos-15-intel` uses macOS x64. Capabilities are explicit selection
attributes, not installed-software probes. SSH refuses a mismatched OS or CPU.

A dedicated **private** GitHub control repository is required even when the
source repository is public. Bootstrap it explicitly:

```sh
gh auth status
t workflow runners setup --github-repo OWNER/private-test-runners --create
```

Omit `--create` for an existing empty or already managed private broker. Public
and internal repositories are refused. Setup installs protocol files and helper
workflows on the broker's default branch, verifies its immutable repository ID,
and refuses incompatible or unmanaged content. This uses the existing `gh`
authentication; it does not print tokens or change scopes. The credential needs
contents/actions write access to the broker and permission to install workflows
(classic tokens require `workflow`). Organization policies can also restrict
Actions and macOS capacity.

If GitHub refuses workflow installation with 403 or 404, check the credential's
workflow-write permission. For a classic `gh` OAuth token, `gh auth refresh -s
workflow` explicitly requests that permission; `t` never changes scopes for you.
After granting access, retry setup without `--create`. The protocol marker makes
an interrupted installation resumable.

## Run and inspect

Run from the source checkout or any of its linked worktrees:

```sh
t workflow runs start -- python3 -m pytest
t workflow runs start --target linux --include tests/new_test.py -- python3 -m pytest
t workflow runs start --os macos --capability tests --timeout 1200 -- python3 -m pytest
t workflow runs list --json
t workflow runs status RUN_UUID --json
t workflow runs logs RUN_UUID --target linux
t workflow runs artifacts RUN_UUID --output ./downloaded-results
t workflow runs cancel RUN_UUID
t workflow runs cleanup RUN_UUID
```

Use `--target` repeatedly to choose targets explicitly. `--os` and repeatable
`--capability` filter that selection; an empty result fails instead of silently
falling back. `--repo ALIAS` on start must match the registered current checkout.
The command after `--` is an argv vector, not shell text. To use shell syntax,
explicitly request a shell, for example `-- sh -c 'make test && make report'`.
Per-target environments have clean HOME/XDG directories and do not inherit the
controller's credentials or source-host environment. Tests must install or find
their own dependencies on each selected machine.

The clean environment is execution hygiene, not an OS sandbox. Run trusted test
code on a dedicated SSH runner account or machine: the command still has that
account's operating-system permissions outside its temporary workspace.

Current tracked regular files include staged and unstaged edits; tracked files
that were deleted are omitted. Untracked files enter only through explicit
`--include PATH` (a relative file or directory). `.git`, agent state, credential
locations, common environment/secret files and unrelated ignored inputs are
excluded. Symlinks, submodules, special files, traversal and changing files fail
snapshot creation. The snapshot is bounded to 256 MiB and 10,000 files, with
per-file hashes and source HEAD provenance. Filename exclusions cannot detect
secrets embedded in arbitrary source. The source index, refs and files are never
changed by snapshotting.

`--artifact PATH` adds a relative result path. Artifacts, logs and extraction
have size/file-count bounds and never follow links or traversal entries. A
missing or invalid artifact is reported separately from the test command's
actual result. Keep result paths specific so they do not select dependency
caches or test-created credentials.

List, status and logs read local receipts only: they never contact providers,
clean resources, prompt or trigger automatic updates. Receipts are private under
`${XDG_STATE_HOME:-~/.local/state}/t/runners`. Start returns the command's failure
status; provisioning/transport failures are separate from a test failure. A
receipt records provider URLs, lifecycle and cleanup outcomes. Another terminal
can inspect or request cancellation while start is waiting. Ctrl-C requests
cancellation for started targets and returns 130. If termination cannot be
confirmed, the receipt reports unknown state rather than a false cancellation.

After a controller exits unexpectedly, `runs cleanup RUN_UUID` recovers completed
remote results while retaining active jobs. If preparation stopped before a target
was dispatched, use `t workflow runs cancel RUN_UUID` followed by
`t workflow runs cleanup RUN_UUID` to release its recorded source resources.

## Privacy, lifecycle and provider choice

GitHub source payloads are temporary private release assets, not commits in the
broker. The controller verifies private visibility, identity and protocol before
upload, dispatches a UUID-correlated workflow, and deletes the source release/tag
after confirmed completion. Hosted capacity is disposed of by GitHub. A scheduled
and manually dispatchable sweeper retries stale protocol-owned source-resource
deletion without deleting active jobs; scheduled cleanup is best effort, not a
fixed deletion deadline. An offline controller can leave resources pending until
that sweep or explicit `runs cleanup`. Local bounded logs and artifacts remain
available after remote workspace cleanup.

SSH invokes the portable helper through `zsh -lic`, outside any existing checkout.
The helper enforces the command deadline locally, uses a job-specific cancellation
token, and removes its workspace and isolated home. Cleanup removes only the
exact recorded job directory. Network failure can prevent confirmation; retry
cleanup after restoring access.

As evaluated on 2026-10-28, GitHub Actions supplies managed Linux/macOS capacity
without a separate cloud account, AWS credentials or self-hosted cloud agents.
Private repository allowances, billed runner minutes, storage and account limits
apply; startup queue time is variable. Check [GitHub runner
availability](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)
and [Actions limits](https://docs.github.com/en/actions/reference/limits) for your
account. The control plane uses documented [release
assets](https://docs.github.com/en/rest/releases/assets) and [workflow
dispatch](https://docs.github.com/en/rest/actions/workflows).

[CircleCI's execution environments](https://circleci.com/execution-environments/)
were considered. They offer managed OS environments but require another account,
project/configuration and API integration, with the same need to transport an
uncommitted snapshot. This version implements GitHub Actions and SSH only.

## Validation evidence

The implementation was exercised with one synthetic worktree containing a
modified tracked input and an explicitly included new input. GitHub-hosted
Ubuntu x64 and an existing SSH macOS arm64 machine ran the same snapshot ID,
produced matching result-artifact hashes, and returned successful command
results. The source HEAD and working-tree status stayed unchanged. GitHub
release/tag deletion and removal of the SSH job directory were verified after
retrieval. The validation credential lacked workflow-installation permission,
so the control workflows were bootstrapped through an existing authorized Git
connection before setup verified and bound the broker; normal setup requires
the permission described above.
