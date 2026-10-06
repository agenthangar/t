"""Closing a desktop reservation must not mistake it for an absent tmux slot."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_close(tmp_path, args, *, tmux_state="absent", reserved=True):
    home = tmp_path / "home"
    config = home / ".config" / "t"
    config.mkdir(parents=True)
    worktree = home / "worktrees" / "api" / "13"
    worktree.mkdir(parents=True)
    (config / "local.zsh").write_text(
        'DEV_REPOS[api]="$HOME/code/api"\n'
        'DEV_REPOS[alias]="$HOME/code/api"\n'
        'DEV_WORKTREE_ROOT="$HOME/worktrees"\n'
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    env.update(
        HOME=str(home), ZDOTDIR=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
    )
    env.pop("TMUX", None)
    env.pop("T_LOCAL_RC", None)
    code = f'''
source {shlex.quote(str(ROOT / "t.plugin.zsh"))}
tmux_state={shlex.quote(tmux_state)}
reserved={"1" if reserved else "0"}
_dev_app_slot_reserved() {{ [[ $reserved == 1 && $1 == "$HOME/worktrees/api/13" ]]; }}
_t_infer_repo() {{ print -r -- api; }}
_dev_remote_delegate() {{ print -u2 -- UNEXPECTED_REMOTE; return 1; }}
_dev_session_remote_fallback() {{ print -u2 -- UNEXPECTED_REMOTE; return 1; }}
_dev_kill_fg() {{ return 2; }}
_dev_session_has_claude() {{ [[ $tmux_state == cli ]]; }}
_dev_agent_of_session() {{ print -r -- codex; }}
_dev_stop_rooted() {{ :; }}
tmux() {{
  case $1 in
    has-session) [[ $tmux_state != absent && $3 == '=dev-api-13:' ]] ;;
    list-sessions) [[ $tmux_state != absent ]] && print -r -- dev-api-13 ;;
    display-message) print -r -- "$HOME/worktrees/api/13" ;;
    kill-session) print -r -- "$*" >> "$HOME/tmux-actions" ;;
  esac
}}
_dev_kill {args}
'''
    return subprocess.run(
        ["zsh", "-f", "-c", code], env=env, capture_output=True, text=True, timeout=20,
    )


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("args,tmux_state", [
    ("api 13", "absent"),
    ("alias 13", "absent"),
    ("api 13", "parked"),
    ("13", "absent"),
])
def test_desktop_slot_close_reports_owner_without_killing(tmp_path, args, tmux_state):
    result = run_close(tmp_path, args, tmux_state=tmux_state)
    assert result.returncode == 1
    assert "Codex desktop conversation" in result.stderr
    assert "there is no terminal session to kill" in result.stderr
    assert "t open" in result.stderr and "13 --cli" in result.stderr
    assert "No session" not in result.stdout + result.stderr
    assert not (tmp_path / "home" / "tmux-actions").exists()
    assert "UNEXPECTED_REMOTE" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("args", ["api", "api all"])
def test_desktop_only_repo_close_reports_reserved_slot(tmp_path, args):
    result = run_close(tmp_path, args)
    assert result.returncode == 1
    assert "api 13 is a Codex desktop conversation" in result.stderr
    assert "No sessions" not in result.stdout + result.stderr
    assert not (tmp_path / "home" / "tmux-actions").exists()


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
def test_live_cli_slot_remains_closable_even_with_stale_marker(tmp_path):
    result = run_close(tmp_path, "api 13 yes", tmux_state="cli")
    assert result.returncode == 0, result.stderr
    assert "Killed dev-api-13" in result.stdout
    assert "kill-session" in (tmp_path / "home" / "tmux-actions").read_text()
    assert "Codex desktop conversation" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
def test_missing_unreserved_slot_keeps_existing_message(tmp_path):
    result = run_close(tmp_path, "api 13", reserved=False)
    assert result.returncode == 1
    assert "No sessions for 'api' here." in result.stdout
    assert "Codex desktop conversation" not in result.stderr
