"""Recovery keeps the exact thread and never launches alongside its old owner."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture
def restart_slot(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, "CONFIG", str(tmp_path / "no-config"))
    cfg = t_mod.Config()
    cfg.repos = {"api": str(tmp_path / "api")}
    cfg.worktree_root = str(tmp_path / "worktrees")
    cwd = tmp_path / "worktrees" / "api" / "1"
    cwd.mkdir(parents=True)
    row = dict(host="local", sid=SID, cwd=str(cwd), slot="api-1",
               state="detached", context="active", agent="codex", summary="Task")
    monkeypatch.setattr(t_mod, "zsh_capture", lambda _: "rows")
    monkeypatch.setattr(t_mod, "_parse_rows", lambda _: [row])
    return cfg, row


def test_restart_command(t_mod, restart_slot, monkeypatch, capsys):
    cfg, row = restart_slot
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Saved pane text\n", "")
    monkeypatch.setattr(t_mod, "_run", run)
    args = t_mod.build_parser().parse_args(["restart", "api", "1"])
    assert t_mod.cmd_restart(cfg, args) == 0
    assert shlex.join(["_t_restart_slot", "dev-api-1", row["cwd"], SID, "codex", "restart"]) in calls[0][-1]
    assert "t open api 1" in capsys.readouterr().out
    args.dry_run = True
    assert t_mod.cmd_restart(cfg, args) == 0
    assert calls[-1][-1].endswith("dry-run")
    assert "Restarted" not in capsys.readouterr().out


@pytest.mark.parametrize("problem", ["agent", "sid", "cwd", "ambiguous", "failure"])
def test_restart_preflight_and_failure(t_mod, restart_slot, monkeypatch, capsys, problem):
    cfg, row = restart_slot
    if problem == "agent":
        row["agent"] = "cursor"
    elif problem == "sid":
        row["sid"] = "-"
    elif problem == "cwd":
        Path(row["cwd"]).rmdir()
    elif problem == "ambiguous":
        monkeypatch.setattr(t_mod, "_parse_rows", lambda _: [row, dict(row, slot="api-2")])
    def run(*a, **kw):
        assert problem == "failure"
        return subprocess.CompletedProcess([], 1, "", "client has not exited\n")
    monkeypatch.setattr(t_mod, "_run", run)
    args = t_mod.build_parser().parse_args(["restart", "api"])
    assert t_mod.cmd_restart(cfg, args) == 1
    assert capsys.readouterr().err


def run_shell(tmp_path, code):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path),
               XDG_CACHE_HOME=str(tmp_path / "cache"),
               XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_STATE_HOME=str(tmp_path / "state"))
    env.pop("TMUX", None)
    return subprocess.run(["zsh", "-f", "-c", f"source {shlex.quote(str(ROOT / 'zsh/resume.zsh'))}\n" + code],
                          env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh required")
@pytest.mark.parametrize("scenario", ["normal", "dry", "missing", "changed", "desktop", "no_pid", "self",
                                      "capture_failed", "race", "still_running", "pane_alive", "respawn_failed",
                                      "locked", "term_failed", "multiple_panes", "stopped_dead"])
def test_restart_shell_safety(tmp_path, scenario):
    log = tmp_path / "actions"
    lock = tmp_path / "cache/t/restart/dev-api-1.lock"
    if scenario == "locked":
        lock.mkdir(parents=True)
    code = f'''
scenario={scenario}
dir={shlex.quote(str(tmp_path))}
log={shlex.quote(str(log))}
tmux() {{
  print -r -- "$*" >> "$log"
  case $1 in
    has-session) [[ $scenario != missing ]]; return ;;
    display-message)
      case ${{@: -1}} in
        *session_path*) print -r -- "$dir" ;;
        *pane_id*) print '%1' ;;
        *pane_dead*) [[ $scenario == pane_alive || $scenario == no_pid ]] && print 0 || print 1 ;;
      esac ;;
    list-panes) print '%1'; [[ $scenario == multiple_panes ]] && print '%2' ;;
    capture-pane) [[ $scenario == capture_failed ]] && return 1; print 'unsent draft' ;;
    show-options) print off ;;
    respawn-pane) [[ $scenario != respawn_failed ]]; return ;;
  esac
  return 0
}}
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ [[ $scenario == changed ]] && print other || print {SID}; }}
_dev_app_slot_reserved() {{ [[ $scenario == desktop ]]; }}
_dev_session_claude_pid() {{
  [[ $scenario == no_pid || $scenario == stopped_dead ]] && return 1
  [[ $scenario == race && -f $log && $(<"$log") == *capture-pane* ]] && {{ print 88888; return; }}
  [[ $scenario == self ]] && print $PPID || print 99999
}}
_dev_agent_resume_cmd() {{ print -r -- "codex resume $2"; }}
ps() {{ print 1; }}
kill() {{
  print -r -- "signal $*" >> "$log"
  [[ $1 == -TERM ]] && {{ [[ $scenario != term_failed ]]; return; }}
  [[ $scenario == still_running ]]
}}
sleep() {{ :; }}
_t_restart_slot dev-api-1 "$dir" {SID} codex restart
'''
    if scenario == "dry":
        code = code.rsplit("_t_restart_slot", 1)[0] + f'_t_restart_slot dev-api-1 "$dir" {SID} codex dry-run\n'
    result = run_shell(tmp_path, code)
    assert (result.returncode == 0) == (scenario in ("normal", "dry", "stopped_dead")), result.stderr
    actions = log.read_text() if log.exists() else ""
    if scenario in ("normal", "still_running", "pane_alive", "respawn_failed", "term_failed"):
        assert actions.index("capture-pane") < actions.index("signal -TERM")
        assert "Saved pane text" in result.stdout
        saved = [p for p in lock.parent.iterdir() if p.is_file()]
        assert len(saved) == 1
        assert saved[0].read_text() == "unsent draft\n"
        assert saved[0].stat().st_mode & 0o777 == 0o600
    else:
        assert "signal" not in actions
    assert ("respawn-pane" in actions) == (scenario in ("normal", "respawn_failed", "stopped_dead"))
    assert "respawn-pane -k" not in actions
    assert lock.exists() == (scenario == "locked")


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("zsh"), reason="tmux and zsh required")
@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_restart_real_tmux_preserves_pane_thread_and_worktree(tmp_path, agent):
    socket = "t-restart-" + uuid.uuid4().hex
    tmux = [shutil.which("tmux"), "-L", socket, "-f", os.devnull]
    session = "dev-api-1"
    thread_log = tmp_path / "resumed"
    # The replacement uses a login shell with an isolated home. This function
    # stands in for the agent, recording the actual argv and cwd of the launch.
    # Ubuntu's global zshrc runs compinit before this home's zshrc. CI's shared
    # completion directories can trigger an interactive permissions prompt;
    # completion setup is unrelated to the login-shell contract under test.
    (tmp_path / ".zshenv").write_text("skip_global_compinit=1\n")
    (tmp_path / ".zshrc").write_text(f'{agent}() {{ print -r -- "$PWD|$*" > {shlex.quote(str(thread_log))}; sleep 30; }}\n')
    dirty = tmp_path / "uncommitted.txt"
    dirty.write_text("keep my changes\n")
    code = f'''
tmux() {{ command {shlex.join(tmux)} "$@"; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{ tmux display-message -p -t '=dev-api-1:' '#{{pane_pid}}'; }}
_dev_app_slot_reserved() {{ return 1; }}
source {shlex.quote(str(ROOT / "zsh/agent.zsh"))}
_dev_agent_of_session() {{ print {agent}; }}
tmux new-session -d -s {session} -c {shlex.quote(str(tmp_path))} 'exec sleep 60' || exit
tmux set-option -p -t '=dev-api-1:' remain-on-exit on
before=$(tmux display-message -p -t '=dev-api-1:' '#{{pane_id}}')
_t_restart_slot {session} {shlex.quote(str(tmp_path))} {SID} {agent} restart || exit
[[ $(tmux display-message -p -t '=dev-api-1:' '#{{pane_id}}') == $before ]] || exit 2
tmux show-options -p -v -t '=dev-api-1:' remain-on-exit
'''
    try:
        result = run_shell(tmp_path, code)
        assert result.returncode == 0, result.stderr
        assert result.stdout.endswith("on\n")
        deadline = time.monotonic() + 5
        while not thread_log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not thread_log.exists():
            pane = subprocess.run(tmux + ["capture-pane", "-p", "-t", "=dev-api-1:"],
                                  capture_output=True, text=True)
            pytest.fail(f"replacement agent did not start: {pane.stdout}{pane.stderr}")
        resume_arg = "resume" if agent == "codex" else "-r"
        assert thread_log.read_text().strip() == f"{tmp_path}|{resume_arg} {SID}"
        assert dirty.read_text() == "keep my changes\n"
    finally:
        subprocess.run(tmux + ["kill-server"], capture_output=True)
