#!/usr/bin/env python3
"""Probe CLI frontend termination before an independent desktop-like resume.

Everything lives under one disposable HOME/CODEX_HOME. The synthetic history
contains a saved user item, but no model turn or network inference is started.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pty
import runpy
import select
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid

from poc_desktop_release import Rpc
from poc_desktop_shared_backend import connect


HANDOFF = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                          "libexec" / "t_app_handoff.py"))


def run_cmd(argv, env, cwd, *, input=None, timeout=10):
    return subprocess.run(argv, env=env, cwd=cwd, input=input, capture_output=True,
                          text=True, timeout=timeout)


def lock_holders(path, env, cwd):
    if not path.exists():
        return []
    result = run_cmd(["lsof", "-nP", "-t", "--", str(path)], env, cwd)
    return [int(pid) for pid in result.stdout.splitlines() if pid.isdecimal()]


def wait_until(test, *, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("synthetic CLI did not become ready")


def drain(fd, seconds=0.1):
    end = time.monotonic() + seconds
    output = b""
    while time.monotonic() < end and select.select([fd], [], [], min(0.1, end - time.monotonic()))[0]:
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        output += data
    return output


def attempt_resume(rpc, sid, rollout, cwd):
    try:
        thread = rpc.request("thread/resume", {"threadId": sid, "path": str(rollout),
                                               "cwd": str(cwd)})["thread"]
        return "same_id" if thread["id"] == sid else "wrong_id"
    except RuntimeError as error:
        message = str(error)
        if "active writer" in message:
            return "active_writer"
        if "archived" in message:
            return "archived"
        return "other_error: " + message.replace(sid, "<thread>").replace(str(rollout), "<rollout>")


def run(binary: Path, no_daemon: bool, shared_backend: bool,
        graceful: bool, wait_release: bool, production_wait: bool):
    if production_wait and not shared_backend:
        raise ValueError("--production-wait requires --shared-backend")
    with tempfile.TemporaryDirectory(prefix="t-cli-to-desktop-") as scratch:
        root = Path(scratch).resolve()
        for name in ("home", "codex", "config", "cache", "data", "state", "tmp", "repo"):
            (root / name).mkdir()
        home, codex_home, cwd = root / "home", root / "codex", root / "repo"
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "TERM": "xterm-256color", "LANG": "C", "HOME": str(home),
            "CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root / "tmp"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        }
        (codex_home / "config.toml").write_text(
            'model_provider = "poc"\nmodel = "poc"\n[features]\nplugins = false\n'
            '[model_providers.poc]\nname = "No inference POC"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\n'
            "\n[projects." + json.dumps(str(cwd)) + "]\ntrust_level = \"trusted\"\n"
        )
        run_cmd(["git", "init", "-q", str(cwd)], env, root)
        login = run_cmd([str(binary), "login", "--with-api-key"], env, cwd,
                        input="sk-poc-never-used\n")
        if login.returncode:
            raise RuntimeError("temporary fake login failed")
        sid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        folder = codex_home / "sessions" / now.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        rollout = folder / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
        records = [
            {"timestamp": timestamp, "type": "session_meta", "payload": {
                "id": sid, "session_id": sid, "timestamp": timestamp, "cwd": str(cwd),
                "originator": "codex-tui", "cli_version": "0.159.0", "source": "cli"}},
            {"timestamp": timestamp, "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text",
                                                         "text": "synthetic saved history"}]}}
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
        lock = codex_home / "thread-writer-locks" / (sid + ".lock")
        server = None
        shared = None
        control = None
        if shared_backend:
            socket_path = root / "tmp" / "shared.sock"
            server_log = (root / "shared.log").open("wb")
            server = subprocess.Popen([str(binary), "app-server", "--listen",
                                       f"unix://{socket_path}", "--disable", "plugins"],
                                      env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=server_log,
                                      start_new_session=True)
            wait_until(lambda: socket_path.exists())
            shared = connect(str(socket_path), "t_cli_to_desktop_observer")
            control = shared.request("thread/start", {"cwd": str(root)})["thread"]["id"]
            shared.request("thread/inject_items", {"threadId": control, "items": [{
                "type": "message", "role": "user", "content": [{
                    "type": "input_text", "text": "synthetic unrelated control"}]}]})
        master, slave = pty.openpty()
        argv = [str(binary), "resume"]
        if shared_backend:
            argv.extend(["--remote", f"unix://{socket_path}"])
        argv.extend([sid, "--cd", str(cwd), "--no-alt-screen", "--disable", "plugins"])
        if no_daemon:
            argv.append("--no-daemon")
        cli = subprocess.Popen(argv, env=env, cwd=cwd, stdin=slave, stdout=slave,
                               stderr=slave, start_new_session=True)
        os.close(slave)
        observer = None
        try:
            # Respond to the common terminal capability probes without issuing
            # a prompt or model request.
            drain(master, 0.2)
            os.write(master, b"\x1b[1;1R\x1b]10;rgb:ffff/ffff/ffff\x07"
                     b"\x1b]11;rgb:0000/0000/0000\x07\x1b[?1;2c")
            output = drain(master, 0.4)
            if b"Trust" in output:
                os.write(master, b"\r")
            initial_holders = wait_until(lambda: lock_holders(lock, env, cwd))
            observer = Rpc([str(binary), "app-server", "--stdio"], env, cwd)
            observer.initialize(experimental=True)
            before = attempt_resume(observer, sid, rollout, cwd)
            if before != "active_writer":
                raise RuntimeError("independent backend was not blocked before CLI exit: " + before)
            contract = (HANDOFF["preflight_cli_release"](
                sid, str(cwd), codex_home=str(codex_home), rollout=str(rollout))
                if production_wait else None)
            graceful_exit = False
            if graceful:
                os.write(master, b"\x04")
                try:
                    cli.wait(timeout=5)
                    graceful_exit = True
                except subprocess.TimeoutExpired:
                    pass
            if cli.poll() is None:
                cli.send_signal(signal.SIGTERM)
                cli.wait(timeout=5)
            after_holders = lock_holders(lock, env, cwd)
            immediate_after = attempt_resume(observer, sid, rollout, cwd)
            after = immediate_after
            loaded_after = (sid in shared.request("thread/loaded/list")["data"]
                            if shared is not None else None)
            archive = run_cmd([str(binary), "archive", sid], env, cwd, timeout=5)
            released_after_seconds = None
            wait_announced = False
            if production_wait and after == "active_writer":
                def announce():
                    nonlocal wait_announced
                    wait_announced = True

                started_wait = time.monotonic()
                HANDOFF["wait_cli_released"](
                    sid, str(cwd), codex_home=str(codex_home),
                    rollout=str(rollout), contract=contract, timeout=75,
                    interval=0.2, on_wait=announce)
                released_after_seconds = round(time.monotonic() - started_wait, 1)
                after = attempt_resume(observer, sid, rollout, cwd)
                assert after == "same_id", "production wait ended before independent resume"
                assert control in shared.request("thread/loaded/list")["data"]
            elif wait_release and after == "active_writer":
                started_wait = time.monotonic()
                for _ in range(80):
                    time.sleep(1)
                    if not lock_holders(lock, env, cwd):
                        released_after_seconds = round(time.monotonic() - started_wait, 1)
                        break
                if released_after_seconds is not None:
                    after = attempt_resume(observer, sid, rollout, cwd)
            shared_archive = None
            after_unarchive = None
            control_after_archive = None
            if shared is not None and after == "active_writer":
                try:
                    shared.request("thread/archive", {"threadId": sid})
                    shared_archive = "succeeded"
                    control_after_archive = control in shared.request("thread/loaded/list")["data"]
                    observer.request("thread/unarchive", {"threadId": sid})
                    with sqlite3.connect(f"file:{codex_home / 'state_5.sqlite'}?mode=ro", uri=True) as db:
                        restored_path = db.execute("select rollout_path from threads where id=?",
                                                   (sid,)).fetchone()[0]
                    after_unarchive = attempt_resume(observer, sid, Path(restored_path), cwd)
                except RuntimeError as error:
                    shared_archive = "error: " + str(error).replace(sid, "<thread>")
            row = None
            db_path = codex_home / "state_5.sqlite"
            if db_path.is_file():
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
                    row = db.execute("select archived from threads where id=?", (sid,)).fetchone()
            return {
                "version": run_cmd([str(binary), "--version"], env, cwd).stdout.strip(),
                "no_daemon": no_daemon,
                "explicit_shared_backend": shared_backend,
                "graceful_frontend_exit": graceful_exit,
                "cli_exit_code": cli.returncode,
                "initial_writer_holders": len(initial_holders),
                "cli_pid_was_writer": cli.pid in initial_holders,
                "independent_resume_before_exit": before,
                "writer_holders_after_frontend_exit": len(after_holders),
                "former_cli_pid_still_writer": cli.pid in after_holders,
                "independent_resume_immediately_after_exit": immediate_after,
                "independent_resume_after_wait": after if (wait_release or production_wait) else None,
                "production_wait_restored_same_id": after == "same_id" if production_wait else None,
                "shared_target_loaded_after_frontend_exit": loaded_after,
                "control_thread_loaded_after_frontend_exit": (
                    control in shared.request("thread/loaded/list")["data"]
                    if shared is not None else None),
                "natural_release_after_seconds": released_after_seconds,
                "production_preflight_contract": contract,
                "production_wait_announced": wait_announced,
                "shared_archive_after_frontend_exit": shared_archive,
                "control_loaded_after_shared_archive": control_after_archive,
                "independent_resume_after_unarchive": after_unarchive,
                "plain_archive_exit_code": archive.returncode,
                "archived_after_plain_archive": bool(row and row[0]),
                "inference_requested": False,
            }
        finally:
            if observer is not None:
                observer.close()
            if shared is not None:
                shared.sock.close()
            if cli.poll() is None:
                cli.terminate()
                try:
                    cli.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    cli.kill()
                    cli.wait()
            os.close(master)
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                server_log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--no-daemon", action="store_true")
    parser.add_argument("--shared-backend", action="store_true")
    parser.add_argument("--graceful", action="store_true")
    parser.add_argument("--wait-release", action="store_true")
    parser.add_argument("--production-wait", action="store_true")
    args = parser.parse_args()
    if not args.codex:
        parser.error("codex is required")
    print(json.dumps(run(Path(args.codex).resolve(), args.no_daemon,
                         args.shared_backend,
                         args.graceful, args.wait_release,
                         args.production_wait), sort_keys=True, indent=2))
