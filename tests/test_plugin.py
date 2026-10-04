"""Standalone shell loading and reload behavior in an isolated home."""

import os
from pathlib import Path
import shutil
import shlex
import time
import subprocess
import uuid

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


@pytest.mark.skipif(not shutil.which("zsh") or os.uname().sysname != "Darwin",
                    reason="Codex desktop launch requires macOS and zsh")
@pytest.mark.parametrize("reopen", [False, True])
def test_open_app_creates_worktree_without_starting_tmux_or_cli(tmp_path, reopen):
    bins = tmp_path / "stubbin"
    bins.mkdir()
    codex = bins / "codex"
    codex.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CODEX_LOG"\n')
    codex.chmod(0o755)
    log = tmp_path / "codex.log"
    repo = tmp_path / "home" / "code" / "api"
    if reopen:
        metadata = tmp_path / "home" / "worktrees" / "api" / "1" / ".git"
        metadata.mkdir(parents=True)
        (metadata / "t-app-slot").write_text("codex-app\n")
    result = shell(
        tmp_path,
        f'''mkdir -p {repo}
        _dev_slot_fresh() {{ [[ $2 == 1 ]] }}
        _dev_worktree_create() {{ local wt="$HOME/worktrees/api/$2"; mkdir -p "$wt/.git"; print -r -- "$wt"; }}
        _dev_app_slot_marker() {{ print -r -- "$1/.git/t-app-slot"; }}
        {'_dev_worktree_create() { print -u2 -- "unexpected worktree creation"; return 1; };' if reopen else ''}
        t open api {'1' if reopen else '--app'}
        ''',
        local_text='DEV_REPOS[api]="$HOME/code/api"\nDEV_WORKTREE_ROOT="$HOME/worktrees"\nDEV_OPEN_MODE_DEFAULT=cli\n',
        extra_env={"PATH": f"{bins}:{os.environ['PATH']}", "CODEX_LOG": str(log),
                   "TMUX_TMPDIR": str(tmp_path / "tmux")},
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == f"app {tmp_path}/home/worktrees/api/1"
    assert (tmp_path / "home" / "worktrees" / "api" / "1" / ".git" / "t-app-slot").read_text().strip() == "codex-app"
    assert "Open request sent" in result.stdout
    assert "unexpected worktree creation" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_open_app_rejects_conflicting_flags_before_worktree_changes(tmp_path):
    for flags in ("--claude", "--remote", "--fg", "--host mini", "3 --new"):
        result = shell(tmp_path / flags.replace(" ", "_"),
                       f't open api --app {flags}',
                       local_text='DEV_REPOS[api]="$HOME/code/api"\n')
        assert result.returncode != 0, (flags, result.stdout, result.stderr)
        assert "t open --app:" in result.stderr


@pytest.mark.skipif(not shutil.which("zsh") or os.uname().sysname != "Darwin",
                    reason="Codex desktop handoff requires macOS and zsh")
def test_open_app_hands_live_codex_slot_to_existing_thread(tmp_path):
    bins = tmp_path / "stubbin"
    bins.mkdir()
    for name, body in {
        "codex": "#!/bin/sh\nexit 0\n",
        "tmux": '#!/bin/sh\n[ "$1" = has-session ]\n',
        "t": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$APP_LOG"\n',
    }.items():
        stub = bins / name
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "app.log"
    repo = tmp_path / "home" / "code" / "api"
    result = shell(
        tmp_path,
        f'''mkdir -p {repo}
        _dev_agent_of_session() {{ print -r -- codex; }}
        _dev_worktree_create() {{ print -u2 -- 'unexpected worktree creation'; return 1; }}
        t open api 3 --app
        ''',
        local_text='DEV_REPOS[api]="$HOME/code/api"\nDEV_WORKTREE_ROOT="$HOME/worktrees"\n',
        extra_env={"PATH": f"{bins}:{os.environ['PATH']}", "APP_LOG": str(log)},
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == "app api 3"
    assert "unexpected worktree creation" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh") or os.uname().sysname != "Darwin",
                    reason="Codex desktop launch requires macOS and zsh")
def test_open_app_refuses_a_slot_owned_by_remote_host(tmp_path):
    bins = tmp_path / "stubbin"
    bins.mkdir()
    codex = bins / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    repo = tmp_path / "home" / "code" / "api"
    result = shell(
        tmp_path,
        f'''mkdir -p {repo}
        _dev_remote_resolve() {{ print -r -- $'mini\\tapi\\t3'; }}
        _dev_worktree_create() {{ print -u2 -- 'unexpected worktree creation'; return 1; }}
        t open api 3 --app
        ''',
        local_text='DEV_REPOS[api]="$HOME/code/api"\nREMOTE_HOSTS[mini]=unused\n',
        extra_env={"PATH": f"{bins}:{os.environ['PATH']}",
                   "TMUX_TMPDIR": str(tmp_path / "tmux")},
    )
    assert result.returncode != 0
    assert "is live on mini" in result.stderr
    assert "unexpected worktree creation" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_app_reservation_protects_worktree_from_cli_reuse_and_sweep(tmp_path):
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
    result = shell(
        tmp_path,
        f'''_dev_app_slot_reserve "{wt}" || return 1
        _dev_app_slot_reserved "{wt}"; print -r -- "reserved=$?"
        _dev_worktree_create api 1; print -r -- "create=$?"
        _dev_slot_fresh api 1; print -r -- "fresh=$?"
        _dev_branch_merged() {{ return 0; }}
        _dev_worktree_sweep_run
        [[ -e "{wt}/.git" ]]; print -r -- "kept=$?"
        _dev_session_rows
        ''',
        local_text=f'DEV_REPOS[api]="{repo}"\nDEV_WORKTREE_ROOT="$HOME/worktrees"\n',
        extra_env={"TMUX_TMPDIR": str(tmp_path / "tmux")},
    )
    assert result.returncode == 0, result.stderr
    assert "reserved=0" in result.stdout
    assert "create=1" in result.stdout
    assert "fresh=1" in result.stdout
    assert "kept=0" in result.stdout
    assert f"-\t{wt}\tapi-1\tapp\tnone\t(Codex desktop workspace" in result.stdout


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("zsh"),
                    reason="tmux and zsh are required")
@pytest.mark.parametrize("launch", ["new", "resume"])
@pytest.mark.parametrize("repo", ["api", "agenthangar.github.io"])
def test_repo_launch_and_reattach_on_isolated_tmux(tmp_path, launch, repo):
    """Exercise tmux parsing, agent launch, logging, and live-slot ownership."""
    socket = "t-dotted-" + uuid.uuid4().hex
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    home = tmp_path / "home"
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local" / "state"))
    env.pop("TMUX", None)
    command = [shutil.which("tmux"), "-L", socket, "-f", os.devnull]

    def tmux(*args):
        return subprocess.run(command + list(args), env=env, capture_output=True,
                              text=True, timeout=5)

    session = f"dev-{repo}-4"
    target = "=" + session + ":"
    marker = tmp_path / "started"
    launch_command = f"printf launched > {shlex.quote(str(marker))}; printf 'agent output\\n'; sleep 60"
    wrapper = f'''tmux() {{
      if [[ $1 == attach-session ]]; then
        shift
        command {shlex.join(command)} has-session "$@"
        return
      fi
      if [[ $1 == new-session ]]; then
        command {shlex.join(command)} "$@" 'exec zsh -f'
      else
        command {shlex.join(command)} "$@"
      fi
    }}
    _dev_agent_check() {{ return 0; }}
    _dev_agent_new_cmd() {{ print -r -- {shlex.quote(launch_command)}; }}
    _dev_agent_resume_cmd() {{ print -r -- {shlex.quote(launch_command)}; }}
    _dev_repo_prepare() {{ :; }}
    _dev_recovery_watch() {{ :; }}
    '''
    launch_code = (f'_dev_slot_fresh() {{ [[ $2 == 4 ]]; }}; t open {repo} --new --codex' if launch == "new"
                   else f'_dev_resume_session {session} "$HOME/code/{repo}" thread-1 codex')
    local = (f'DEV_REPOS[{repo}]="$HOME/code/{repo}"\n'
             f'DEV_WORKTREE[{repo}]=0\n')
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


@pytest.mark.parametrize("flags,repo_mode,expected", [
    ("", "", "app api"),
    ("3", "", "app api 3"),
    ("--new", "", "app api --new"),
    ("--cli", "", "cli api"),
    ("--fg", "", "cli api -f"),
    ("--claude", "", "cli api --claude"),
    ("fg", "", "cli api fg"),
    ("thread-id", "", "cli api thread-id"),
    ("", "cli", "cli api"),
    ("--app", "cli", "app api --app"),
    ("--remote", "", "cli api -r"),
    ("--host mini", "", "remote mini api --cli"),
])
def test_open_mode_defaults_and_explicit_overrides(tmp_path, flags, repo_mode, expected):
    result = shell(tmp_path,
        '_t_open_app() { print -r -- "app $*"; }; '
        '_t_dev() { print -r -- "cli $*"; }; '
        '_dev_remote_open() { print -r -- "remote $*"; }; '
        f'_t_open api {flags}',
        local_text='DEV_REPOS[api]=/code/api\nDEV_OPEN_MODE_DEFAULT=app\n'
                   + (f'DEV_OPEN_MODE[api]={repo_mode}\n' if repo_mode else ''))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    cache = tmp_path / "home" / ".config" / "t" / "config.sh"
    assert "DEV_OPEN_MODE_DEFAULT=app" in cache.read_text()
    if repo_mode:
        assert f"DEV_OPEN_MODE[api]={repo_mode}" in cache.read_text()


def test_open_cli_and_app_conflict(tmp_path):
    result = shell(tmp_path, '_t_open api --app --cli')
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr


@pytest.mark.parametrize("args,expected", [
    ("api 3", "app api 3"),
    ("api 3 --codex", "app api 3 --codex"),
    ("alias 3", "app alias 3"),
    ("3", "app 3"),
    ("", "app "),
    ("api 4", "cli api 4"),
    ("api", "cli api"),
    ("api --new", "cli api new"),
    ("--new", "cli new"),
    ("api 3 --cli", "cli api 3"),
    ("api 3 --fg", "cli api 3 -f"),
    ("api 3 --claude", "cli api 3 --claude"),
    ("api 3 --remote", "cli api 3 -r"),
    ("api 3 --host mini", "remote mini api 3 --cli"),
])
def test_open_infers_reserved_desktop_slot_before_cli_default(tmp_path, args, expected):
    result = shell(tmp_path,
        '_t_open_app() { print -r -- "app $*"; }; '
        '_t_dev() { print -r -- "cli $*"; }; '
        '_dev_remote_open() { print -r -- "remote $*"; }; '
        '_t_infer_repo() { print -r -- api; }; '
        '_dev_repo_of_dir() { print -r -- $\'api\\t3\'; }; '
        '_dev_app_slot_reserved() { [[ $1 == "$HOME/worktrees/api/3" ]]; }; '
        f'_t_open {args}',
        local_text='DEV_REPOS[api]=/code/api\nDEV_REPOS[alias]=/code/api\n'
                   'DEV_WORKTREE_ROOT="$HOME/worktrees"\nDEV_OPEN_MODE_DEFAULT=cli\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == expected
