"""Safety checks for the desktop-to-CLI tmux handoff."""

import os
import pathlib
import shlex
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SID = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize(
    "case,ok,launch",
    [
        ("missing", True, "new-session"),
        ("dead", True, "respawn-pane"),
        ("app_only", True, "respawn-pane"),
        ("shell", True, "new-window"),
        ("no_marker", False, None),
        ("alias", False, None),
        ("duplicate", False, None),
        ("other_owner", False, None),
        ("foreground_cwd", False, None),
        ("other_thread", False, None),
        ("live_agent", False, None),
    ("live_verified", True, None),
        ("many_panes", False, None),
        ("launch_failure", False, "respawn-pane"),
    ],
)
def test_app_pull_preserves_ownership_until_verified(tmp_path, case, ok, launch):
    if not shutil.which("zsh"):
        pytest.skip("zsh required")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    marker = tmp_path / "t-app-slot"
    if case != "no_marker":
        marker.write_text("codex-app\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "codex").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "codex").chmod(0o755)
    calls = tmp_path / "calls"
    expected = "-" if case in ("missing", "app_only") else SID
    script = f'''
source {shlex.quote(str(ROOT / 'zsh/resume.zsh'))}
case={shlex.quote(case)}
_test_marker={shlex.quote(str(marker))}
calls={shlex.quote(str(calls))}
launched=0
_dev_app_slot_marker() {{ print -r -- "$_test_marker"; }}
_dev_fg_rows() {{ [[ $case == foreground_cwd ]] && print -r -- $'other-thread\\t'{shlex.quote(str(worktree))}$'\\tforeground:fg'; return 0; }}
_dev_recovery_watch() {{ :; }}
_t_app_pull_ready() {{ [[ $case != launch_failure && $case != live_agent ]] ; }}
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{
  [[ $case == app_only && $launched == 0 ]] && return 0
  [[ $case == other_thread && $launched == 0 ]] && print other || print {SID}
}}
_dev_session_claude_pid() {{
  [[ $case == live_agent || $case == live_verified || ( $case == other_owner && $1 == dev-other-1 ) ||
     ( $launched == 1 && $case != launch_failure ) ]] && print 123
}}
tmux() {{
  case "$1" in
    list-sessions)
      case $case in
        missing|no_marker) ;;
        alias) print -r -- $'dev-alias\\t'{shlex.quote(str(worktree))} ;;
        duplicate) print -r -- $'dev-repo-1\\t'{shlex.quote(str(worktree))}; print -r -- $'dev-alias\\t'{shlex.quote(str(worktree))} ;;
        other_owner) print -r -- $'dev-repo-1\\t'{shlex.quote(str(worktree))}; print -r -- $'dev-other-1\\t/other/worktree' ;;
        *) print -r -- $'dev-repo-1\\t'{shlex.quote(str(worktree))} ;;
      esac ;;
    has-session) [[ $case != missing ]] || return 1 ;;
    display-message)
      [[ "$*" == *pane_dead* ]] && {{ [[ $case == shell ]] && print 0 || print 1; return; }}
      print -r -- {shlex.quote(str(worktree))} ;;
    list-panes)
      print '%1'; [[ $case == many_panes || $case == live_verified ]] && print '%2' ;;
    respawn-pane|new-window|new-session)
      print -r -- "$1 $*" >> "$calls"
      launched=1 ;;
  esac
  return 0
}}
sleep() {{ :; }}
_t_app_pull_slot dev-repo-1 {shlex.quote(str(worktree))} {SID} {expected}
'''
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_STATE_HOME": str(tmp_path / "state"),
           "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"], "TERM": "dumb"}
    result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is ok, result.stderr
    recorded = calls.read_text() if calls.exists() else ""
    if launch:
        assert launch in recorded
        assert SID in recorded
        assert "zsh -lic" in recorded
    else:
        assert recorded == ""
    assert marker.exists() is (not ok and case != "no_marker")


@pytest.mark.parametrize("mode", ["missing", "dead", "shell", "failure"])
def test_app_pull_real_tmux_launch(tmp_path, mode):
    """Exercise tmux's actual command parsing, pane lifetime, and window selection."""
    if not all(shutil.which(name) for name in ("tmux", "zsh", "cc")):
        pytest.skip("tmux, zsh, and cc required")
    import time
    import uuid

    worktree = tmp_path / "work tree"
    worktree.mkdir()
    marker = tmp_path / "t-app-slot"
    marker.write_text("codex-app\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / ".zshrc").write_text("export PATH=" + shlex.quote(str(fake_bin)) + ":$PATH\n")
    source = tmp_path / "codex.c"
    source.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char cwd[4096];
    getcwd(cwd, sizeof cwd);
    FILE *f = fopen(getenv("T_APP_TEST_RECORD"), "w");
    if (f) {
        fprintf(f, "%s\n", cwd);
        fprintf(f, "pid=%d\n", getpid());
        for (int i = 1; i < argc; i++) fprintf(f, "%s\n", argv[i]);
        fclose(f);
    }
    if (getenv("T_APP_TEST_FAIL")) { sleep(8); return 1; }
    char registry[4096];
    snprintf(registry, sizeof registry, "%s/claude-sessions/%d", getenv("XDG_CACHE_HOME"), getpid());
    f = fopen(registry, "w");
    if (f) { fprintf(f, "%s\t%s\n", argv[2], cwd); fclose(f); }
    sleep(15);
    return 0;
}
''')
    subprocess.run(["cc", str(source), "-o", str(fake_bin / "codex")],
                   check=True, capture_output=True)
    socket = "t-app-pull-" + uuid.uuid4().hex
    tmux = [shutil.which("tmux"), "-L", socket, "-f", os.devnull]
    env = dict((k, v) for k, v in os.environ.items() if not k.startswith("COV_CORE_"))
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path),
               XDG_CACHE_HOME=str(tmp_path / "cache"),
               XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_STATE_HOME=str(tmp_path / "state"),
               T_APP_TEST_RECORD=str(tmp_path / "launch"),
               TERM="xterm-256color", T_RECOVERY_DISABLE="1",
               PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
    (tmp_path / "cache" / "claude-sessions").mkdir(parents=True)
    if mode == "failure":
        env["T_APP_TEST_FAIL"] = "1"
    session = "dev-test-1"

    def run_tmux(*args, check=True):
        return subprocess.run(tmux + list(args), env=env, capture_output=True,
                              text=True, check=check)

    try:
        if mode != "missing":
            run_tmux("new-session", "-d", "-s", session, "-c", str(worktree), "sleep 30")
            run_tmux("set-environment", "-t", "=" + session, "CLAUDE_RESUME_ID", SID)
            run_tmux("set-environment", "-t", "=" + session, "DEV_AGENT", "codex")
            if mode in ("dead", "failure"):
                pane = run_tmux("display-message", "-p", "-t", "=" + session + ":",
                                "#{pane_id}").stdout.strip()
                run_tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
                run_tmux("respawn-pane", "-k", "-t", pane, "false")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if run_tmux("display-message", "-p", "-t", pane,
                                "#{pane_dead}").stdout.strip() == "1":
                        break
                    time.sleep(.02)
        script = f'''
zmodload zsh/datetime
source {shlex.quote(str(ROOT / 'zsh/agent.zsh'))}
source {shlex.quote(str(ROOT / 'zsh/sessions.zsh'))}
source {shlex.quote(str(ROOT / 'zsh/resume.zsh'))}
tmux() {{ command {shlex.join(tmux)} "$@"; }}
_dev_app_slot_marker() {{ print -r -- {shlex.quote(str(marker))}; }}
_dev_fg_rows() {{ :; }}
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ tmux show-environment -t "=$1" CLAUDE_RESUME_ID 2>/dev/null | cut -d= -f2; }}
_dev_recovery_watch() {{ :; }}
_codex_pane_sid() {{ :; }}
_t_app_pull_slot {session} {shlex.quote(str(worktree))} {SID} {'-' if mode == 'missing' else SID}
'''
        result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                capture_output=True, text=True, timeout=15)
        if (result.returncode == 0) is (mode == "failure"):
            launch_path = tmp_path / "launch"
            launch = launch_path.read_text().splitlines() if launch_path.exists() else []
            fake_pid = launch[1][4:] if len(launch) > 1 and launch[1].startswith("pid=") else None
            fake_ps = (subprocess.run(["ps", "-o", "lstart=,comm=", "-p", fake_pid],
                                      env=env, capture_output=True, text=True).stdout.strip()
                       if fake_pid else "no fake codex PID recorded")
            registry = []
            for entry in sorted((tmp_path / "cache" / "claude-sessions").iterdir()):
                registry.append((entry.name, entry.stat().st_mtime, entry.read_text()))
            diagnostic = {
                "stderr": result.stderr,
                "launch": launch,
                "fake_ps": fake_ps,
                "registry": registry,
                "panes": run_tmux("list-panes", "-a", "-F",
                                  "#{session_name} #{pane_id} #{pane_pid} #{pane_current_command} #{pane_dead}",
                                  check=False).stdout.strip(),
                "pane_text": run_tmux("capture-pane", "-p", "-t", "=" + session + ":",
                                      check=False).stdout.strip(),
            }
            pytest.fail(f"app pull real tmux {mode}: {diagnostic}")
        assert marker.exists() is (mode == "failure")
        if mode != "failure":
            assert (tmp_path / "launch").exists(), (
                run_tmux("capture-pane", "-p", "-t", "=" + session + ":").stdout,
                run_tmux("show-environment", "-g", "T_APP_TEST_RECORD", check=False).stdout,
                run_tmux("list-panes", "-s", "-t", "=" + session,
                         "-F", "#{pane_dead} #{pane_current_command} #{pane_pid}").stdout)
            record = (tmp_path / "launch").read_text().splitlines()
            assert record[0] == str(worktree)
            assert record[1].startswith("pid=")
            assert record[2:] == ["resume", SID, "--cd", str(worktree)]
            assert run_tmux("show-environment", "-t", "=" + session,
                            "CLAUDE_RESUME_ID").stdout.strip() == "CLAUDE_RESUME_ID=" + SID
            if mode == "shell":
                windows = run_tmux("list-windows", "-t", "=" + session,
                                   "-F", "#{window_active} #{pane_current_command}").stdout.splitlines()
                assert len(windows) == 2
                assert "1 codex" in windows
        else:
            pane = run_tmux("display-message", "-p", "-t", "=" + session + ":",
                            "#{pane_id}").stdout.strip()
            assert run_tmux("show-option", "-p", "-v", "-t", pane,
                            "remain-on-exit").stdout.strip() == "on"
    finally:
        run_tmux("kill-server", check=False)


@pytest.mark.parametrize("case,ready", [
    ("fresh", True), ("stale", False), ("wrong_sid", False),
    ("wrong_cwd", False), ("stale_with_pane", True),
])
def test_app_pull_readiness_requires_fresh_pid_proof(tmp_path, case, ready):
    if not shutil.which("zsh"):
        pytest.skip("zsh required")
    import datetime

    start = "Sun Oct  4 12:00:00 2026"
    born = datetime.datetime.strptime(start, "%a %b %d %H:%M:%S %Y").timestamp()
    cache = tmp_path / "cache" / "claude-sessions"
    cache.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    registry = cache / "12345"
    registry.write_text(
        ("other-thread" if case == "wrong_sid" else SID) + "\t" +
        ("/wrong/cwd" if case == "wrong_cwd" else str(worktree)) + "\n"
    )
    os.utime(registry, (born - 60, born - 60) if case.startswith("stale")
             else (born + 1, born + 1))
    script = f'''
source {shlex.quote(str(ROOT / 'zsh/resume.zsh'))}
ps() {{ print -r -- {shlex.quote(start)}; }}
_codex_pane_sid() {{ [[ {shlex.quote(case)} == stale_with_pane ]] && print -r -- {SID}; }}
_t_app_pull_ready dev-test-1 {shlex.quote(str(worktree))} {SID} 12345
'''
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "PATH": os.environ["PATH"]}
    result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is ready, result.stderr
