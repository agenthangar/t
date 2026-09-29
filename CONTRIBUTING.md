# Contributing

Bug reports and focused improvements are welcome. Include your OS, zsh/Python/tmux
versions, `t doctor` output with private paths removed, and a minimal reproduction.
Discuss larger changes in an issue before building them.

By opening a pull request you accept the [Contributor License Agreement](CLA.md)
and license your contribution under [MIT](LICENSE).

Develop in a worktree or feature branch; canonical main may be a live installation.
Install test dependencies with `python3 -m pip install -r requirements-dev.txt` and
run `python3 -m pytest --cov --cov-fail-under=97`. Lint Bash with shellcheck and
validate every changed zsh file with `zsh -n`. Keep new behavior covered by meaningful
sandbox tests. No runtime pip dependencies are installed by install.sh.

Never include private paths, hosts, credentials, or real agent transcripts in issues,
fixtures, or demo recordings. Use generic, disposable examples.
