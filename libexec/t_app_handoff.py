"""Read-only checks before moving a Codex desktop thread back to the CLI."""

import json
import os
import re
import select
import subprocess
import time


_RETRY = "Finish the turn, quit the Codex desktop app, then retry t app pull."


def _processes(run, retry):
    result = run(["ps", "-ww", "-x", "-o", "pid=,command="], timeout=5)
    if result.returncode:
        raise ValueError("could not inspect desktop and app-server processes; " + retry)
    processes = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            processes.append((int(fields[0]), fields[1]))
        elif line.strip():
            raise ValueError("could not parse the process list; " + retry)
    return processes


def _owners(bundle, processes):
    # The app may have been uninstalled since the reservation was written.
    # Recognize both historical names without requiring their bundles to exist.
    candidates = [os.path.join(base, name + ".app")
                  for base in ("/Applications", os.path.join(os.path.expanduser("~"), "Applications"))
                  for name in ("ChatGPT", "Codex")]
    if bundle:
        candidates.append(bundle)
    roots = {path + os.sep for candidate in candidates
             for path in (os.path.abspath(candidate), os.path.realpath(candidate))}
    app = []
    daemon = []
    for pid, command in processes:
        # The command path may contain spaces (notably Electron helpers). A
        # mention of the bundle in another process's arguments blocks safely.
        if any(root in command for root in roots):
            app.append(pid)
        elif (re.search(r"(?:^|[\s/])codex(?:-[\w-]+)?(?:\s|$)", command)
              and re.search(r"(?:^|\s)app-server(?:\s|$)", command)):
            daemon.append(pid)
    return app, daemon


def _read_response(process, request_id, deadline, buffer=b""):
    while True:
        if b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            try:
                reply = json.loads(raw)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == request_id:
                if "error" in reply or not isinstance(reply.get("result"), dict):
                    raise ValueError("app-server refused the thread status check")
                return reply["result"], buffer
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise ValueError("app-server thread status check timed out")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise ValueError("app-server proxy exited before verifying the thread")
        buffer += chunk
        if len(buffer) > 1024 * 1024:
            raise ValueError("app-server thread status reply was too large")


def _thread_status(sid, cwd):
    """Connect only through proxy; it does not start or stop the shared daemon."""
    process = subprocess.Popen(
        ["codex", "app-server", "proxy"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.monotonic() + 8
    try:
        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "t-app-pull", "version": "1"}}})
        _, pending = _read_response(process, 1, deadline)
        send({"method": "initialized"})
        send({"id": 2, "method": "thread/read",
              "params": {"threadId": sid, "includeTurns": False}})
        result, _ = _read_response(process, 2, deadline, pending)
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != sid or thread.get("cwd") != cwd:
            raise ValueError("app-server returned a different thread or worktree")
        status = thread.get("status")
        if not isinstance(status, dict) or status.get("type") not in ("idle", "notLoaded"):
            raise ValueError("the desktop thread is still active or its status is unknown")
    finally:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        process.stdin.close()
        process.stdout.close()


def assert_released(bundle, sid, cwd, run, *, retry=_RETRY):
    """Raise unless no desktop owner remains and any shared daemon reports idle."""
    app, daemon = _owners(bundle, _processes(run, retry))
    if app:
        raise ValueError("the Codex desktop app is still running. " + retry)
    if daemon:
        try:
            _thread_status(sid, cwd)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"could not verify that the desktop thread is released: {exc}. {retry}") from exc
    # Recheck the GUI after the RPC: a launch during the probe must block the pull.
    app, _ = _owners(bundle, _processes(run, retry))
    if app:
        raise ValueError("the Codex desktop app opened during the status check. " + retry)
