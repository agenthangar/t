"""Grouped t commands keep shell-bound behavior and completion in zsh."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

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
    for key in tuple(env):
        if key.startswith("COV_CORE_"):
            env.pop(key)
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
        t hosts run mini -- echo --help
        t session open --help
        t session
        t repo
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bin:<session><read><api><4>",
        "bin:<repo><locate><api>",
        "bin:<hosts><run><mini><--><echo><--help>",
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
@pytest.mark.parametrize("command,placeholder,case", [
    ("t open api 3 --cli", False, "success"),
    ("t open api 3 --cli", True, "success"),
    ("t session open api 3 --cli", False, "success"),
    ("t open alias 3 --cli", False, "success"),
    ("t open 3 --cli", False, "success"),
    ("t open --cli", False, "success"),
    ("t open api 3 --fg", False, "success"),
    *[("t open api 3 --cli", placeholder, case)
      for placeholder in (False, True)
      for case in ("blocked", "malformed", "wrong_repo", "cli_exited")],
])
def test_open_hands_desktop_slot_to_cli_before_attaching(tmp_path, monkeypatch,
                                                       command, placeholder, case):
    home = tmp_path / "home"
    (home / "code" / "api").mkdir(parents=True)
    marker = home / "app-slot"
    marker.write_text("codex-app\n")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "tmux"))
    monkeypatch.delenv("TMUX", raising=False)
    result = shell(tmp_path, f'''
        DEV_REPOS=(api "$HOME/code/api" alias "$HOME/code/api")
        DEV_WORKTREE_ROOT="$HOME/worktrees"
        _dev_branch_for() {{ print main; }}
        _t_infer_repo() {{ print api; }}
        _dev_repo_of_dir() {{ print -r -- $'api\\t3'; }}
        _dev_app_slot_marker() {{ print -r -- "$HOME/app-slot"; }}
        _dev_worktree_create() {{ print BAD-create >> "$HOME/actions"; return 99; }}
        _dev_worktree_freshen() {{ print BAD-freshen >> "$HOME/actions"; return 99; }}
        _dev_new_session() {{ print BAD-launch >> "$HOME/actions"; return 99; }}
        _dev_agent_check() {{ print BAD-agent >> "$HOME/actions"; return 99; }}
        _t_pop() {{ print -r -- "pop:$1" >> "$HOME/actions"; }}
        tmux() {{
          case $1 in
            has-session) [[ {int(placeholder)} == 1 && $3 == '=dev-api-3:' ]] ;;
            attach-session)
              print -r -- "attach:$3" >> "$HOME/actions"
              return {19 if case == 'cli_exited' else 0} ;;
            *) print -r -- "BAD-tmux:$*" >> "$HOME/actions"; return 99 ;;
          esac
        }}
        {command}
    ''', bin_text=f'''#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/bridge-calls"
[ "$1" = _app-open-cli ] || exit 98
case {shlex.quote(case)} in
  blocked) printf '%s\\n' 't open: quit Codex and retry t open api 3 --cli' >&2; exit 7 ;;
  malformed) printf '%s\\n' 'unexpected output'; exit 0 ;;
  wrong_repo) printf '%s\\n' dev-other-3; exit 0 ;;
esac
rm "$HOME/app-slot"
printf '%s\\n' dev-api-3
''')
    assert result.returncode == {"success": 0, "blocked": 7, "malformed": 1, "wrong_repo": 1, "cli_exited": 19}[case], result.stderr
    repo = "alias" if " alias " in command else "api"
    assert (home / "bridge-calls").read_text() == f"_app-open-cli {repo} 3\n"
    actions = (home / "actions").read_text() if (home / "actions").exists() else ""
    expected = "pop:dev-api-3\n" if "--fg" in command else "attach:=dev-api-3:\n"
    assert actions == (expected if case in ("success", "cli_exited") else "")
    assert marker.exists() is (case in ("blocked", "malformed", "wrong_repo"))
    assert "could not create the worktree" not in result.stderr


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
def test_host_management_reloads_targets_defaults_and_shortcuts(tmp_path):
    result = shell(tmp_path, '''
        t hosts add lab user@old --default
        print -r -- "added:$REMOTE_HOSTS[lab]:$TBEAM_HOST:${+functions[lab]}"
        t hosts edit lab user@new
        print -r -- "edited:$REMOTE_HOSTS[lab]:$TBEAM_HOST"
        t hosts default --clear
        print -r -- "cleared:${TBEAM_HOST:-none}"
        t hosts default lab
        t hosts rm lab
        print -r -- "removed:${#REMOTE_HOSTS}:${TBEAM_HOST:-none}:${+functions[lab]}"
        personal() { print -r -- personal-function; }
        t hosts add personal user@personal
        t hosts remove personal
        personal
    ''', bin_text=f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'bin/t'))} \"$@\"\n")
    assert result.returncode == 0, result.stderr
    assert "added:user@old:user@old:1" in result.stdout
    assert "edited:user@new:user@new" in result.stdout
    assert "cleared:none" in result.stdout
    assert "removed:0:none:0" in result.stdout
    assert result.stdout.splitlines()[-1] == "personal-function"


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_host_completion_covers_plural_aliases_targets_and_flags(tmp_path):
    result = shell(tmp_path, '''
        REMOTE_HOSTS=(lab user@lab)
        _values() { print -r -- "values:${(j:,:)argv[2,-1]}"; }
        _message() { print -r -- "message:$1"; }
        local -a words
        words=(t hosts rm '') CURRENT=4; _t
        words=(t hosts edit lab '') CURRENT=5; _t
        words=(t hosts add '') CURRENT=4; _t
        words=(t hosts add lab user@lab --) CURRENT=6; _t
        words=(t hosts default --) CURRENT=4; _t
        words=(t hosts list --) CURRENT=4; _t
        words=(t hosts --) CURRENT=3; _t
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "values:lab", "message:SSH config name, address or user@host",
        "message:new host alias", "values:--default,-h,--help",
        "values:--clear,-h,--help", "values:--json,-h,--help",
        "values:--json,-h,--help",
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
        words=(t hosts run '') CURRENT=4; _t
    ''')
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:3] == ["describe:nouns", "describe:actions", "describe:actions"]
    assert lines[3].startswith("values:")
    source = (ROOT / "zsh" / "shim.zsh").read_text()
    assert "local -a nouns=(session repo cursor hosts config profile policy agent system login help)" in source


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


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_login_spellings_forward_without_shell_session_actions(tmp_path):
    result = shell(tmp_path, '''
        t login --ignore codex --dry-run
        t agent login claude --within-hours 2
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bin:<login><--ignore><codex><--dry-run>",
        "bin:<agent><login><claude><--within-hours><2>",
    ]
