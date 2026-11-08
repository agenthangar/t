"""Public session commands across isolated login shells and a private tmux server.

The app delivery and agent processes are fakes. Git worktrees, shell routing,
desktop reservations, and tmux ownership use the production plugin.
"""

import json
import os
from pathlib import Path
import pty
import runpy
import select
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
    # Pin the checkout under test; an installed t earlier on the developer's
    # PATH otherwise makes the plugin select canonical main in this sandbox.
    (bindir / "t").symlink_to(ROOT / "bin/t")
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
    env.update(HOME=str(home), ZDOTDIR=str(home), SHELL=shutil.which("zsh"),
               XDG_CONFIG_HOME=str(base / "config"),
               XDG_CACHE_HOME=str(base / "cache"),
               XDG_STATE_HOME=str(base / "state"),
               XDG_DATA_HOME=str(base / "data"),
               CODEX_HOME=str(base / "codex"), TMUX_TMPDIR=str(base / "tmux"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               T_NO_AUTORELOAD="1", T_NO_UPDATE_CHECK="1",
               T_RECOVERY_DISABLE="1", T_AUTO_TRUST="0",
               T_TEST_BIN=str(bindir), T_TEST_WT=str(base / "worktrees" / "repo" / "repo-1"),
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
  const char *seconds = getenv("T_TEST_AGENT_SECONDS");
  sleep(seconds ? atoi(seconds) : 60);
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
        # Drain while the command runs: a progress rail can fill the PTY buffer
        # and block the child if the parent waits for exit before reading.
        chunks = []
        deadline = time.monotonic() + 25
        while True:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            if time.monotonic() >= deadline:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("isolated remote command did not exit: " +
                            b"".join(chunks).decode(errors="replace"))
            if process.poll() is not None and not select.select([master], [], [], 0)[0]:
                break
        process.wait(timeout=5)
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


@pytest.mark.parametrize("leaf", ["1", "repo-1"])
@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_public_cli_open_and_reattach_keep_one_owner(lifecycle, agent, leaf):
    base, env = lifecycle
    worktree = base / "worktrees" / "repo" / leaf
    if leaf == "1":
        result = _run(["git", "-C", str(base / "repo"), "worktree", "add", "-b", "dev/repo-1", str(worktree), "origin/main"], env=env, cwd=base)
        assert result.returncode == 0, result.stderr
    first = _shell(base, env, "t session open repo 1 --" + agent)
    assert first.returncode == 0, first.stderr or first.stdout
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
    worktree = base / "worktrees" / "repo" / "repo-1"
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
def test_public_list_keeps_live_owner_and_worktree_unchanged(lifecycle, agent):
    base, env = lifecycle
    opened = _shell(base, env, "t session open repo 1 --" + agent)
    assert opened.returncode == 0, opened.stderr or opened.stdout
    log = base / "agents.log"
    launches = _until(lambda: log.read_text().splitlines() if log.exists() else None)
    worktree = base / "worktrees/repo/repo-1"
    marker = worktree / "unfinished.txt"
    marker.write_text("keep these edits\n")
    before = _run(["git", "-C", str(worktree), "status", "--porcelain"], env=env, cwd=base)
    listed = _shell(base, env, "t session list repo")
    assert listed.returncode == 0, listed.stderr or listed.stdout
    assert "repo-1" in listed.stdout
    assert log.read_text().splitlines() == launches
    assert marker.read_text() == "keep these edits\n"
    after = _run(["git", "-C", str(worktree), "status", "--porcelain"], env=env, cwd=base)
    assert before.returncode == after.returncode == 0
    assert before.stdout == after.stdout
    alive = _run([str(base / "bin/tmux"), "has-session", "-t", "=dev-repo-1:"], env=env, cwd=base)
    assert alive.returncode == 0, alive.stderr


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_public_close_stops_owner_and_preserves_uncommitted_work(lifecycle, agent):
    base, env = lifecycle
    opened = _shell(base, env, "t session open repo 1 --" + agent)
    assert opened.returncode == 0, opened.stderr or opened.stdout
    log = base / "agents.log"
    launches = _until(lambda: log.read_text().splitlines() if log.exists() else None)
    worktree = base / "worktrees/repo/repo-1"
    marker = worktree / "unfinished.txt"
    marker.write_text("keep these edits\n")
    head = _run(["git", "-C", str(worktree), "rev-parse", "HEAD"], env=env, cwd=base)
    closed = _shell(base, env, "t session close repo 1 -y")
    assert closed.returncode == 0, closed.stderr or closed.stdout
    alive = _run([str(base / "bin/tmux"), "has-session", "-t", "=dev-repo-1:"], env=env, cwd=base)
    assert alive.returncode != 0
    assert marker.read_text() == "keep these edits\n"
    assert (worktree / ".git").exists()
    after = _run(["git", "-C", str(worktree), "rev-parse", "HEAD"], env=env, cwd=base)
    assert head.returncode == after.returncode == 0 and head.stdout == after.stdout
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


@pytest.mark.parametrize("leaf", ["1", "repo-1"])
@pytest.mark.parametrize("swept", [False, True])
@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_public_resume_revives_saved_thread_once(lifecycle, agent, leaf, swept):
    base, env = lifecycle
    repo = base / "repo"
    worktree = base / "worktrees" / "repo" / leaf
    worktree.parent.mkdir(parents=True)
    checkout = _run(["git", "-C", str(repo), "worktree", "add", "-q", "-b",
                     "dev/repo-1", str(worktree), "origin/main"], env=env, cwd=base)
    assert checkout.returncode == 0, checkout.stderr
    if agent == "codex":
        codex_home = base / "codex"
        (codex_home / "thread-writer-locks").mkdir()
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
        transcript.write_text(json.dumps({"type": "user", "cwd": str(worktree),
            "sessionId": SID, "message": {
            "role": "user", "content": "Resume my saved work"}},
            separators=(",", ":")) + "\n")
    # Real lifecycle hooks retain the conversation's revision and workspace
    # generation. Without that evidence safe history resume must ask before
    # choosing a revision, rather than borrowing today's slot branch.
    recorded = _shell(base, env, f'_dev_history_record {SID} {agent} {shlex.quote(str(worktree))} resume')
    assert recorded.returncode == 0, recorded.stderr
    if swept:
        removed = _run(["git", "-C", str(repo), "worktree", "remove", str(worktree)], env=env, cwd=base)
        assert removed.returncode == 0, removed.stderr
    (base / "cache" / "claude-sessions").mkdir(parents=True, exist_ok=True)
    listed = _shell(base, env, "t session resume repo 1")
    assert listed.returncode == 0, listed.stderr
    assert "1 conversations" in listed.stdout
    assert not (base / "agents.log").exists()
    resumed = _shell(base, env, "t session resume repo 1 --cli")
    assert resumed.returncode == 0, resumed.stderr
    log = base / "agents.log"
    launches = _until(lambda: log.read_text().splitlines() if log.exists() else None)
    assert len(launches) == 1
    destination = base / "worktrees/repo/repo-2" if swept else worktree
    expected = "|resume|" + SID + "|--cd|" + str(destination) + "|--no-daemon" if agent == "codex" else "|-r|" + SID
    assert launches[0].endswith(expected)
    assert str(destination) in launches[0]
    assert destination.is_dir()
    opened = _shell(base, env, f"t session open repo {2 if swept else 1}")
    assert opened.returncode == 0, opened.stderr
    assert log.read_text().splitlines() == launches
    assert transcript.is_file()
