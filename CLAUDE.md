# t architecture

Follow AGENTS.md for worktree development, PR auto-merge, and validation.

`bin/t` owns argument parsing, configuration parsing, subprocess-safe commands,
agent installation/settings, diagnostics, MCP, and desktop handoff. `t.plugin.zsh`
sources ordered modules under zsh/. Shell-bound commands run in the caller so cd,
foreground resume, and push/pop retain their original semantics. The executable
must keep its name; tests import it with SourceFileLoader.

## Contracts

- T_LOCAL_RC is the private source of truth; the plugin generates config.sh for
  Python and cursor-beam. Never execute that data bridge in Python.
- Remote helpers run through `zsh -lic`; each host loads the plugin from its own rc.
- t.plugin.zsh follows the installed bin source on reload. Canonical main and active
  dev source are separate; t system update resolves canonical through git common-dir.
- One live owner per conversation. Transfer transcript files and origin stamps;
  do not copy agent credential stores or a live Codex SQLite database.
- Worktree creation failure aborts rather than falling back to a shared tree.
  Cleanup protects live sources, dirty worktrees, and shared agent infrastructure.
- .t-install-version is the dotfiles integration capability marker. Changing this
  contract needs an explicit compatible migration in the consuming repository.
- Standalone defaults do not import personal policy. T_PERMISSIONS_DIR selects
  policy; T_PERMISSION_DEFAULTS and T_AUTO_TRUST are separate explicit opt-ins.
- Existing hooks/settings are per-machine data; add/merge only the entries t owns.

## Tests

Run the pytest/coverage gate (97%), shellcheck on Bash and sh files, and zsh syntax
checks. Fixtures use a temporary HOME/XDG_CONFIG_HOME and isolated tmux sockets.
Never source an install test's tmux config into a developer's real server. Strip
COV_CORE_* for copied measured binaries, or coverage counts them as new modules.

The release demo's raw capture and recipe live in scripts/demo/ and docs/media/;
only make claims supported by the recorded behavior. Desktop handoff helpers are
macOS-specific and need capability checks; normal t sessions also work on Linux.
