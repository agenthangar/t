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


def test_install_migrates_mcp_blanket_to_read_tools(box, tmp_path):
    checkout, home = box
    settings = home / '.claude/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'permissions': {'allow': ['Bash(ls)', 'mcp__sessions'],
                                                    'ask': ['Bash(rm *)']}}))
    bins = tmp_path / 'fake-bin'
    bins.mkdir()
    claude = bins / 'claude'
    claude.write_text('#!/bin/sh\nexit 0\n')
    claude.chmod(0o755)
    env = dict(T_NO_MCP='', PATH=f'{bins}:{os.environ["PATH"]}')
    outcome = run_install(checkout, home, **env)
    assert outcome.returncode == 0, outcome.stderr
    perms = json.loads(settings.read_text())['permissions']
    assert perms['allow'] == ['Bash(ls)', 'mcp__sessions__find_sessions',
                             'mcp__sessions__list_sessions', 'mcp__sessions__session_detail']
    assert perms['ask'] == ['Bash(rm *)']
    before = settings.read_bytes()
    assert run_install(checkout, home, **env).returncode == 0
    assert settings.read_bytes() == before


def test_clean_install_owns_eight_links_and_preserves_shell_content(box):
    checkout, home = box
    (home / ".zshrc").write_text("# my shell\n")
    (home / ".tmux.conf").write_text("# my tmux\n")
    result = run_install(checkout, home)
    assert result.returncode == 0, result.stderr
    assert "  source " in result.stdout
    expected = {
        "bin/t": "bin/t",
        "bin/t-session-start": "bin/t-session-start",
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
    assert (home / ".zshrc").read_text().startswith("# my shell\n# >>> t shell integration >>>")
    assert (home / ".tmux.conf").read_text() == "# my tmux\n"
    assert (home / ".config/t/local.zsh").is_file()
    assert (home / ".claude/settings.json").is_file()
    assert not (home / ".ssh").exists()


@pytest.mark.parametrize("codex_hooks", [False, True])
def test_links_only_is_idempotent_and_does_not_seed_local_config(box, codex_hooks):
    checkout, home = box
    # Exercise both paths on CI regardless of whether its host has Codex installed.
    if codex_hooks:
        (home / ".codex").mkdir()
        (home / ".codex/config.toml").write_text('model = "gpt-6"\n')
    options = {"T_LINKS_ONLY": "1", "T_NO_CODEX_HOOKS": "" if codex_hooks else "1"}
    first = run_install(checkout, home, **options)
    hooks = home / ".codex/hooks.json"
    before = hooks.read_bytes() if codex_hooks else None
    second = run_install(checkout, home, **options)
    assert first.returncode == second.returncode == 0
    if codex_hooks:
        assert "Review hooks" in second.stdout and "Allow selected" in second.stdout
        assert "Registered the SessionStart hook" not in second.stdout
        assert hooks.read_bytes() == before
    else:
        assert second.stdout == ""
        assert not hooks.exists()
    assert not (home / ".config/t/local.zsh").exists()
    assert not (home / ".claude/settings.json").exists()
    assert git("config", "--local", "--get", "core.hooksPath", cwd=checkout, check=False).returncode != 0


def test_full_install_enables_local_hook_without_replacing_custom_path(box):
    checkout, home = box
    result = run_install(checkout, home)
    assert result.returncode == 0, result.stderr
    assert git("config", "--local", "--get", "core.hooksPath", cwd=checkout).stdout.strip() == ".githooks"
    git("config", "--local", "core.hooksPath", "/tmp/other-hooks", cwd=checkout)
    assert run_install(checkout, home).returncode == 0
    assert git("config", "--local", "--get", "core.hooksPath", cwd=checkout).stdout.strip() == "/tmp/other-hooks"


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


def _brew_tree(tmp_path, version, opt):
    source = tmp_path / "Cellar/t" / version / "libexec"
    _seed_worktree(source)
    (source / ".t-release-version").write_text("v" + version + "\n")
    brew = tmp_path / "bin/brew"
    brew.parent.mkdir(parents=True, exist_ok=True)
    brew.write_text("#!/bin/sh\nexit 0\n")
    brew.chmod(0o755)
    (source / ".t-homebrew").write_text(json.dumps({
        "formula": "agenthangar/tap/t", "opt_libexec": str(opt), "brew": str(brew),
    }))
    return source


def test_homebrew_links_use_stable_opt_path_across_cellar_upgrade(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    opt = tmp_path / "opt/t/libexec"
    opt.parent.parent.mkdir(parents=True)
    first = _brew_tree(tmp_path, "0.3.0", opt)
    opt.parent.symlink_to(first.parent, target_is_directory=True)
    result = run_install(first, home, T_LINKS_ONLY="1", T_LINK_DEV="1")
    assert result.returncode == 0, result.stderr
    links = {
        "bin/t": "bin/t", "bin/t-session-start": "bin/t-session-start",
        "bin/claude-stamp-tmux": "bin/claude-stamp-tmux",
        "bin/cursor-beam": "bin/cursor-beam",
        ".claude/commands/tpush.md": "claude/commands/tpush.md",
        ".claude/commands/tpop.md": "claude/commands/tpop.md",
        ".codex/prompts/tpush.md": "codex/prompts/tpush.md",
        ".codex/prompts/tpop.md": "codex/prompts/tpop.md",
    }
    for destination, asset in links.items():
        assert os.readlink(home / destination) == str(opt / asset)
        assert (home / destination).resolve() == first / asset

    second = _brew_tree(tmp_path, "0.3.1", opt)
    opt.parent.unlink()
    opt.parent.symlink_to(second.parent, target_is_directory=True)
    assert run_install(second, home, T_LINKS_ONLY="1").returncode == 0
    for destination, asset in links.items():
        assert os.readlink(home / destination) == str(opt / asset)
        assert (home / destination).resolve() == second / asset


def test_homebrew_marker_rejects_wrong_opt_before_touching_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    opt = tmp_path / "opt/t/libexec"
    opt.parent.parent.mkdir(parents=True)
    source = _brew_tree(tmp_path, "0.3.0", opt)
    opt.parent.symlink_to(source.parent, target_is_directory=True)
    opt.parent.unlink()
    rejected = run_install(source, home, T_LINKS_ONLY="1", T_LINK_DEV="1")
    assert rejected.returncode != 0 and "opt path" in rejected.stderr
    assert not (home / "bin").exists()
    marker = source / ".t-homebrew"
    marker.unlink()
    marker.symlink_to(source / "missing-marker")
    rejected = run_install(source, home, T_LINKS_ONLY="1")
    assert rejected.returncode != 0 and "regular file" in rejected.stderr
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
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "$HOME/bin/t-session-start"
    assert data["permissions"]["allow"] == ["Bash(git status)"]  # MCP opt-out in helper
    before = settings.read_text()
    assert run_install(checkout, home).returncode == 0
    assert settings.read_text() == before


def test_explicit_policy_runs_after_fresh_settings_seed(box):
    checkout, home = box
    policy = home / "policy"
    policy.mkdir()
    (policy / "permissions.allow").write_text("Bash(git status)\n")
    probe = home / "policy-probe.txt"
    binary = checkout / "bin/t"
    binary.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
settings = Path(os.environ['HOME']) / '.claude/settings.json'
Path(os.environ['T_POLICY_PROBE']).write_text('present' if settings.is_file() else 'missing')
""")
    binary.chmod(0o755)
    result = run_install(checkout, home, T_PERMISSIONS_DIR=str(policy),
                         T_NO_PERMISSIONS="", T_POLICY_PROBE=str(probe))
    assert result.returncode == 0, result.stderr
    assert probe.read_text() == "present"


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


@pytest.mark.parametrize("absolute", [False, True])
def test_install_migrates_standard_hook_names_preserving_options_and_trust(box, absolute):
    checkout, home = box
    prefix = str(home) if absolute else "$HOME"
    old = prefix + "/bin/claude-stamp-tmux"
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[hooks.state."legacy"]\ntrusted_hash = "sha256:unchanged"\n')
    trust = config.read_bytes()
    paths = [(home / ".claude/settings.json", ""), (home / ".codex/hooks.json", " --agent codex")]
    expected = []
    for path, flags in paths:
        path.parent.mkdir(exist_ok=True)
        custom = {"type": "command", "command": "/opt/bin/claude-stamp-tmux" + flags + " --quiet"}
        hook = {"type": "command", "command": old + flags, "timeout": 12, "statusMessage": "Session tracking"}
        data = {"description": "keep me", "hooks": {
            "SessionStart": [{"matcher": "startup|resume", "hooks": [hook, custom]}],
            "Stop": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}
        path.write_text(json.dumps(data))
        hook["command"] = "$HOME/bin/t-session-start" + flags
        expected.append(data)
    result = run_install(checkout, home, T_LINKS_ONLY="1")
    assert result.returncode == 0, result.stderr
    assert "Updated the SessionStart hook" in result.stdout
    for (path, _), data in zip(paths, expected):
        assert json.loads(path.read_text()) == data
    assert config.read_bytes() == trust
    before = [path.read_bytes() for path, _ in paths]
    assert run_install(checkout, home, T_LINKS_ONLY="1").returncode == 0
    assert [path.read_bytes() for path, _ in paths] == before
    assert config.read_bytes() == trust


def test_install_refreshes_only_explicitly_configured_agent_workflow(box):
    checkout, home = box
    native = home / '.codex/AGENTS.md'
    assert run_install(checkout, home).returncode == 0
    assert '<!-- t agent-workflow: begin -->' not in native.read_text()
    assert not (home / '.config/t/workflow.json').exists()
    policy = {'enabled': True, 'planning': {'model': 'chosen-planner', 'effort': 'high'},
              'implementation': {'model': 'chosen-coder', 'effort': 'medium'},
              'review': {'model': 'chosen-reviewer', 'effort': 'one-level-lower'}}
    saved = home / '.config/t/workflow.json'
    saved.write_text(json.dumps({'version': 1, 'global': policy, 'repositories': {}}))
    original = native.read_text()
    result = run_install(checkout, home)
    assert result.returncode == 0, result.stderr
    assert native.read_text().startswith(original)
    assert 'chosen-planner' in native.read_text()
    assert 'chosen-coder' in (home / '.claude/CLAUDE.md').read_text()
    assert native.read_text().count('<!-- t public-issues: begin -->') == 1
    assert native.read_text().count('<!-- t agent-workflow: begin -->') == 1
    assert not (checkout / 'AGENTS.md').exists()
