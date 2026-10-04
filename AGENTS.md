# Agent instructions

`t` is a Python CLI plus a zsh plugin for agent sessions and isolated Git worktrees.

Work in a per-session worktree on `dev/t-<slot>` or a `cursor/*` feature branch.
The canonical checkout is the live release on `main`; never develop or commit there.

When ready, commit, push, and open a PR to `main`. Immediately enable squash
auto-merge with `gh pr merge <number> --auto --squash`. Verify state,
autoMergeRequest, mergeable, and mergeStateStatus; resolve conflicts and monitor CI
through merge. After every merge to main, run `t update --local .` from this repo
to install canonical main locally, even when the previous install used Homebrew
or a release. If the installed command predates `--local`, bootstrap with
`./bin/t update --local .`. Verify the installed `t --version` matches canonical
main. Do not leave a ready PR for manual merge.

Run `python3 -m pytest --cov --cov-fail-under=97`, Bash shellcheck, and `zsh -n`
for changed shell files. Compile bin/t to `/tmp`, not the checkout. Preserve coverage.
Use sandbox HOME/XDG directories and isolated tmux sockets in tests; never let an
installer test mutate the developer's agent configuration or tmux server. Strip
COV_CORE_* when launching copies of measured binaries.

Keep the one-live-owner guarantee, remote `zsh -lic` contract, and worktree safety
checks. No plain local declarations of zsh tied parameters such as path/fpath.
The package must work without dotfiles, personal policy, iCloud, or launchd.
Never commit private config, transcripts, auth state, or recorded personal sessions.
