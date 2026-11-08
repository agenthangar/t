"""Standalone shell loading and reload behavior in an isolated home."""

import os
import json
from pathlib import Path
import shutil
import shlex
import time
import subprocess
import uuid

import pytest

from native_agent_fixture import write_native_agent


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
        'T_DOTFILES_LIVE_TREE=$T_HOME; _t_tree_is_live "$T_HOME"; echo "selected=$?"; '
        '_t_tree_is_live "$HOME"; echo "home=$?"',
    )
    assert result.returncode == 0, result.stderr
    assert "selected=0" in result.stdout
    assert "home=1" in result.stdout


def test_worktree_creation_failure_still_explains_repair(tmp_path):
    result = shell(tmp_path, '_dev_worktree_refuse api 1',
                   local_text='DEV_REPOS[api]="$HOME/api"\n')
    assert "could not create the worktree" in result.stderr
    assert "no fetched origin default branch" in result.stderr
    assert "remote set-head origin -a" in result.stderr
    assert "t app pull" not in result.stderr


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("zsh"),
                    reason="tmux and zsh are required")
@pytest.mark.parametrize("launch", ["new", "resume"])
@pytest.mark.parametrize("repo", ["api", "agenthangar.github.io"])
def test_repo_launch_and_reattach_on_isolated_tmux(tmp_path, launch, repo):
    """Exercise tmux parsing, agent launch, logging, and live-slot ownership."""
    socket = "t-dotted-" + uuid.uuid4().hex
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    home = tmp_path / "home"
    env.update(HOME=str(home), ZDOTDIR=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local" / "state"))
    env.pop("TMUX", None)
    command = [shutil.which("tmux"), "-L", socket, "-f", os.devnull]

    def tmux(*args):
        return subprocess.run(command + list(args), env=env, capture_output=True,
                              text=True, timeout=5)

    session = f"dev-{repo}-4"
    target = "=" + session + ":"
    marker = tmp_path / "started"
    bins = home / 'bin'
    bins.mkdir(parents=True)
    (bins / 't').symlink_to(ROOT / 'bin/t')
    env['PATH'] = str(bins) + os.pathsep + env['PATH']
    env['CODEX_HOME'] = str(home / '.codex')
    sid = '12345678-1234-1234-1234-123456789abc'
    locks = Path(env['CODEX_HOME']) / 'thread-writer-locks'
    locks.mkdir(parents=True)
    rollout = Path(env['CODEX_HOME']) / 'sessions/2026/11/07' / ('rollout-2026-11-07T00-00-00-' + sid + '.jsonl')
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid, cwd=str(home / 'code' / repo), cli_version='0.160.0'))) + '\n')
    write_native_agent(bins / 'codex', f'''
import fcntl, os, pathlib, sys, time
if '--help' in sys.argv:
    print('--no-daemon')
    sys.exit(0)
if 'resume' in sys.argv:
    writer = open(pathlib.Path(os.environ['CODEX_HOME']) / 'thread-writer-locks' / (sys.argv[2] + '.lock'), 'w')
    fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
pathlib.Path({str(marker)!r}).write_text('launched')
print('agent output', flush=True)
time.sleep(60)
''')
    wrapper = f'''tmux() {{
      if [[ $1 == attach-session ]]; then
        shift
        command {shlex.join(command)} has-session "$@"
        return
      fi
      command {shlex.join(command)} "$@"
    }}
    _dev_agent_check() {{ return 0; }}
    _dev_repo_prepare() {{ :; }}
    _dev_recovery_watch() {{ :; }}
    '''
    launch_code = (f'_dev_slot_fresh() {{ [[ $2 == 4 ]]; }}; t open {repo} --new --codex' if launch == "new"
                   else f'_dev_resume_session {session} "$HOME/code/{repo}" {sid} codex')
    local = (f'DEV_REPOS[{repo}]="$HOME/code/{repo}"\n'
             f'DEV_WORKTREE[{repo}]=0\n')
    home.mkdir(parents=True, exist_ok=True)
    (home / ".zshenv").write_text("skip_global_compinit=1\n")
    (home / '.zshrc').write_text(
        f'export PATH={shlex.quote(env["PATH"])}\nsource {shlex.quote(str(ROOT / "t.plugin.zsh"))}\n'
        '_dev_repo_prepare() { :; }\n')
    try:
        if "." in repo:
            # Older tmux replaces dots with underscores at creation, so it
            # cannot exercise the literal dotted-name parsing regression.
            home.mkdir(parents=True, exist_ok=True)
            probe = tmux("new-session", "-d", "-s", session, "sleep", "60")
            assert probe.returncode == 0, probe.stderr
            actual = tmux("list-sessions", "-F", "#{session_name}").stdout.strip()
            tmux("kill-server")
            if actual != session:
                pytest.skip("this tmux rewrites dots in session names")
        result = shell(tmp_path, wrapper + f'mkdir -p "$HOME/code/{repo}"; ' + launch_code,
                       local_text=local, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "can't find pane" not in result.stderr
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert marker.exists(), result.stderr
        assert tmux("show-environment", "-t", "=" + session, "DEV_AGENT").stdout.strip() == "DEV_AGENT=codex"
        assert tmux("display-message", "-p", "-t", target, "#{pane_pipe}").stdout.strip() == "1"
        assert tmux("show-options", "-w", "-v", "-t", target, "window-size").stdout.strip() == "latest"
        assert tmux("pipe-pane", "-t", target).returncode == 0
        result = shell(tmp_path, wrapper + f'''t open {repo} 4
                       _dev_slot_fresh {repo} 4; print -r -- fresh=$?
                       _dev_local_slot_live {repo} 4; print -r -- live=$?
                       ''',
                       local_text=local, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "Reattaching " + session in result.stdout
        assert "fresh=1" in result.stdout
        assert "live=0" in result.stdout
        assert "can't find pane" not in result.stderr
        assert tmux("display-message", "-p", "-t", target, "#{pane_pipe}").stdout.strip() == "1"
    finally:
        tmux("kill-server")


def test_open_rejects_removed_app_flag(tmp_path):
    result = shell(tmp_path, '_t_open api --app --cli')
    assert result.returncode == 2
    assert "support has been removed" in result.stderr


def test_legacy_reservation_protects_worktree_from_freshen_and_sweep(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("# disposable\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "start"], check=True)
    wt = tmp_path / "home" / "worktrees" / "repo" / "1"
    wt.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "dev/repo-1",
                    str(wt), "main"], check=True)
    gitdir = subprocess.check_output(['git', '-C', str(wt), 'rev-parse', '--absolute-git-dir'], text=True).strip()
    (Path(gitdir) / 't-app-slot').write_text('codex-app\n')
    result = shell(
        tmp_path,
        f'''tmux() {{ return 1; }}
        _dev_legacy_workspace_reserved "{wt}"; print -r -- "reserved=$?"
        _dev_branch_merged() {{ print -u2 UNEXPECTED_FRESHEN; return 0; }}
        _dev_worktree_freshen api 1 "{wt}" dev/repo-1
        _dev_worktree_create api 1; print -r -- "create=$?"
        _dev_worktree_sweep_run
        [[ -e "{wt}/.git" ]]; print -r -- "kept=$?"
        ''',
        local_text=f'DEV_REPOS[api]="{repo}"\nDEV_WORKTREE_ROOT="$HOME/worktrees"\n',
        extra_env={"TMUX_TMPDIR": str(tmp_path / "tmux"),
                   "T_NO_AUTORELOAD": "1", "T_NO_UPDATE_CHECK": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "reserved=0" in result.stdout
    assert "create=0" in result.stdout
    assert "kept=0" in result.stdout
    assert 'UNEXPECTED_FRESHEN' not in result.stderr
