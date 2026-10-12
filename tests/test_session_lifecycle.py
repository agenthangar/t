"""Public session commands across isolated login shells and a private tmux server.

The app delivery and agent processes are fakes. Git worktrees, shell routing,
desktop reservations, and tmux ownership use the production plugin.
"""

import json
import os
from pathlib import Path
import pty
import runpy
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
SID = "12345678-1234-1234-1234-123456789abc"
UNINDEXED = ("22345678-1234-1234-1234-123456789abc",
             "32345678-1234-1234-1234-123456789abc")


def _run(argv, *, env, cwd, timeout=20):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


@pytest.fixture
def lifecycle(tmp_path):
    if not all(shutil.which(tool) for tool in ("zsh", "tmux", "git", "cc")):
        pytest.skip("zsh, tmux, git, and cc are required")
    base = tmp_path.resolve()
    home = base / "home"
    bindir = base / "bin"
    remote = base / "origin.git"
    repo = base / "repo"
    for directory in (home, bindir, base / "tmux", base / "codex"):
        directory.mkdir()
    real_tmux = shutil.which("tmux")
    # Unix socket paths have a small kernel length limit on macOS. Use a unique
    # short /tmp name while keeping all server contents in the isolated HOME.
    socket = Path("/tmp") / ("t-life-" + uuid.uuid4().hex[:12] + ".sock")
    tmux = bindir / "tmux"
    tmux.write_text("#!/bin/sh\n" +
                    "case \"$1\" in attach-session) shift; exec " +
                    shlex.quote(real_tmux) + " -S " + shlex.quote(str(socket)) +
                    " -f /dev/null has-session \"$@\" ;; esac\n" +
                    "exec " + shlex.quote(real_tmux) + " -S " +
                    shlex.quote(str(socket)) + " -f /dev/null \"$@\"\n")
    tmux.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COV_CORE_") and
           key not in ("TMUX", "TMUX_PANE", "T_LOCAL_RC", "CLAUDE_RESUME_ID",
                       "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")}
    env.update(HOME=str(home), ZDOTDIR=str(home),
               XDG_CONFIG_HOME=str(base / "config"),
               XDG_CACHE_HOME=str(base / "cache"),
               XDG_STATE_HOME=str(base / "state"),
               XDG_DATA_HOME=str(base / "data"),
               CODEX_HOME=str(base / "codex"), TMUX_TMPDIR=str(base / "tmux"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               T_NO_AUTORELOAD="1", T_NO_UPDATE_CHECK="1",
               T_RECOVERY_DISABLE="1", T_AUTO_TRUST="0",
               T_TEST_BIN=str(bindir), T_TEST_WT=str(base / "worktrees" / "repo" / "1"),
               T_TEST_AGENT_LOG=str(base / "agents.log"),
               T_TEST_APP_LOG=str(base / "app.log"),
               PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    (home / ".zshenv").write_text("skip_global_compinit=1\n")
    (home / ".zshrc").write_text("export PATH=" + shlex.quote(env["PATH"]) + "\n" +
                                  "source " + shlex.quote(str(ROOT / "t.plugin.zsh")) + "\n")
    config = base / "config" / "t"
    config.mkdir(parents=True)
    (config / "local.zsh").write_text(
        "DEV_REPOS[repo]=" + shlex.quote(str(repo)) + "\n" +
        "DEV_WORKTREE_ROOT=" + shlex.quote(str(base / "worktrees")) + "\n" +
        "DEV_AGENT_DEFAULT=codex\n")
    source = base / "agent.c"
    source.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc == 3 && !strcmp(argv[1], "resume") && !strcmp(argv[2], "--help")) {
    puts("--no-daemon"); return 0;
  }
  FILE *f = fopen(getenv("T_TEST_AGENT_LOG"), "a");
  if (!f) return 2;
  char cwd[4096];
  if (!getcwd(cwd, sizeof cwd)) return 2;
  fprintf(f, "%s|%s", argv[0], cwd);
  for (int i = 1; i < argc; i++) fprintf(f, "|%s", argv[i]);
  fputc('\n', f);
  fclose(f);
  const char *sid = argc >= 3 && !strcmp(argv[1], "resume") ? argv[2] :
                    (strstr(argv[0], "/codex") ? getenv("T_TEST_FRESH_SID") : NULL);
  if (sid && *sid) {
    char locks[4096], lock[4096], registry[4096];
    snprintf(locks, sizeof locks, "%s/thread-writer-locks", getenv("CODEX_HOME"));
    mkdir(locks, 0700);
    snprintf(lock, sizeof lock, "%s/%s.lock", locks, sid);
    int fd = open(lock, O_CREAT | O_RDWR, 0600);
    if (fd < 0 || flock(fd, LOCK_EX | LOCK_NB)) return 3;
    snprintf(registry, sizeof registry, "%s/claude-sessions/%d",
             getenv("XDG_CACHE_HOME"), getpid());
    f = fopen(registry, "w");
    if (f) { fprintf(f, "%s\t%s\n", sid, cwd); fclose(f); }
  }
  sleep(60);
  return 0;
}
''')
    for agent in ("codex", "claude"):
        compiled = _run(["cc", str(source), "-o", str(bindir / agent)], env=env, cwd=base)
        assert compiled.returncode == 0, compiled.stderr
    for argv in (["git", "init", "-q", "--bare", str(remote)],
                 ["git", "clone", "-q", str(remote), str(repo)]):
        completed = _run(argv, env=env, cwd=base)
        assert completed.returncode == 0, completed.stderr
    (repo / "README.md").write_text("disposable lifecycle repository\n")
    for argv in (["git", "-C", str(repo), "add", "README.md"],
                 ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                  "user.email=test@example.invalid", "commit", "-qm", "initial"],
                 ["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"],
                 ["git", "-C", str(repo), "fetch", "-q", "origin", "main"]):
        completed = _run(argv, env=env, cwd=base)
        assert completed.returncode == 0, completed.stderr
    try:
        yield base, env
    finally:
        _run([str(tmux), "kill-server"], env=env, cwd=base, timeout=5)
        socket.unlink(missing_ok=True)


def _shell(base, env, script, *, cwd=None):
    return _run(["zsh", "-lic", script], env=env, cwd=cwd or base, timeout=25)


def _shell_tty(base, env, script):
    argv = ["zsh", "-lic", script]
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(argv, env=env, cwd=base, stdin=slave,
                                   stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = -1
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("isolated remote command did not exit")
        chunks = []
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return subprocess.CompletedProcess(argv, process.returncode,
                                           b"".join(chunks).decode(errors="replace"), "")
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)


def _until(predicate, *, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    pytest.fail("isolated agent did not reach expected state")


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_public_cli_open_and_reattach_keep_one_owner(lifecycle, agent):
    base, env = lifecycle
    first = _shell(base, env, "t session open repo 1 --" + agent)
    assert first.returncode == 0, first.stderr or first.stdout
    worktree = base / "worktrees" / "repo" / "1"
    log = base / "agents.log"
    _until(lambda: log.exists() and log.read_text().strip())
    launches = log.read_text().splitlines()
    assert len(launches) == 1 and str(worktree) in launches[0]
    assert Path(worktree / ".git").exists()
    second = _shell(base, env, "t session open repo 1")
    assert second.returncode == 0, second.stderr
    assert log.read_text().splitlines() == launches
    sessions = _run([str(base / "bin" / "tmux"), "list-sessions", "-F",
                     "#{session_name}"], env=env, cwd=base)
    assert sessions.stdout.splitlines() == ["dev-repo-1"]


def test_concurrent_alias_opens_keep_one_worktree_owner(lifecycle):
    base, env = lifecycle
    with (base / "config" / "t" / "local.zsh").open("a") as config:
        config.write("DEV_REPOS[alias]=$DEV_REPOS[repo]\n")
    commands = ("t session open repo 1 --codex --cli",
                "t session open alias 1 --codex --cli")
    processes = [subprocess.Popen(["zsh", "-lic", command], env=env, cwd=base,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for command in commands]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=25)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert any(rc == 0 for rc, _, _ in results), results
    worktree = base / "worktrees" / "repo" / "1"
    assert worktree.is_dir()
    log = base / "agents.log"
    launches = _until(lambda: log.read_text().splitlines() if log.exists() else None)
    assert len(launches) == 1, (results, launches)
    assert str(worktree) in launches[0]
    sessions = _run([str(base / "bin" / "tmux"), "list-sessions", "-F",
                     "#{session_name}:#{session_path}"], env=env, cwd=base)
    assert sessions.returncode == 0, sessions.stderr
    rows = sessions.stdout.splitlines()
    assert len(rows) == 1 and rows[0].endswith(":" + str(worktree)), (results, rows)
    attached = _shell(base, env, "t session open alias 1 --cli")
    assert attached.returncode == 0, attached.stderr
    assert log.read_text().splitlines() == launches


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_public_remote_open_uses_login_shell_and_reattaches(lifecycle, agent):
    base, env = lifecycle
    config = base / "config" / "t" / "local.zsh"
    with config.open("a") as file:
        file.write("REMOTE_HOSTS[mini]=test.invalid\n")
    ssh = base / "bin" / "ssh"
    ssh.write_text("#!/bin/sh\n" +
                   "printf '%s\\n' \"$*\" >> \"$T_TEST_SSH_LOG\"\n" +
                   "case \"$1\" in -t) shift ;; -o) shift 4 ;; *) exit 90 ;; esac\n" +
                   "[ \"$1\" = test.invalid ] || exit 91; shift\n" +
                   "exec /bin/sh -c \"$1\"\n")
    ssh.chmod(0o755)
    env["T_TEST_SSH_LOG"] = str(base / "ssh.log")
    first = _shell_tty(base, env, "t session open repo --new --host mini --" + agent)
    assert first.returncode == 0, first.stderr or first.stdout
    log = base / "agents.log"
    launches = _until(lambda: log.read_text().splitlines() if log.exists() else None)
    assert len(launches) == 1 and agent in launches[0]
    second = _shell_tty(base, env, "t session open repo 1 --host mini")
    assert second.returncode == 0, second.stderr
    assert log.read_text().splitlines() == launches
    calls = [call for call in (base / "ssh.log").read_text().splitlines()
             if call.startswith("-t ")]
    assert len(calls) == 2
    assert all(call.startswith("-t test.invalid zsh -lic ") for call in calls)
    assert all("--cli" in call for call in calls)


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_public_resume_revives_saved_thread_once(lifecycle, agent):
    base, env = lifecycle
    repo = base / "repo"
    worktree = base / "worktrees" / "repo" / "1"
    worktree.parent.mkdir(parents=True)
    checkout = _run(["git", "-C", str(repo), "worktree", "add", "-q", "-b",
                     "dev/repo-1", str(worktree), "origin/main"], env=env, cwd=base)
    assert checkout.returncode == 0, checkout.stderr
    if agent == "codex":
        codex_home = base / "codex"
        transcript = codex_home / ("rollout-2026-10-11T00-00-00-" + SID + ".jsonl")
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": SID, "cwd": str(worktree), "source": "cli", "cli_version": "0.160.0"}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"type": "message",
                        "role": "user", "content": [{"type": "input_text",
                        "text": "Resume my saved work"}]}}) + "\n")
        with sqlite3.connect(codex_home / "state_5.sqlite") as db:
            db.execute("create table threads (id text, rollout_path text, cwd text, "
                       "name text, title text, updated_at integer, archived integer, "
                       "source text, first_user_message text)")
            db.execute("insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (SID, str(transcript), str(worktree), None, "Saved work", 1, 0,
                        "cli", "Resume my saved work"))
    else:
        project = base / "home" / ".claude" / "projects" / "".join(
            char if char.isalnum() else "-" for char in str(worktree))
        project.mkdir(parents=True)
        transcript = project / (SID + ".jsonl")
        transcript.write_text(json.dumps({"type": "user", "message": {
            "role": "user", "content": "Resume my saved work"}},
            separators=(",", ":")) + "\n")
    (base / "cache" / "claude-sessions").mkdir(parents=True)
    resumed = _shell(base, env, "t session resume repo 1")
    assert resumed.returncode == 0, resumed.stderr
    log = base / "agents.log"
    launches = _until(lambda: log.read_text().splitlines() if log.exists() else None)
    assert len(launches) == 1
    assert launches[0].endswith(("|resume|" if agent == "codex" else "|-r|") + SID)
    assert str(worktree) in launches[0]
    opened = _shell(base, env, "t session open repo 1")
    assert opened.returncode == 0, opened.stderr
    assert log.read_text().splitlines() == launches
    assert transcript.is_file()


def _install_app_bridge(base):
    bindir = base / "bin"
    bridge = bindir / "t"
    bridge.write_text("#!/bin/sh\n" +
                      "case \"$1\" in\n" +
                      "  _app-workspace) printf 'workspace|%s|%s\\n' \"$2\" \"$3\" >> \"$T_TEST_APP_LOG\"; exit 0 ;;\n" +
                      "  _app-open-cli)\n" +
                      "    gitdir=$(git -C \"$T_TEST_WT\" rev-parse --absolute-git-dir) || exit 1\n" +
                      "    tmux new-session -d -s dev-repo-1 -c \"$T_TEST_WT\" \"$T_TEST_BIN/codex\" resume " + SID + " || exit 1\n" +
                      "    tmux set-environment -t =dev-repo-1 CLAUDE_RESUME_ID " + SID + " || exit 1\n" +
                      "    tmux set-environment -t =dev-repo-1 DEV_AGENT codex || exit 1\n" +
                      "    rm \"$gitdir/t-app-slot\" || exit 1\n" +
                      "    printf 'pull|%s|%s\\n' \"$2\" \"$3\" >> \"$T_TEST_APP_LOG\"\n" +
                      "    printf 'dev-repo-1\\n'; exit 0 ;;\n" +
                      "  app)\n" +
                      "    gitdir=$(git -C \"$T_TEST_WT\" rev-parse --absolute-git-dir) || exit 1\n" +
                      "    tmux show-environment -t =dev-repo-1 CLAUDE_RESUME_ID >> \"$T_TEST_APP_LOG\" || exit 1\n" +
                      "    tmux kill-session -t =dev-repo-1 || exit 1\n" +
                      "    printf 'codex-app\\n' > \"$gitdir/t-app-slot\"\n" +
                      "    printf 'push|%s|%s\\n' \"$2\" \"$3\" >> \"$T_TEST_APP_LOG\"\n" +
                      "    exit 0 ;;\n" +
                      "esac\n" +
                      "exec " + shlex.quote(sys.executable) + " " +
                      shlex.quote(str(ROOT / "bin" / "t")) + " \"$@\"\n")
    bridge.chmod(0o755)


def _install_production_bridge(base):
    """Load production bin/t, replacing only native Codex app delivery."""
    bridge = base / "bin" / "t"
    bridge.write_text("#!/usr/bin/env python3\n" +
        "import importlib.util, os, subprocess, sys\n" +
        "from importlib.machinery import SourceFileLoader\n" +
        "path = " + repr(str(ROOT / "bin" / "t")) + "\n" +
        "loader = SourceFileLoader('t_lifecycle_bin', path)\n" +
        "spec = importlib.util.spec_from_loader('t_lifecycle_bin', loader)\n" +
        "mod = importlib.util.module_from_spec(spec)\n" +
        "sys.modules[spec.name] = mod\n" +
        "loader.exec_module(mod)\n" +
        "mod._app_bundle = lambda: '/tmp/disposable-codex.app'\n" +
        "class Window:\n    def focus(self): pass\n" +
        "mod._app_new_window = lambda bundle: Window()\n" +
        "real_run = mod._run\n" +
        "def run(argv, **kwargs):\n" +
        "    if argv and argv[0] == 'open':\n" +
        "        with open(os.environ['T_TEST_APP_LOG'], 'a') as out:\n" +
        "            out.write('native|' + '|'.join(argv[3:]) + '\\n')\n" +
        "        return subprocess.CompletedProcess(argv, 0, '', '')\n" +
        "    return real_run(argv, **kwargs)\n" +
        "mod._run = run\n" +
        "sys.exit(mod.main(sys.argv[1:]))\n")
    bridge.chmod(0o755)


@pytest.mark.skipif(sys.platform != "darwin", reason="app opening requires macOS")
def test_public_app_cli_app_round_trip_keeps_worktree_and_thread(lifecycle):
    base, env = lifecycle
    _install_app_bridge(base)
    worktree = base / "worktrees" / "repo" / "1"
    fresh = _shell(base, env, "t session open repo 1 --app")
    assert fresh.returncode == 0, fresh.stderr
    gitdir = _run(["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
                  env=env, cwd=base).stdout.strip()
    marker = Path(gitdir) / "t-app-slot"
    assert marker.read_text() == "codex-app\n"
    assert not (base / "agents.log").exists()
    history = base / "codex" / "history.jsonl"
    history.write_text(json.dumps({"thread": SID, "cwd": str(worktree),
                                   "message": "Keep this work"}) + "\n")
    # Reproduce the reported failure alongside this round trip: two unrelated
    # desktop backends hold locks with no local state DB row. The production
    # ownership helper must allow a target-scoped open, while destructive close
    # retains its strict unknown-owner check.
    rollout = base / "codex" / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(worktree), "cli_version": "0.160.0"}}) + "\n")
    locks = base / "codex" / "thread-writer-locks"
    locks.mkdir()
    (locks / (SID + ".lock")).touch()
    paths = [locks / (other + ".lock") for other in UNINDEXED]
    for path in paths:
        path.touch()
    with sqlite3.connect(base / "codex" / "state_5.sqlite") as db:
        db.execute("create table threads (id text, cwd text)")
        db.execute("insert into threads values (?, ?)", (SID, str(worktree)))
    holder = subprocess.Popen([sys.executable, "-c",
        "import fcntl,sys; files=[open(p,'r') for p in sys.argv[1:]]; "
        "[fcntl.flock(f,fcntl.LOCK_EX) for f in files]; "
        "print('ready',flush=True); sys.stdin.read()", *map(str, paths)],
        env=env, cwd=base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        handoff = runpy.run_path(str(ROOT / "libexec" / "t_app_handoff.py"))
        options = dict(codex_home=str(base / "codex"), rollout=str(rollout))
        unknown = set()
        fake_run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "")
        handoff["assert_released"](None, SID, str(worktree), fake_run,
                                   **options, unknown=unknown)
        assert unknown == set(UNINDEXED)
        assert handoff["preflight_cli_release"](SID, str(worktree),
                                                **options, unknown=set()) == "modern"
        with pytest.raises(ValueError, match="could not be matched to its worktree"):
            handoff["assert_released"](None, SID, str(worktree), fake_run, **options)
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
        holder.stdout.close()
        holder.stderr.close()
    for cycle in range(2):
        pulled = _shell(base, env, "t session open repo 1 --cli")
        assert pulled.returncode == 0, pulled.stderr
        assert not marker.exists()
        launches = _until(lambda: (base / "agents.log").read_text().splitlines()
                          if (base / "agents.log").exists() and
                          len((base / "agents.log").read_text().splitlines()) == cycle + 1
                          else None)
        assert launches[-1].endswith("|resume|" + SID)
        assert str(worktree) in launches[-1]
        pushed = _shell(base, env, "t session open repo 1 --app")
        assert pushed.returncode == 0, pushed.stderr
        assert marker.read_text() == "codex-app\n"
        reopened = _shell(base, env, "t session open repo 1")
        assert reopened.returncode == 0, reopened.stderr
        assert marker.read_text() == "codex-app\n"
        assert (base / "agents.log").read_text().splitlines() == launches
        assert json.loads(history.read_text())["thread"] == SID
        assert worktree.is_dir()
    assert (base / "app.log").read_text().splitlines() == [
        f"workspace|{worktree}|", "pull|repo|1", f"CLAUDE_RESUME_ID={SID}",
        "push|repo|1", f"workspace|{worktree}|--reopen", "pull|repo|1",
        f"CLAUDE_RESUME_ID={SID}", "push|repo|1",
        f"workspace|{worktree}|--reopen"]


@pytest.mark.skipif(sys.platform != "darwin", reason="app opening requires macOS")
def test_public_cli_first_sends_exact_thread_to_app(lifecycle):
    base, env = lifecycle
    _install_app_bridge(base)
    started = _shell(base, env, "t session open repo 1 --codex")
    assert started.returncode == 0, started.stderr
    worktree = base / "worktrees" / "repo" / "1"
    _until(lambda: (base / "agents.log").is_file())
    stamped = _run([str(base / "bin" / "tmux"), "set-environment", "-t",
                    "=dev-repo-1", "CLAUDE_RESUME_ID", SID], env=env, cwd=base)
    assert stamped.returncode == 0, stamped.stderr
    history = base / "codex" / "history.jsonl"
    history.write_text(json.dumps({"thread": SID, "cwd": str(worktree),
                                   "message": "Started in the CLI"}) + "\n")
    sent = _shell(base, env, "t session open repo 1 --app")
    assert sent.returncode == 0, sent.stderr
    gitdir = _run(["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
                  env=env, cwd=base).stdout.strip()
    assert (Path(gitdir) / "t-app-slot").read_text() == "codex-app\n"
    assert (base / "app.log").read_text().splitlines() == [
        f"CLAUDE_RESUME_ID={SID}", "push|repo|1"]
    assert json.loads(history.read_text())["thread"] == SID
    assert _run([str(base / "bin" / "tmux"), "has-session", "-t", "=dev-repo-1:"],
                env=env, cwd=base).returncode != 0


@pytest.mark.skipif(sys.platform != "darwin", reason="app opening requires macOS")
@pytest.mark.parametrize("first_mode", ["app", "cli"])
def test_production_app_coordinator_round_trip_with_unindexed_writers(lifecycle, first_mode):
    base, env = lifecycle
    _install_production_bridge(base)
    worktree = base / "worktrees" / "repo" / "1"
    if first_mode == "app":
        with (base / "config" / "t" / "local.zsh").open("a") as config:
            config.write("DEV_OPEN_MODE_DEFAULT=app\n")
        opened = _shell(base, env, "t session open repo 1")
    else:
        (base / "cache" / "claude-sessions").mkdir(parents=True)
        env["T_TEST_FRESH_SID"] = SID
        opened = _shell(base, env, "t session open repo 1 --codex --cli")
    assert opened.returncode == 0, opened.stderr
    gitdir = _run(["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
                  env=env, cwd=base).stdout.strip()
    marker = Path(gitdir) / "t-app-slot"
    if first_mode == "app":
        assert marker.read_text() == "codex-app\n"
    else:
        assert not marker.exists()
        _until(lambda: (base / "agents.log").is_file())
        stamped = _run([str(base / "bin" / "tmux"), "set-environment", "-t",
                        "=dev-repo-1", "CLAUDE_RESUME_ID", SID], env=env, cwd=base)
        assert stamped.returncode == 0, stamped.stderr
    codex_home = base / "codex"
    rollout = codex_home / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(worktree), "source": "cli", "cli_version": "0.160.0"}}) + "\n" +
        json.dumps({"type": "response_item", "payload": {"type": "message",
                    "role": "user", "content": [{"type": "input_text",
                    "text": "Keep the original conversation"}]}}) + "\n")
    with sqlite3.connect(codex_home / "state_5.sqlite") as db:
        db.execute("create table threads (id text, rollout_path text, cwd text, "
                   "name text, title text, updated_at integer, archived integer, "
                   "source text, first_user_message text)")
        db.execute("insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (SID, str(rollout), str(worktree), None, "Original task", 1, 0,
                    "cli", "Keep the original conversation"))
    locks = codex_home / "thread-writer-locks"
    locks.mkdir(exist_ok=True)
    (locks / (SID + ".lock")).touch()
    other_locks = [locks / (other + ".lock") for other in UNINDEXED]
    for path in other_locks:
        path.touch()
    holder = subprocess.Popen([sys.executable, "-c",
        "import fcntl,sys; files=[open(p,'r') for p in sys.argv[1:]]; "
        "[fcntl.flock(f,fcntl.LOCK_EX) for f in files]; "
        "print('ready',flush=True); sys.stdin.read()", *map(str, other_locks)],
        env=env, cwd=base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        if first_mode == "cli":
            pushed_first = _shell(base, env, "t session open repo 1 --app")
            assert pushed_first.returncode == 0, pushed_first.stderr
            assert "other held Codex writer lock(s)" in pushed_first.stderr
            assert marker.read_text() == "codex-app\n"
            assert (base / "agents.log").read_text().count(str(worktree)) == 1
        pulled = _shell(base, env, "t session open repo 1 --cli")
        assert pulled.returncode == 0, pulled.stderr
        assert "other held Codex writer lock(s)" in pulled.stderr
        assert not marker.exists()
        agent_log = base / "agents.log"
        _until(lambda: agent_log.is_file() and "|resume|" + SID in agent_log.read_text())
        assert str(worktree) in agent_log.read_text()
        pushed = _shell(base, env, "t session open repo 1 --app")
        assert pushed.returncode == 0, pushed.stderr
        assert "other held Codex writer lock(s)" in pushed.stderr
        assert marker.read_text() == "codex-app\n"
        assert rollout.read_text().count("Keep the original conversation") == 1
        native = (base / "app.log").read_text().splitlines()
        if first_mode == "app":
            assert native[0].startswith("native|codex://threads/new?mode=codex&path=")
        else:
            assert native[0] == "native|codex://threads/" + SID
        assert native[-1] == "native|codex://threads/" + SID
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
        holder.stdout.close()
        holder.stderr.close()
