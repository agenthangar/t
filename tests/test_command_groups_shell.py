"""Grouped t commands keep shell-bound behavior and completion in zsh."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def shell(tmp_path, code, *, bin_text=None):
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "t").write_text(bin_text or "#!/bin/sh\nprintf 'bin:'\nprintf '<%s>' \"$@\"\nprintf '\\n'\n")
    (bindir / "t").chmod(0o755)
    env = {**os.environ,
           "HOME": str(home),
           "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_CACHE_HOME": str(home / ".cache"),
           "XDG_STATE_HOME": str(home / ".local" / "state"),
           "PATH": f"{bindir}:{os.environ['PATH']}",
           "T_NO_UPDATE_CHECK": "1"}
    env.pop("T_LOCAL_RC", None)
    return subprocess.run(
        ["zsh", "-f", "-c", f'source "{ROOT / "t.plugin.zsh"}"; {code}'],
        env=env, capture_output=True, text=True, timeout=20,
    )


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_grouped_shell_commands_keep_arguments_and_caller_shell(tmp_path):
    result = shell(tmp_path, '''
        _t_open() { print -r -- "open:${(j:,:)@}"; }
        _t_push() { print -r -- "push:${(j:,:)@}"; }
        _t_find() { print -r -- "find:${(j:,:)@}"; }
        _t_beam_xlate() { print -r -- "move:${(j:,:)@}"; }
        _t_repos_cd() { print -r -- "repo-cd:${(j:,:)@}"; }
        t session open api 4 --fg
        t session push api 4
        t session search "a search query"
        t session move api 4 --host mini
        t repo cd api
        t open api 4
        t repos cd api
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "open:api,4,--fg", "push:api,4", "find:a search query",
        "move:api,4,--host,mini", "repo-cd:api", "open:api,4",
        "repo-cd:api",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_grouped_bin_commands_and_help_keep_exact_arguments(tmp_path):
    result = shell(tmp_path, '''
        _t_open() { print -r -- "BAD SHELL DISPATCH"; }
        t session read api 4
        t repo locate api
        t host run mini -- echo --help
        t session open --help
        t session
        t repo
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bin:<session><read><api><4>",
        "bin:<repo><locate><api>",
        "bin:<host><run><mini><--><echo><--help>",
        "bin:<session><open><--help>",
        "bin:<session>", "bin:<repo>",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
@pytest.mark.parametrize("command", ["t open", "t session open"])
@pytest.mark.parametrize("repo,hint", [
    ("codex", "t open --codex --new"),
    ("claude", "t open --claude --new"),
    ("missing", "t repo list"),
    ("", "t open <repo>"),
])
def test_invalid_open_prints_actionable_error_instead_of_help(tmp_path, command, repo, hint):
    result = shell(tmp_path, f'''
        DEV_REPOS=()
        _t_infer_repo() {{ return 1; }}
        {command} {repo} --new
    ''')
    assert result.returncode == 1
    assert result.stdout == ""
    assert hint in result.stderr
    assert "t open --help" in result.stderr
    assert len(result.stderr.splitlines()) == 3
    assert ("unknown repository" if repo else "not inside a registered repository") in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_registered_repo_named_codex_still_opens(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(codex "$HOME/code/codex")
        _dev_branch_for() { print -r -- main; }
        t open codex --new --cli
    ''')
    # It reaches the checkout check, rather than treating a registered name as
    # an agent selector or starting a session against the developer's tmux.
    assert result.returncode == 1
    assert "Repo dir not found:" in result.stdout
    assert "unknown repository" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_legacy_and_grouped_config_edits_reload_the_caller(tmp_path):
    result = shell(tmp_path, '''
        _t_reload() { print -r -- reloaded; }
        t config --edit
        t config edit
        t config show --edit
    ''', bin_text='''#!/bin/sh
printf 'bin:'; printf '<%s>' "$@"; printf '\\n'
if [ "$1" = config ]; then printf 'changed\\n' >> "$T_LOCAL_RC"; fi
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bin:<config><--edit>", "reloaded",
        "bin:<config><edit>", "reloaded",
        "bin:<config><show><--edit>", "reloaded",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_completion_exposes_groups_and_group_actions(tmp_path):
    result = shell(tmp_path, '''
        _describe() { print -r -- "describe:$4"; }
        _values() { print -r -- "values:${(j:,:)argv[2,-1]}"; }
        local -a words
        words=(t '') CURRENT=2; _t
        words=(t session '') CURRENT=3; _t
        words=(t repo '') CURRENT=3; _t
        words=(t host run '') CURRENT=4; _t
    ''')
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:3] == ["describe:nouns", "describe:actions", "describe:actions"]
    assert lines[3].startswith("values:")
    source = (ROOT / "zsh" / "shim.zsh").read_text()
    assert "local -a nouns=(session repo cursor host config profile policy agent system help)" in source


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_app_completion_tracks_direction_and_arguments(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(api /tmp/api)
        _values() { print -r -- "values:${(j:,:)argv[2,-1]}"; }
        _message() { print -r -- "message:$1"; }
        local -a words
        words=(t app '') CURRENT=3; _t
        words=(t app push '') CURRENT=4; _t
        words=(t session open-app pull api '') CURRENT=6; _t
        words=(t app pull api 3 --) CURRENT=6; _t
        words=(t session open-app pull api 3 --thread '') CURRENT=8; _t
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "values:push,pull,api", "values:api", "message:local slot number",
        "values:--thread,--dry-run,-h,--help", "message:saved conversation ID",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_shell_dispatch_matches_python_command_registry(tmp_path, t_mod):
    # These actions are handled in the caller's shell or must reload it after
    # running the binary. Compare both public and compatibility spellings with
    # the Python registry so a registry edit cannot strand the zsh front door.
    handlers = {
        ("session", "open"): "open", ("session", "pop"): "pop",
        ("session", "push"): "push", ("session", "resume"): "resume",
        ("session", "cd"): "cd", ("session", "move"): "move",
        ("session", "search"): "search", ("repo", "cd"): "repo-cd",
        ("config", "open"): "install:config", ("config", "show"): "install:config",
        ("config", "edit"): "install:config", ("config", "setup"): "install:setup",
        ("repo", "create"): "bin", ("repo", "clone"): "install:checkout",
        ("agent", "install"): "install:install",
        ("system", "integrate"): "bin", ("system", "update"): "bin",
    }
    commands = []
    expected = []
    for (group, action), handler in handlers.items():
        legacy = t_mod.COMMAND_GROUPS[group][action]
        for spelling, prefix in (("canonical", (group, action)), ("legacy", legacy)):
            # Only shell commands whose established parser expects an argument
            # receive one; no stub ever launches a session or changes config.
            args = ("sentinel",) if (group, action) in {
                ("session", "open"), ("session", "resume"), ("session", "cd"),
                ("session", "move"), ("repo", "cd"), ("repo", "create"),
                ("repo", "clone"),
            } else ()
            words = (*prefix, *args)
            commands.append("t " + shlex.join(words))
            expected.append((group, action, spelling, handler, words))
    code = '''
        _t_open() { print -r -- handler:open; }
        _t_pop() { print -r -- handler:pop; }
        _t_push() { print -r -- handler:push; }
        _t_resume() { print -r -- handler:resume; }
        _t_cd() { print -r -- handler:cd; }
        _t_beam_xlate() { print -r -- handler:move; }
        _t_find() { print -r -- handler:search; }
        _t_repos_cd() { print -r -- handler:repo-cd; }
        _t_install() { print -r -- "handler:install:$1"; }
        _t_reload() { print -r -- reloaded; }
    ''' + "\n".join(commands)
    result = shell(tmp_path, code)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    cursor = 0
    for group, action, spelling, handler, words in expected:
        assert cursor < len(lines), (group, action, spelling, result.stdout)
        line = lines[cursor]
        if handler == "bin":
            assert line == "bin:" + "".join(f"<{word}>" for word in words), (group, action, spelling, line)
            cursor += 1
            assert lines[cursor] == "reloaded", (group, action, spelling, lines[cursor])
        else:
            assert line == "handler:" + handler, (group, action, spelling, line)
        cursor += 1
    assert cursor == len(lines), lines[cursor:]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_completion_actions_match_python_command_registry(tmp_path, t_mod):
    groups = tuple(t_mod.COMMAND_GROUPS)
    code = '''
        _describe() { typeset -p "$4"; }
        local -a words
    ''' + "\n".join(
        f"words=(t {shlex.quote(group)} '') CURRENT=3; _t"
        for group in groups
    )
    result = shell(tmp_path, code)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(groups), result.stdout
    for group, line in zip(groups, lines):
        assert "actions=( " in line and line.endswith(" )"), (group, line)
        offered = set(line.partition("actions=( ")[2].removesuffix(" )").split())
        assert offered == set(t_mod.COMMAND_GROUPS[group]), (group, offered)
