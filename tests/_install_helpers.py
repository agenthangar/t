"""Disposable Git checkouts and homes for standalone installer integration tests."""

import os
from pathlib import Path
import shutil
import subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
ASSETS = (
    ".t-install-version", "install.sh", "t.plugin.zsh", "local.zsh.example",
    "bin/t", "bin/t-session-start", "bin/claude-stamp-tmux", "bin/cursor-beam",
    "libexec/t_progress.py", "libexec/t_shell_setup.py", "libexec/t_onboarding.py", "libexec/t_discovery.py",
    "libexec/t_toml.py", "libexec/t_codex_socket.py", "libexec/vendor/SHA256SUMS", "libexec/vendor/tomli-LICENSE", "libexec/vendor/tomli/__init__.py", "libexec/vendor/tomli/_parser.py", "libexec/vendor/tomli/_re.py", "libexec/vendor/tomli/_types.py", "libexec/vendor/tomli/py.typed",
    "libexec/t_mcp_setup.py", "libexec/t_mcp_controls.py", "libexec/t_workflow.py", "libexec/t_workflow_sync.py", "libexec/t_issue.py", "libexec/t_issue_policy.txt", "libexec/t_resume_source.py", "libexec/t_history.py", "libexec/t_candidates.py", "libexec/t_session_details.py", "libexec/t_picker_filters.py", "libexec/t_repos.py",
    "libexec/t_local_worker.py", "libexec/t_local_setup.py",
    "libexec/t_runners.py", "libexec/t_runner_job.py", "share/runners/broker.yml", "share/runners/sweeper.yml",
    "libexec/t_share.py", "libexec/t_share_sessions.py",
    "libexec/t_share_context.py", "libexec/t_share_flow.py", "libexec/t_share_launch.py", "libexec/t_share_ui.py",
    "zsh/share.zsh",
    "claude/commands/tpush.md", "claude/commands/tpop.md",
    "claude/settings.json.example",
    "codex/prompts/tpush.md", "codex/prompts/tpop.md",
)


def git(*args, cwd, check=True, **kwargs):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True, **kwargs,
    )


def _seed_worktree(path):
    """Copy the standalone install surface into a throwaway checkout."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        src, dst = REPO_ROOT / name, path / name
        if not src.exists():
            raise FileNotFoundError(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def run_install(cwd, home, **extra_env):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE_", "T_"))},
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "XDG_CONFIG_HOME": str(Path(home) / ".config"),
        "XDG_DATA_HOME": str(Path(home) / ".local" / "share"),
        "T_NO_MCP": "1",  # Never run the developer's actual Claude CLI.
        "T_NO_PERMISSIONS": "1",
        "T_NO_TRUST": "1",
        "TMUX": "",
    }
    env.update(extra_env)
    return subprocess.run(
        ["./install.sh"], cwd=str(cwd), env=env,
        capture_output=True, text=True,
    )
