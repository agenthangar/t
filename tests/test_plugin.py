"""Standalone shell loading and reload behavior in an isolated home."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def shell(tmp_path, code, *, local_text="", plugin=ROOT, extra_env=None):
    home = tmp_path / "home"
    config = home / ".config" / "t"
    config.mkdir(parents=True, exist_ok=True)
    (config / "local.zsh").write_text(local_text)
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "PATH": os.environ["PATH"],
        **(extra_env or {}),
    }
    env.pop("T_LOCAL_RC", None)
    return subprocess.run(
        ["zsh", "-f", "-c", f'source "{plugin / "t.plugin.zsh"}"; {code}'],
        env=env, capture_output=True, text=True, timeout=20,
    )


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_clean_home_loads_plugin_and_writes_selected_config(tmp_path):
    result = shell(
        tmp_path,
        'print -r -- "root=$T_HOME local=$T_LOCAL_RC"; whence -w t; cat "$XDG_CONFIG_HOME/t/config.sh"',
        local_text='DEV_REPOS[api]="$HOME/code/api"\nDEV_MODEL[codex]="local/model"\n',
    )
    assert result.returncode == 0, result.stderr
    assert f"root={ROOT}" in result.stdout
    assert "local=" in result.stdout and "/.config/t/local.zsh" in result.stdout
    assert "t: function" in result.stdout
    assert "DEV_REPOS[api]=" in result.stdout
    assert "DEV_MODEL[codex]=local/model" in result.stdout
    assert "T_LOCAL_RC=" in result.stdout


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_login_shell_loads_the_installed_plugin_from_minimal_zshrc(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bins = home / "bin"
    bins.mkdir()
    (bins / "t").symlink_to(ROOT / "bin" / "t")
    (home / ".zshrc").write_text(
        'export PATH="$HOME/bin:$PATH"\n'
        'source "${${:-$HOME/bin/t}:A:h:h}/t.plugin.zsh"\n'
    )
    env = {**os.environ, "HOME": str(home), "ZDOTDIR": str(home)}
    env.pop("T_LOCAL_RC", None)
    result = subprocess.run(
        ["zsh", "-lic", 'print -r -- "root=$T_HOME"; whence -w t'],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert f"root={ROOT}" in result.stdout
    assert "t: function" in result.stdout


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_shortcuts_reserve_t_and_existing_commands_and_refresh(tmp_path):
    local = tmp_path / "home" / ".config" / "t" / "local.zsh"
    result = shell(
        tmp_path,
        f'''print -r -- "first=$aliases[api] reserved=$aliases[t] echo=$aliases[echo]"
        print -r -- "host=$+functions[mini] conflict=$+functions[api]"
        print -r -- 'DEV_REPOS[web]="$HOME/code/web"' >| "{local}"
        source "{ROOT / "t.plugin.zsh"}"
        print -r -- "old=$aliases[api] next=$aliases[web] host=$+functions[mini]"
        print -r -- "hooks=${{(M)#precmd_functions:#_t_reload_if_moved}}"
        ''',
        local_text='DEV_REPOS[t]="$HOME/code/t"\nDEV_REPOS[api]="$HOME/code/api"\n'
                   'DEV_REPOS[echo]="$HOME/code/echo"\nREMOTE_HOSTS[mini]=host\n'
                   'REMOTE_HOSTS[api]=host\n',
    )
    assert result.returncode == 0, result.stderr
    assert "first=cd " in result.stdout
    assert "reserved= echo=" in result.stdout
    assert "host=1 conflict=0" in result.stdout
    assert "old= next=cd " in result.stdout and "host=0" in result.stdout
    assert "hooks=1" in result.stdout


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
@pytest.mark.parametrize("release", [False, True])
def test_reloader_follows_installed_binary_source_switch(tmp_path, release):
    roots = [tmp_path / "main", tmp_path / "dev"]
    for root in roots:
        (root / "bin").mkdir(parents=True)
        shutil.copy2(ROOT / "t.plugin.zsh", root / "t.plugin.zsh")
        shutil.copytree(ROOT / "zsh", root / "zsh")
        shutil.copytree(ROOT / "ui", root / "ui")
        (root / "bin" / "t").write_text("#!/bin/sh\nexit 0\n")
        (root / "bin" / "t").chmod(0o755)
        if release:
            (root / ".t-release-version").write_text(
                "v0.2.0\n" if root == roots[0] else "v0.3.0\n"
            )
    links = tmp_path / "links"
    links.mkdir()
    (links / "t").symlink_to(roots[0] / "bin" / "t")
    code = f'''print -r -- "first=$T_HOME"
    rm "{links / "t"}"
    ln -s "{roots[1] / "bin" / "t"}" "{links / "t"}"
    _t_reload_if_moved
    print -r -- "second=$T_HOME version=$_T_LOADED_HEAD"
    _t_tree_is_live "$T_HOME"; print -r -- "protected=$?"
    '''
    result = shell(tmp_path, code, plugin=roots[0],
                   extra_env={"PATH": f"{links}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert f"first={roots[0]}" in result.stdout
    assert f"second={roots[1]}" in result.stdout
    assert "protected=0" in result.stdout
    if release:
        assert "version=v0.3.0" in result.stdout


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_live_tree_guard_protects_canonical_and_selected_code(tmp_path):
    result = shell(
        tmp_path,
        '_t_tree_is_live "$T_HOME"; echo "selected=$?"; '
        '_t_tree_is_live "$HOME"; echo "home=$?"',
    )
    assert result.returncode == 0, result.stderr
    assert "selected=0" in result.stdout
    assert "home=1" in result.stdout
