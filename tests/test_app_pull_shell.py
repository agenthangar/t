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
        ("unsupported", False, None),
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
    (fake_bin / "codex").write_text("#!/bin/sh\n[ \"$1 $2\" != 'resume --help' ] || "
                                    "{ [ -n \"$T_APP_TEST_NO_DAEMON\" ] || echo --no-daemon; }\nexit 0\n")
    (fake_bin / "codex").chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
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
      [[ "$*" == *'#{{session_name}}|#{{session_path}}'* ]] || return 91
      case $case in
        missing|no_marker) ;;
        alias) print -r -- 'dev-alias|'{shlex.quote(str(worktree))} ;;
        duplicate) print -r -- 'dev-repo-1|'{shlex.quote(str(worktree))}; print -r -- 'dev-alias|'{shlex.quote(str(worktree))} ;;
        other_owner) print -r -- 'dev-repo-1|'{shlex.quote(str(worktree))}; print -r -- 'dev-other-1|/other/worktree' ;;
        *) print -r -- 'dev-repo-1|'{shlex.quote(str(worktree))} ;;
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
_t_app_pull_slot dev-repo-1 {shlex.quote(str(worktree))} {SID} {expected} {shlex.quote(str(codex_home))}
'''
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_STATE_HOME": str(tmp_path / "state"),
           "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"], "TERM": "dumb"}
    if case == "unsupported":
        env["T_APP_TEST_NO_DAEMON"] = "1"
    result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is ok, result.stderr
    recorded = calls.read_text() if calls.exists() else ""
    if launch:
        assert launch in recorded
        assert SID in recorded
        assert "zsh -lic" in recorded
        assert "--no-daemon" in recorded
        assert "CODEX_HOME=" in recorded
    else:
        assert recorded == ""
    assert marker.exists() is (not ok and case != "no_marker")
    if case == "unsupported":
        assert "upgrade Codex" in result.stderr


@pytest.mark.parametrize("mode", ["missing", "dead", "shell", "alias", "no_registry", "no_lock", "failure"])
def test_app_pull_real_tmux_launch(tmp_path, mode):
    """Exercise tmux's actual command parsing, pane lifetime, and window selection."""
    if not all(shutil.which(name) for name in ("tmux", "zsh", "cc")):
        pytest.skip("tmux, zsh, and cc required")
    import time
    import uuid

    worktree = tmp_path / ("work|tree" if mode == "alias" else "work tree")
    worktree.mkdir()
    marker = tmp_path / "t-app-slot"
    marker.write_text("codex-app\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Ubuntu's global zshrc can prompt about the runner's shared completion
    # directories before reading this isolated home's zshrc.
    (tmp_path / ".zshenv").write_text("skip_global_compinit=1\n")
    (tmp_path / ".zshrc").write_text("export PATH=" + shlex.quote(str(fake_bin)) + ":$PATH\n"
                                      "export CODEX_HOME=/wrong/login-shell/home\n")
    source = tmp_path / "codex.c"
    source.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "resume") && !strcmp(argv[2], "--help")) {
        puts("--no-daemon"); return 0;
    }
    char cwd[4096];
    getcwd(cwd, sizeof cwd);
    FILE *f = fopen(getenv("T_APP_TEST_RECORD"), "w");
    if (f) {
        fprintf(f, "%s\n", cwd);
        fprintf(f, "pid=%d\n", getpid());
        fprintf(f, "home=%s\n", getenv("CODEX_HOME"));
        for (int i = 1; i < argc; i++) fprintf(f, "%s\n", argv[i]);
        fclose(f);
    }
    if (getenv("T_APP_TEST_FAIL")) { sleep(8); return 1; }
    char locks[4096], lock_path[4096];
    snprintf(locks, sizeof locks, "%s/thread-writer-locks", getenv("CODEX_HOME"));
    mkdir(locks, 0700);
    snprintf(lock_path, sizeof lock_path, "%s/%s.lock", locks, argv[2]);
    int lock_fd = open(lock_path, O_CREAT | O_RDWR, 0600);
    if (!getenv("T_APP_TEST_NO_LOCK") && (lock_fd < 0 || flock(lock_fd, LOCK_EX | LOCK_NB))) return 1;
    if (getenv("T_APP_TEST_NO_HOOK")) {
        puts("» Ask Codex to do anything");
        fflush(stdout);
        sleep(15);
        return 0;
    }
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
               CODEX_HOME=str(tmp_path / "codex-home"),
               T_APP_TEST_RECORD=str(tmp_path / "launch"),
               TERM="xterm-256color", LANG="C", LC_ALL="C", T_RECOVERY_DISABLE="1",
               PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
    (tmp_path / "cache" / "claude-sessions").mkdir(parents=True)
    if mode == "failure":
        env["T_APP_TEST_FAIL"] = "1"
    if mode == "no_lock":
        env["T_APP_TEST_NO_LOCK"] = "1"
    if mode == "no_registry":
        import json
        import sqlite3

        env["T_APP_TEST_NO_HOOK"] = "1"
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        rollout = codex_home / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": SID, "cwd": str(worktree), "source": "cli"}}) + "\n")
        with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
            conn.execute("create table threads (id text, rollout_path text, cwd text, "
                         "archived integer, first_user_message text, source text, "
                         "name text, updated_at integer)")
            conn.execute("insert into threads values (?,?,?,?,?,?,?,?)",
                         (SID, str(rollout), str(worktree), 0, "prior user prompt", "cli", None, 1))
    else:
        (tmp_path / "codex-home").mkdir(exist_ok=True)
    session = "dev-test-1"

    def run_tmux(*args, check=True):
        return subprocess.run(tmux + list(args), env=env, capture_output=True,
                              text=True, check=check)

    try:
        if mode not in ("missing", "no_registry"):
            existing_session = "dev-alias-1" if mode == "alias" else session
            run_tmux("new-session", "-d", "-s", existing_session, "-c", str(worktree), "sleep 30")
            run_tmux("set-environment", "-t", "=" + existing_session, "CLAUDE_RESUME_ID", SID)
            run_tmux("set-environment", "-t", "=" + existing_session, "DEV_AGENT", "codex")
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
_t_app_pull_slot {session} {shlex.quote(str(worktree))} {SID} {'-' if mode in ('missing', 'no_registry') else SID} {shlex.quote(str(tmp_path / 'codex-home'))}
'''
        result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                capture_output=True, text=True, timeout=15)
        if mode == "alias":
            assert result.returncode != 0
            assert "another tmux session owns this worktree" in result.stderr
            assert marker.exists()
            assert not (tmp_path / "launch").exists()
            assert run_tmux("has-session", "-t", "=dev-alias-1:").returncode == 0
            assert run_tmux("has-session", "-t", "=" + session + ":", check=False).returncode != 0
            return
        if (result.returncode == 0) is (mode in ("failure", "no_lock")):
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
        assert marker.exists() is (mode in ("failure", "no_lock"))
        if mode == "no_registry":
            assert not list((tmp_path / "cache" / "claude-sessions").iterdir())
        if mode == "no_lock":
            registries = list((tmp_path / "cache" / "claude-sessions").iterdir())
            assert len(registries) == 1
            assert registries[0].read_text() == SID + "\t" + str(worktree) + "\n"
        if mode not in ("failure", "no_lock"):
            assert (tmp_path / "launch").exists(), (
                run_tmux("capture-pane", "-p", "-t", "=" + session + ":").stdout,
                run_tmux("show-environment", "-g", "T_APP_TEST_RECORD", check=False).stdout,
                run_tmux("list-panes", "-s", "-t", "=" + session,
                         "-F", "#{pane_dead} #{pane_current_command} #{pane_pid}").stdout)
            record = (tmp_path / "launch").read_text().splitlines()
            assert record[0] == str(worktree)
            assert record[1].startswith("pid=")
            assert record[2] == "home=" + str(tmp_path / "codex-home")
            assert record[3:] == ["resume", SID, "--cd", str(worktree), "--no-daemon"]
            assert run_tmux("show-environment", "-t", "=" + session,
                            "CLAUDE_RESUME_ID").stdout.strip() == "CLAUDE_RESUME_ID=" + SID
            if mode == "missing":
                owner_script = f'''
source {shlex.quote(str(ROOT / 'zsh/agent.zsh'))}
source {shlex.quote(str(ROOT / 'zsh/sessions.zsh'))}
source {shlex.quote(str(ROOT / 'zsh/resume.zsh'))}
tmux() {{ command {shlex.join(tmux)} "$@"; }}
_dev_agent_of_session() {{ print codex; }}
_t_app_pull_owner {session} {shlex.quote(str(worktree))} {SID} {shlex.quote(str(tmp_path / 'codex-home'))}
'''
                owner = subprocess.run(["zsh", "-f", "-c", owner_script], env=env,
                                       capture_output=True, text=True, timeout=5)
                assert owner.returncode == 0 and owner.stdout.strip() == record[1][4:], owner.stderr
                wrong = subprocess.run(["zsh", "-f", "-c", owner_script.replace(
                    shlex.quote(str(worktree)) + " " + SID, shlex.quote(str(tmp_path)) + " " + SID)],
                    env=env, capture_output=True, text=True, timeout=5)
                assert wrong.returncode != 0
            if mode == "shell":
                windows = run_tmux("list-windows", "-t", "=" + session,
                                   "-F", "#{window_active} #{pane_current_command}").stdout.splitlines()
                assert len(windows) == 2
                assert "1 codex" in windows
                # A retry after the CLI starts but before reservation cleanup
                # must find the existing owner through the real tmux formatter.
                marker.write_text("codex-app\n")
                retry = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                       capture_output=True, text=True, timeout=15)
                assert retry.returncode == 0, retry.stderr
                assert not marker.exists()
                assert len(run_tmux("list-windows", "-t", "=" + session,
                                    "-F", "#{window_id}").stdout.splitlines()) == 2
                assert (tmp_path / "launch").read_text().splitlines()[1] == record[1]
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
_t_app_pull_lock_owned() {{ :; }}
_codex_pane_sid() {{ [[ {shlex.quote(case)} == stale_with_pane ]] && print -r -- {SID}; }}
_t_app_pull_ready dev-test-1 {shlex.quote(str(worktree))} {SID} 12345
'''
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "PATH": os.environ["PATH"]}
    result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is ready, result.stderr


def test_app_pull_lock_proves_exact_live_pid_and_rejects_symlink(tmp_path):
    if not all(shutil.which(name) for name in ("zsh", "lsof")):
        pytest.skip("zsh and lsof required")
    import sys

    codex_home = tmp_path / "codex home"
    locks = codex_home / "thread-writer-locks"
    locks.mkdir(parents=True)
    held = locks / (SID + ".lock")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'w+'); "
         "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read(1)", str(held)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=tmp_path,
        env={key: os.environ[key] for key in ("PATH", "TERM", "LANG", "LC_ALL") if key in os.environ},
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        script = ("source " + shlex.quote(str(ROOT / "zsh/resume.zsh")) + "; "
                  "_t_app_pull_lock_owned " + str(holder.pid) + " " + SID + " "
                  + shlex.quote(str(codex_home)))
        env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
        good = subprocess.run(["zsh", "-f", "-c", script], env=env,
                              capture_output=True, text=True, timeout=5)
        assert good.returncode == 0, good.stderr
        wrong_pid = subprocess.run(["zsh", "-f", "-c", script.replace(
            "_t_app_pull_lock_owned " + str(holder.pid), "_t_app_pull_lock_owned " + str(os.getpid()))],
            env=env, capture_output=True, text=True, timeout=5)
        assert wrong_pid.returncode != 0
        held.rename(locks / "held.lock")
        held.symlink_to(locks / "held.lock")
        symlink = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                 capture_output=True, text=True, timeout=5)
        assert symlink.returncode != 0
        held.unlink()
        held.touch()
        unlocked = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                  capture_output=True, text=True, timeout=5)
        assert unlocked.returncode != 0
    finally:
        holder.communicate(input="\n", timeout=3)


@pytest.mark.parametrize("case,ready", [
    ("unnamed_old_thread", True),
    ("wrong_sid", False),
    ("wrong_cwd", False),
    ("error_screen", False),
    ("error_mentions_prompt", False),
    ("wrong_rollout", False),
])
def test_app_pull_hook_free_ready_needs_exact_process_thread_and_loaded_ui(tmp_path, case, ready):
    """An idle resumed thread need not update SQLite or have a hook or a name."""
    import json
    import sqlite3

    if not shutil.which("zsh"):
        pytest.skip("zsh required")
    worktree = tmp_path / "work tree"
    worktree.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" if case == "wrong_rollout" else SID,
        "cwd": str(worktree), "source": "cli"}}) + "\n")
    db = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table threads (id text, rollout_path text, cwd text, "
                     "archived integer, first_user_message text, source text, "
                     "name text, updated_at integer)")
        conn.execute("insert into threads values (?,?,?,?,?,?,?,?)",
                     (SID, str(rollout), str(worktree), 0, "prior user prompt", "cli",
                      None, 1))  # Older than the current process, and unnamed.
    asked_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" if case == "wrong_sid" else SID
    asked_cwd = "/wrong/worktree" if case == "wrong_cwd" else str(worktree)
    pane = ("Codex startup failed" if case == "error_screen" else
            "Error: Ask Codex to do anything after restarting" if case == "error_mentions_prompt"
            else "» Ask Codex to do anything")
    script = f'''
source {shlex.quote(str(ROOT / 'zsh/resume.zsh'))}
_t_app_pull_lock_owned() {{ :; }}
_codex_pane_sid() {{ :; }}
ps() {{ print -r -- {shlex.quote('codex resume ' + asked_sid + ' --cd ' + asked_cwd + ' --no-daemon')}; }}
tmux() {{ [[ $1 == capture-pane ]] && print -r -- {shlex.quote(pane)}; }}
_t_app_pull_ready dev-test-1 {shlex.quote(str(worktree))} {SID} 12345 {shlex.quote(str(tmp_path))}
'''
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "PATH": os.environ["PATH"]}
    result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is ready, result.stderr
