"""Closing a desktop reservation must not mistake it for an absent tmux slot."""

import os
from pathlib import Path
import json
import shlex
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_close(tmp_path, args, *, tmux_state="absent", reserved=True, close_rc=0,
              regular_cli=False):
    home = tmp_path / "home"
    config = home / ".config" / "t"
    config.mkdir(parents=True)
    worktree = home / "worktrees" / "api" / "13"
    worktree.mkdir(parents=True)
    bins = home / "bin"
    bins.mkdir()
    cli = bins / "t"
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/close-actions"\nexit "${T_CLOSE_RC:-0}"\n')
    cli.chmod(0o755)
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
        PATH=str(bins) + os.pathsep + os.environ["PATH"],
        T_CLOSE_RC=str(close_rc),
    )
    env.pop("TMUX", None)
    env.pop("T_LOCAL_RC", None)
    code = f'''
source {shlex.quote(str(ROOT / "t.plugin.zsh"))}
tmux_state={shlex.quote(tmux_state)}
reserved={"1" if reserved else "0"}
close_rc={close_rc}
regular_cli={"1" if regular_cli else "0"}
_dev_app_slot_reserved() {{ [[ $reserved == 1 && $1 == "$HOME/worktrees/api/13" ]]; }}
_t_infer_repo() {{ print -r -- api; }}
_dev_remote_delegate() {{ print -u2 -- UNEXPECTED_REMOTE; return 1; }}
_dev_session_remote_fallback() {{ print -u2 -- UNEXPECTED_REMOTE; return 1; }}
_dev_kill_fg() {{ return 2; }}
_dev_session_has_claude() {{ [[ $tmux_state == cli || ( $regular_cli == 1 && $1 == dev-api-14 ) ]]; }}
_dev_agent_of_session() {{ print -r -- codex; }}
_dev_stop_rooted() {{ :; }}
tmux() {{
  case $1 in
    has-session) [[ ( $tmux_state != absent && $3 == '=dev-api-13:' ) || ( $regular_cli == 1 && $3 == '=dev-api-14:' ) ]] ;;
    list-sessions) [[ $tmux_state != absent ]] && print -r -- dev-api-13; [[ $regular_cli == 1 ]] && print -r -- dev-api-14 ;;
    display-message)
      [[ $* == *dev-api-14* ]] && print -r -- "$HOME/worktrees/api/14" || print -r -- "$HOME/worktrees/api/13" ;;
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
def test_desktop_slot_close_dispatches_exact_slot_without_killing_tmux(tmp_path, args, tmux_state):
    result = run_close(tmp_path, args, tmux_state=tmux_state)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home" / "close-actions").read_text().strip() == \
        "_app-close " + ("alias" if args.startswith("alias") else "api") + " 13"
    assert "No session" not in result.stdout + result.stderr
    assert not (tmp_path / "home" / "tmux-actions").exists()
    assert "UNEXPECTED_REMOTE" not in result.stderr


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("args", ["api", "api all"])
def test_desktop_only_repo_close_lists_or_closes_reserved_slot(tmp_path, args):
    result = run_close(tmp_path, args)
    assert result.returncode == (0 if args.endswith("all") else 1)
    if args.endswith("all"):
        assert (tmp_path / "home" / "close-actions").read_text().strip() == "_app-close api 13"
    else:
        assert "api 13  ▣ desktop" in result.stdout
        assert not (tmp_path / "home" / "close-actions").exists()
    assert "No sessions" not in result.stdout + result.stderr
    assert not (tmp_path / "home" / "tmux-actions").exists()


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("args", ["api 13", "api all"])
def test_desktop_close_failure_propagates_without_killing_parked_tmux(tmp_path, args):
    result = run_close(tmp_path, args, tmux_state="parked", close_rc=1)
    assert result.returncode == 1
    assert (tmp_path / "home" / "close-actions").read_text().strip() == "_app-close api 13"
    assert not (tmp_path / "home" / "tmux-actions").exists()


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
def test_kill_all_leaves_loaded_desktop_parked_but_kills_regular_cli(tmp_path):
    result = run_close(tmp_path, "api all", tmux_state="parked", close_rc=1,
                       regular_cli=True)
    assert result.returncode == 1
    assert (tmp_path / "home" / "close-actions").read_text().strip() == "_app-close api 13"
    actions = (tmp_path / "home" / "tmux-actions").read_text().splitlines()
    assert actions == ["kill-session -t =dev-api-14:"]


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


def run_verified_close(tmp_path, *, blank=False, parked=False, wrong_stamp=False,
                       hold_lock=False, unreadable_pid=False, stop_failure=False,
                       frontend_running=False):
    """Run the real shell verifier against a disposable Git worktree and Codex index."""
    home = tmp_path / "home"
    home.mkdir()
    repo = home / "code" / "api"
    repo.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    wt = home / "worktrees" / "api" / "13"
    wt.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b",
                    "dev/api-13", str(wt)], check=True)
    gitdir = subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
                            check=True, capture_output=True, text=True).stdout.strip()
    marker = Path(gitdir) / "t-app-slot"
    marker.write_text("codex-app\n")
    config = home / ".config" / "t"
    config.mkdir(parents=True)
    (config / "local.zsh").write_text(
        'DEV_REPOS[api]="$HOME/code/api"\nDEV_WORKTREE_ROOT="$HOME/worktrees"\n')
    codex_home = home / "codex"
    locks = codex_home / "thread-writer-locks"
    locks.mkdir(parents=True)
    sid = "12345678-1234-1234-1234-123456789abc"
    rollout = codex_home / "rollout.jsonl"
    if not blank:
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "cwd": str(wt), "source": "cli", "cli_version": "0.160.0",
        }}) + "\n")
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute("create table threads (id text, cwd text, rollout_path text, archived integer)")
        if not blank:
            connection.execute("insert into threads values (?,?,?,0)", (sid, str(wt), str(rollout)))
    bins = home / "bin"
    bins.mkdir()
    ps = bins / "ps"
    ps.write_text('#!/bin/sh\n'
                  'if [ "$1 $2 $3 $4" = "-ww -x -o pid=,command=" ]; then\n'
                  '  [ "$T_TEST_FRONTEND" = 1 ] && '
                  'printf "123 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\\n"\n'
                  '  exit 0\nfi\n'
                  'exec /bin/ps "$@"\n')
    ps.chmod(0o755)
    env = {"HOME": str(home), "ZDOTDIR": str(home), "CODEX_HOME": str(codex_home),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
           "XDG_STATE_HOME": str(home / ".local" / "state"),
           "PATH": str(bins) + os.pathsep + os.environ["PATH"],
           "TERM": "dumb", "T_RECOVERY_DISABLE": "1",
           "T_TEST_FRONTEND": "1" if frontend_running else "0"}
    env.pop("TMUX", None)
    holder = None
    if hold_lock:
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; "
                                   "f=open(sys.argv[1],'w+'); fcntl.flock(f,fcntl.LOCK_EX); "
                                   "print('ready',flush=True); time.sleep(15)",
                                   str(locks / (sid + ".lock"))],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == "ready"
    script = f'''
source {shlex.quote(str(ROOT / "t.plugin.zsh"))}
_dev_cwd_pids() {{ {"print -r -- 999999999" if unreadable_pid else ":"}; }}
_dev_stop_rooted() {{ print -r -- stop >> "$HOME/close-actions"; {"return 1" if stop_failure else ":"}; }}
_dev_session_has_claude() {{ return 1; }}
tmux() {{
  case $1 in
    list-sessions) {"print -r -- 'dev-api-13|" + str(wt) + "'" if parked else ":"} ;;
    has-session) {":" if parked else "return 1"} ;;
    display-message) print -r -- {shlex.quote(str(wt))} ;;
    show-environment) {"return 1" if blank else "print -r -- 'CLAUDE_RESUME_ID=" + ("other" if wrong_stamp else sid) + "'"} ;;
    kill-session) print -r -- "$*" >> "$HOME/close-actions" ;;
  esac
}}
_t_app_close_slot dev-api-13 {shlex.quote(str(wt))} {"-" if blank else sid} \
  {shlex.quote(str(codex_home))} {"-" if blank else shlex.quote(str(rollout))} 0
'''
    try:
        result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                capture_output=True, text=True, timeout=20)
    finally:
        if holder:
            holder.terminate()
            holder.communicate(timeout=5)
    actions = home / "close-actions"
    return result, marker, actions.read_text().splitlines() if actions.exists() else [], wt, rollout


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("blank,parked", [(False, False), (False, True),
                                           (True, False), (True, True)])
def test_verified_desktop_close_preserves_history_and_worktree(tmp_path, blank, parked):
    result, marker, actions, wt, rollout = run_verified_close(tmp_path, blank=blank,
                                                               parked=parked)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert wt.is_dir()
    assert rollout.exists() is (not blank)
    assert actions == (["kill-session -t =dev-api-13:"] if parked else []) + ["stop"]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("wrong_stamp,hold_lock", [(True, False), (False, True)])
def test_desktop_close_fails_before_cleanup_when_identity_or_writer_changes(tmp_path,
                                                                             wrong_stamp,
                                                                             hold_lock):
    result, marker, actions, wt, rollout = run_verified_close(
        tmp_path, parked=True, wrong_stamp=wrong_stamp, hold_lock=hold_lock)
    assert result.returncode != 0
    assert marker.exists() and wt.is_dir() and rollout.exists()
    assert actions == []


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("unreadable_pid,stop_failure", [(True, False), (False, True)])
def test_desktop_close_keeps_marker_when_process_inspection_or_stop_fails(tmp_path,
                                                                            unreadable_pid,
                                                                            stop_failure):
    result, marker, actions, wt, rollout = run_verified_close(
        tmp_path, unreadable_pid=unreadable_pid, stop_failure=stop_failure)
    assert result.returncode != 0
    assert marker.exists() and wt.is_dir() and rollout.exists()
    assert actions == ([] if unreadable_pid else ["stop"])


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("blank", [False, True])
def test_desktop_close_requires_view_release_even_when_writer_is_free(tmp_path, blank):
    result, marker, actions, wt, rollout = run_verified_close(
        tmp_path, blank=blank, frontend_running=True)
    assert result.returncode != 0
    assert marker.exists() and wt.is_dir()
    assert actions == []
    assert ("close Codex" if blank else "Archive this chat") in result.stderr
