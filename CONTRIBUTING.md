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

Use the [disposable playground](docs/playground.md) to rehearse installation and
the first session in a clean Linux account. Run `python3 scripts/playground.py smoke`
for its automated flow.

## Publishing a release

After the release commit has merged and CI passes, build its runtime archive with
`python3 scripts/build-release.py vX.Y.Z --output /tmp/t-release`. The builder reads
the committed Git tree, includes the runtime and license files, and produces
`t.tar.gz` plus `SHA256SUMS`. The archive has no Git history or development checkout.

Create a draft GitHub release at that commit and attach both files before publishing
it. This keeps the latest-release installer from selecting a release whose assets
are still being built. The release workflow rebuilds and checks the published
assets; it does not replace an existing archive with different bytes.

Smoke-test the public installer in an isolated HOME, including `t --version`,
loading `t.plugin.zsh`, and a repeated `t update`. Use the existing release-installer
tests for failed downloads, corrupt archives, rollback, and custom install paths.
