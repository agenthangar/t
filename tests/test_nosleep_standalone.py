"""The bundled companion works without dotfiles and never starts during setup."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def isolated(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.zshrc').write_text('print unexpected-startup; exit 99\n')
    (home / '.zshenv').write_text('print unexpected-startup; exit 99\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_CACHE_HOME=str(home / '.cache'), XDG_STATE_HOME=str(home / '.local/state'),
               ZDOTDIR=str(home), NO_COLOR='1')
    return home, env


def test_clean_shell_help_and_defaults(tmp_path):
    home, env = isolated(tmp_path)
    for args, expected in [(['--help'], 'nosleep —'), (['config', '--show'], 'nosleep defaults')]:
        result = subprocess.run([str(ROOT / 'bin/nosleep'), *args], env=env,
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout
        assert 'unexpected-startup' not in result.stdout
    assert not (home / '.config').exists()


def test_standalone_existing_config_is_unchanged(tmp_path):
    home, env = isolated(tmp_path)
    config = home / '.config/nosleep/config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"grace": 42, "dim": true}\n')
    before = config.read_bytes()
    result = subprocess.run([str(ROOT / 'bin/nosleep'), 'config', '--show'], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and '42s' in result.stdout and 'Dim' in result.stdout
    assert config.read_bytes() == before


def test_shell_adapter_preserves_unexported_overrides(tmp_path):
    _, env = isolated(tmp_path)
    result = subprocess.run(['zsh', '-f', '-c', '''
NOSLEEP_NET_MIN=123 NOSLEEP_NET_BPS=9
NOSLEEP_DIM_LEVEL=0.2
typeset -A NOSLEEP_APPS=(Custom 'Custom app')
source "$1/zsh/nosleep.zsh"
print -r -- "$NOSLEEP_NET_MIN|$NOSLEEP_NET_BPS|$NOSLEEP_DIM_LEVEL|$NOSLEEP_APPS[Custom]"
nosleep --help
''', 'test', str(ROOT)], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert '123|9|0.2|Custom app' in result.stdout
    assert 'nosleep —' in result.stdout


def test_standalone_uses_selected_worktree_and_falls_back_for_old_tree(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / 'bin/nosleep'))
    selected = tmp_path / 'selected'
    selected.mkdir()
    monkeypatch.setattr(sys, 'argv', ['nosleep', '--help'])
    monkeypatch.setattr(runpy, 'run_path', lambda _: {'select_root': lambda *a, **k: selected})
    calls = []
    monkeypatch.setattr(os, 'execvp', lambda *a: calls.append(a))
    module['main']()
    assert calls[-1][1][-2:] == [str(ROOT), '--help']
    (selected / 'zsh').mkdir()
    (selected / 'zsh/nosleep.zsh').touch()
    module['main']()
    assert calls[-1][1][-2:] == [str(selected), '--help']


def test_non_macos_monitoring_never_launches(tmp_path):
    _, env = isolated(tmp_path)
    bins = tmp_path / 'bin'
    bins.mkdir()
    for name, body in [('uname', 'echo Linux'), ('sudo', 'echo unexpected-power; exit 99')]:
        target = bins / name
        target.write_text('#!/bin/sh\n' + body + '\n')
        target.chmod(0o755)
    env['PATH'] = str(bins) + os.pathsep + env['PATH']
    result = subprocess.run([str(ROOT / 'bin/nosleep')], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'macOS-only' in result.stderr
    assert 'unexpected-power' not in result.stdout
    # Flag parsing and help still work with grouped compatibility options.
    result = subprocess.run([str(ROOT / 'bin/nosleep'), '-dMLh'], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and 'SHORTCUTS' in result.stdout


def test_standalone_inventory_rejects_missing_or_mutating_display():
    lint = runpy.run_path(str(ROOT / 'scripts/lint_cli.py'))
    contract = json.loads((ROOT / 'ui/command-contract.json').read_text())
    check = lint['check_standalone_commands']
    assert check(contract) == []
    contract['standalone_commands']['nosleep']['modes']['show']['read_only'] = False
    assert any('read-only' in error for error in check(contract))
    assert check({})
