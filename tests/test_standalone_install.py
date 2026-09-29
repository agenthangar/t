"""The standalone installer owns only t assets and preserves local user settings."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from _install_helpers import _seed_worktree, git, run_install


@pytest.fixture
def box(tmp_path):
    checkout = tmp_path / "t"
    _seed_worktree(checkout)
    git("init", "-q", "-b", "main", cwd=checkout)
    git("config", "user.email", "t@example.invalid", cwd=checkout)
    git("config", "user.name", "T", cwd=checkout)
    git("add", "-A", cwd=checkout)
    git("commit", "-qm", "seed", "--no-verify", cwd=checkout)
    home = tmp_path / "home"
    home.mkdir()
    return checkout, home


def test_clean_install_owns_seven_links_and_prints_shell_snippet(box):
    checkout, home = box
    (home / ".zshrc").write_text("# my shell\n")
    (home / ".tmux.conf").write_text("# my tmux\n")
    result = run_install(checkout, home)
    assert result.returncode == 0, result.stderr
    assert 'source "$_t_source"' in result.stdout
    expected = {
        "bin/t": "bin/t",
        "bin/claude-stamp-tmux": "bin/claude-stamp-tmux",
        "bin/cursor-beam": "bin/cursor-beam",
        ".claude/commands/tpush.md": "claude/commands/tpush.md",
        ".claude/commands/tpop.md": "claude/commands/tpop.md",
        ".codex/prompts/tpush.md": "codex/prompts/tpush.md",
        ".codex/prompts/tpop.md": "codex/prompts/tpop.md",
    }
    for dst, src in expected.items():
        assert (home / dst).is_symlink()
        assert (home / dst).resolve() == checkout / src
    assert (home / ".zshrc").read_text() == "# my shell\n"
    assert (home / ".tmux.conf").read_text() == "# my tmux\n"
    assert (home / ".config/t/local.zsh").is_file()
    assert (home / ".claude/settings.json").is_file()
    assert not (home / ".ssh").exists()


def test_links_only_is_idempotent_and_does_not_seed_local_config(box):
    checkout, home = box
    first = run_install(checkout, home, T_LINKS_ONLY="1")
    second = run_install(checkout, home, T_LINKS_ONLY="1")
    assert first.returncode == second.returncode == 0
    assert second.stdout == ""
    assert not (home / ".config/t/local.zsh").exists()
    assert not (home / ".claude/settings.json").exists()


def test_running_worktree_installer_selects_canonical_or_dev(box):
    checkout, home = box
    dev = checkout.parent / "dev"
    git("worktree", "add", "-q", "-b", "dev/t-1", str(dev), cwd=checkout)
    normal = run_install(dev, home, T_LINKS_ONLY="1")
    assert normal.returncode == 0, normal.stderr
    assert (home / "bin/t").resolve() == checkout / "bin/t"
    selected = run_install(dev, home, T_LINKS_ONLY="1", T_LINK_DEV="1")
    assert selected.returncode == 0, selected.stderr
    assert (home / "bin/t").resolve() == dev / "bin/t"


def test_invalid_marker_refuses_before_changing_home(box):
    checkout, home = box
    (checkout / ".t-install-version").write_text("2\n")
    result = run_install(checkout, home)
    assert result.returncode != 0
    assert "capability" in result.stderr
    assert not (home / "bin").exists()


def test_existing_claude_settings_are_merged_without_losing_foreign_keys(box):
    checkout, home = box
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "mine", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}, "permissions": {"allow": ["Bash(git status)"]}}))
    result = run_install(checkout, home)
    assert result.returncode == 0, result.stderr
    data = json.loads(settings.read_text())
    assert data["model"] == "mine"
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "say done"}]}]
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "$HOME/bin/claude-stamp-tmux"
    assert data["permissions"]["allow"] == ["Bash(git status)"]  # MCP opt-out in helper
    before = settings.read_text()
    assert run_install(checkout, home).returncode == 0
    assert settings.read_text() == before


def test_live_main_hook_blocks_canonical_but_allows_worktree(box):
    checkout, home = box
    hook = checkout / ".githooks/pre-commit"
    source = Path(__file__).resolve().parent.parent / ".githooks/pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_bytes(source.read_bytes())
    hook.chmod(0o755)
    assert run_install(checkout, home, T_LINKS_ONLY="1").returncode == 0
    env = {**os.environ, "HOME": str(home)}
    blocked = subprocess.run([str(hook)], cwd=checkout, env=env, capture_output=True, text=True)
    assert blocked.returncode == 1
    assert "live t checkout" in blocked.stderr
    dev = checkout.parent / "dev"
    git("worktree", "add", "-q", "-b", "dev/t-1", str(dev), cwd=checkout)
    allowed = subprocess.run([str(hook)], cwd=dev, env=env, capture_output=True, text=True)
    assert allowed.returncode == 0
