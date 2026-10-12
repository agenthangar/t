# Agent instructions

`t` is a Python CLI plus a zsh plugin for agent sessions and isolated Git worktrees.

Before changing a public command, read [the command and terminal contract](ui/README.md)
and [the command inventory](ui/command-contract.json). Classify every new command
and conditional mode; compatibility aliases inherit the canonical entry. Use
the shared `_RailUI` or `_t_fzf_single`/`_t_fzf_multi`/`_t_fzf_filtered`/`_t_fzf_paginated`
profile for interactive behavior. Update the inventory, help, examples, and
contract tests with the implementation. Lists and status are read-only and
must never open a picker or an automatic update prompt. Run
`python3 scripts/lint_cli.py` to check these rules.

Keep public commands in noun/verb families. For issue work, expose `t issue list`
to browse a repository, `t issue status` for read-only ownership discovery, and
`t issue start` to continue or start work. Reuse the shared session picker for
interactive issue selection; allow multiple independent issues to be selected,
but never start a second live owner for one issue. Do not add a bare `t issue`
target form or a new public single-word command except the reviewed `t share`
context handoff command. Run `python3 scripts/lint_cli.py`
after CLI changes; CI enforces this command contract.
Command-group help must show repository argument positions and explain inference
and selection. Keep `--repo` consistent across issue verbs. `t issue list` is a
read-only display like `t session list`: scope to an explicit or current repo,
or show all registered GitHub repositories outside a checkout; `-a`/`--all`
widens the view from inside a checkout. Keep `--state all` independent of repo
scope. List must never open a picker. JSON, remote scanners, and non-interactive
commands must never prompt for a missing repository.

Before implementing any new enhancement or fix in a public repository, verify
the repository's visibility and find a matching open issue on its host or create one.
Record the issue URL before editing code, reuse it for the task, and link the PR
to it. Do not create duplicate issues or publish private context. If visibility
or issue creation cannot be verified, resolve that before implementation.

Work in a per-session worktree on `dev/t-<slot>` or a `cursor/*` feature branch.
The canonical checkout is the live release on `main`; never develop or commit there.

When ready, commit, push, and open a PR to `main`. Immediately enable squash
auto-merge with `gh pr merge <number> --auto --squash`. Verify state,
autoMergeRequest, mergeable, and mergeStateStatus; resolve conflicts and monitor CI
through merge. After every merge to main, run `t system update --local .` from this
repo to install canonical main locally, even when the previous install used
Homebrew or a release. If the installed command predates this syntax, bootstrap
with `./bin/t system update --local .`. Verify the installed `t --version` matches
canonical main. Do not leave a ready PR for manual merge.

Run `python3 -m pytest --cov --cov-fail-under=97`, Bash shellcheck, and `zsh -n`
for changed shell files. Compile bin/t to `/tmp`, not the checkout. Preserve coverage.
Use sandbox HOME/XDG directories and isolated tmux sockets in tests; never let an
installer test mutate the developer's agent configuration or tmux server. Strip
COV_CORE_* when launching copies of measured binaries.

Native macOS UI tests open windows and steal focus. Keep them disabled in routine
local runs; use `--run-macos-ui` only on disposable CI or when the user explicitly
requests native desktop verification. Mark any new interactive desktop tests
`macos_ui` so they share this opt-in gate.
Keep macOS CI enabled by default. Apply the `ci:skip-macos` label to a PR only
when the user explicitly requests skipping macOS CI for that PR; remove the label
when that exception no longer applies. The Linux and other CI jobs still run.

Keep the one-live-owner guarantee, remote `zsh -lic` contract, and worktree safety
checks. No plain local declarations of zsh tied parameters such as path/fpath.
The package must work without dotfiles, personal policy, iCloud, or launchd.
Never commit private config, transcripts, auth state, or recorded personal sessions.
