"""Disposable Git checkouts and homes for standalone installer integration tests."""

import os
from pathlib import Path
import shutil
import subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
ASSETS = (
    ".t-install-version", "install.sh", "t.plugin.zsh", "local.zsh.example",
    "bin/t", "bin/claude-stamp-tmux", "bin/cursor-beam",
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
