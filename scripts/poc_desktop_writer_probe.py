#!/usr/bin/env python3
"""Probe Codex per-rollout writer exclusion in a disposable Codex home.

Use --codex to select 0.159/0.160. This does not start a turn or touch real
sessions. Distinct stdio app-servers stand in for independent GUI/CLI backends.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
import pty
from pathlib import Path
import select
import shutil
import subprocess
import struct
import tempfile
import termios
import time
import uuid

from poc_desktop_duplicate_owner import RPC
from poc_desktop_shared_backend import stop


def _try_lock(path, method):
    # Codex removes this file when a writer releases the conversation. Opening
    # with a+ would recreate it and falsely report that it already existed.
    try:
        file = path.open("r+")
    except FileNotFoundError:
        return "absent"
    with file:
        try:
            if method == "flock":
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            else:
                fcntl.lockf(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.lockf(file.fileno(), fcntl.LOCK_UN)
        except BlockingIOError:
            return "held_by_other_process"
        return "acquired"


def _fds(pid, root):
    proc = subprocess.run(["lsof", "-nP", "-F", "pcfnl", "-p", str(pid)],
                          capture_output=True, text=True, timeout=5)
    if proc.returncode not in (0, 1):
        return {"error": proc.stderr.strip()}
    found = []
    fd = ""
    lock = ""
    for line in proc.stdout.splitlines():
        if line.startswith("f"):
            fd, lock = line[1:], ""
        elif line.startswith("l"):
            lock = line[1:]
        elif line.startswith("n") and str(root) in line and (
                "thread-writer-locks" in line or "/sessions/" in line):
            name = line[1:].replace(str(root.resolve()), "<temp>").replace(str(root), "<temp>")
            found.append({"fd": fd, "lock": lock, "name": name})
    return found


def _resume(rpc, sid, rollout, cwd):
    try:
        result = rpc.call("thread/resume", {"threadId": sid, "path": str(rollout),
                                           "cwd": str(cwd)})
        return {"ok": result["thread"]["id"] == sid}
    except RuntimeError as exc:
        return {"error": str(exc).replace(str(rollout), "<rollout>").replace(sid, "<thread>")}


def _drain(fd, seconds):
    end = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < end:
        if not select.select([fd], [], [], min(0.2, end - time.monotonic()))[0]:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _cli_no_daemon(binary, sid, cwd, env, release_first, second, rollout, writer_lock):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    proc = subprocess.Popen([binary, "resume", sid, "--cd", str(cwd),
                             "--no-daemon", "--no-alt-screen", "--disable", "plugins"],
                            cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True)
    os.close(slave)
    try:
        output = _drain(master, 0.4)
        os.write(master, b"\x1b[1;1R\x1b]10;rgb:ffff/ffff/ffff\x07"
                 b"\x1b]11;rgb:0000/0000/0000\x07\x1b[?1;2c")
        output += _drain(master, 0.4)
        if b"Trust" in output:
            os.write(master, b"\r")
        output += _drain(master, 1.5)
        result = {"ownership_screen_while_first_owns":
                  b"This conversation is open in another app" in output,
                  "cli_remains_open_for_retry": proc.poll() is None,
                  "no_rollout_error": b"no rollout found" in output,
                  "login_prompt": b"Sign in with ChatGPT" in output}
        old_owners = subprocess.run(["lsof", "-nP", "-t", str(writer_lock)],
                                    text=True, capture_output=True, timeout=5)
        result["blocked_cli_pid_has_writer_lock_open"] = str(proc.pid) in old_owners.stdout.splitlines()
        release_first()
        os.write(master, b"r")
        retried = _drain(master, 1.5)
        result["retried_after_owner_exits"] = b"Ask Codex to do anything" in retried
        result["writer_lock_flock_while_cli_owns"] = _try_lock(writer_lock, "flock")
        owners = subprocess.run(["lsof", "-nP", "-t", str(writer_lock)],
                                text=True, capture_output=True, timeout=5)
        result["cli_pid_has_writer_lock_open"] = str(proc.pid) in owners.stdout.splitlines()
        result["second_backend_blocked_while_cli_owns"] = _resume(second, sid, rollout, cwd)
        return result
    finally:
        os.close(master)
        stop(proc)


def run(binary):
    with tempfile.TemporaryDirectory(prefix="t-writer-probe-") as temp, ExitStack() as stack:
        root = Path(temp)
        env = {"PATH": os.environ.get("PATH", os.defpath), "TERM": "xterm-256color",
               "LANG": "en_US.UTF-8", "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
               "GIT_CONFIG_NOSYSTEM": "1"}
        for key in ("HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                    "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR"):
            target = root / key.lower()
            target.mkdir()
            env[key] = str(target)
        codex_home = Path(env["CODEX_HOME"])
        (codex_home / "config.toml").write_text(
            'model_provider = "poc"\nmodel = "poc"\n[features]\nplugins = false\n'
            '[model_providers.poc]\nname = "No inference POC"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\n')
        cwd = root / "worktree"
        subprocess.run(["git", "init", "-q", str(cwd)], env=env, check=True,
                       capture_output=True)
        sid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        folder = codex_home / "sessions" / now.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        rollout = folder / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": sid, "session_id": sid,
                "timestamp": timestamp, "cwd": str(cwd), "originator": "codex-tui",
                "cli_version": "0.159.0", "source": "cli"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Synthetic writer probe."}]}},
        ]
        rollout.write_text("".join(json.dumps(dict(row, timestamp=timestamp)) + "\n"
                                   for row in records))
        writer_lock = codex_home / "thread-writer-locks" / f"{sid}.lock"
        servers = []
        for index in range(2):
            errors = stack.enter_context((root / f"server-{index}.log").open("wb"))
            rpc = RPC(binary, env, cwd, errors)
            stack.callback(rpc.close)
            rpc.initialize()
            servers.append(rpc)
        first, second = servers
        result = {"binary": subprocess.run([binary, "--version"], env=env,
                  text=True, capture_output=True, check=True).stdout.strip()}
        result["first_resume"] = _resume(first, sid, rollout, cwd)
        first_pid = first.call("server/diagnostics", {})["process"]["id"]
        second_pid = second.call("server/diagnostics", {})["process"]["id"]
        result["backend_pids_distinct"] = first_pid != second_pid
        result["rollout_lock_flock_while_loaded"] = _try_lock(rollout, "flock")
        result["rollout_lock_fcntl_while_loaded"] = _try_lock(rollout, "fcntl")
        result["writer_lock_exists"] = writer_lock.exists()
        result["writer_lock_flock_while_loaded"] = _try_lock(writer_lock, "flock")
        result["writer_lock_fcntl_while_loaded"] = _try_lock(writer_lock, "fcntl")
        result["first_backend_open_fds"] = _fds(first_pid, root)
        result["second_backend_open_fds"] = _fds(second_pid, root)
        result["second_resume_while_first_idle"] = _resume(second, sid, rollout, cwd)
        result["first_status"] = first.call("thread/read", {"threadId": sid,
                                                            "includeTurns": False})["thread"]["status"]
        result["first_unsubscribe"] = first.call("thread/unsubscribe", {"threadId": sid})
        result["second_resume_after_unsubscribe"] = _resume(second, sid, rollout, cwd)
        result["cli_no_daemon_while_first_owns"] = _cli_no_daemon(
            binary, sid, cwd, env, first.close, second, rollout, writer_lock)
        result["rollout_lock_flock_after_first_exits"] = _try_lock(rollout, "flock")
        result["rollout_lock_fcntl_after_first_exits"] = _try_lock(rollout, "fcntl")
        result["writer_lock_flock_after_first_exits"] = _try_lock(writer_lock, "flock")
        result["writer_lock_fcntl_after_first_exits"] = _try_lock(writer_lock, "fcntl")
        result["writer_lock_present_after_first_exits"] = writer_lock.exists()
        result["second_resume_after_first_exits"] = _resume(second, sid, rollout, cwd)
        result["second_loaded"] = sid in second.call("thread/loaded/list", {})["data"]
        result["turns_started"] = len(second.call("thread/read", {"threadId": sid,
                    "includeTurns": True})["thread"].get("turns", []))
        try:
            result["archive"] = second.call("thread/archive", {"threadId": sid})
            result["writer_lock_flock_after_archive"] = _try_lock(writer_lock, "flock")
            result["writer_lock_removed_after_archive"] = not writer_lock.exists()
            result["loaded_after_archive"] = sid in second.call("thread/loaded/list", {})["data"]
            restored = second.call("thread/unarchive", {"threadId": sid})["thread"]
            result["unarchive"] = restored["id"] == sid
            result["unarchive_cwd"] = os.path.realpath(restored.get("cwd", "")) == os.path.realpath(cwd)
            read_back = second.call("thread/read", {"threadId": sid, "includeTurns": False})["thread"]
            result["read_after_unarchive_cwd"] = (
                os.path.realpath(read_back.get("cwd", "")) == os.path.realpath(cwd))
            result["read_after_unarchive_cwd_value"] = (
                str(read_back.get("cwd")).replace(str(root.resolve()), "<temp>")
                .replace(str(root), "<temp>"))
            result["read_after_unarchive_keys"] = sorted(read_back)
            result["original_rollout_restored"] = rollout.exists()
        except RuntimeError as exc:
            result["archive_error"] = str(exc).replace(sid, "<thread>")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        parser.error("codex missing")
    print(json.dumps(run(str(Path(args.codex).resolve())), indent=2))


if __name__ == "__main__":
    main()
