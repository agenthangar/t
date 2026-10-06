"""Read-only checks before moving a Codex desktop thread back to the CLI."""

import json
import os
import re
import select
import subprocess
import time


_RETRY = "Retry: t app pull."
_QUIT = "Finish the turn and quit the Codex desktop app on this Mac (Cmd-Q). "


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
    frontends = {root + "Contents/MacOS/" + name
                 for root in roots for name in ("ChatGPT", "Codex")}
    bundled_clis = {root + "Contents/Resources/" + suffix
                   for root in roots for suffix in ("codex", "codex-cli/bin/codex")}
    app = []
    daemon = []
    for pid, command in processes:
        # Only the actual desktop frontend requires quitting. Computer-use,
        # code-mode, and Electron helpers can outlive it, and CLIs may use the
        # bundled executable. Neither a bundle path nor an argument mentioning
        # the app establishes desktop ownership.
        if any(command == exe or command.startswith(exe + " ") for exe in frontends):
            app.append(pid)
        elif ((re.match(r"^(?:\S*/)?codex(?:-(?:aarch64|x86_64)-[\w.-]+)?(?:\s|$)", command)
               or any(command == exe or command.startswith(exe + " ") for exe in bundled_clis))
              and re.search(r"(?:^|\s)app-server(?:\s|$)", command)
              and not re.search(r"\bapp-server\s+(?:pid-update-loop|proxy|generate-ts|generate-json-schema)\b", command)
              and not re.search(r"\bapp-server\s+daemon\s+(?:start|stop|status|version|pid-update-loop)\b", command)):
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


def _thread_status(sid, cwd, daemon_pids):
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
              "params": {"clientInfo": {"name": "t-app-pull", "version": "1"},
                         "capabilities": {"experimentalApi": True}}})
        _, pending = _read_response(process, 1, deadline)
        send({"method": "initialized"})
        # A bundled stdio backend and the shared daemon can coexist. The proxy
        # connects only to the latter: its idle answer must never release a
        # conversation still owned by an unchecked backend.
        send({"id": 2, "method": "server/diagnostics", "params": {}})
        result, pending = _read_response(process, 2, deadline, pending)
        server = result.get("process")
        if (not isinstance(server, dict) or type(server.get("id")) is not int
                or set(daemon_pids) != {server["id"]}):
            raise ValueError("the proxy could not verify every running Codex backend")
        send({"id": 3, "method": "thread/read",
              "params": {"threadId": sid, "includeTurns": False}})
        result, _ = _read_response(process, 3, deadline, pending)
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
        raise ValueError("the Codex desktop app is still running (PID "
                         + ", ".join(map(str, app)) + "). " + _QUIT + retry)
    if daemon:
        try:
            _thread_status(sid, cwd, daemon)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            # A backend can finish shutting down while its proxy is connecting.
            # Recheck before reporting a failure after the user already quit.
            app, remaining = _owners(bundle, _processes(run, retry))
            if app:
                raise ValueError("the Codex desktop app reopened during the status check. "
                                 + _QUIT + retry) from exc
            if remaining:
                raise ValueError("the Codex desktop app is closed, but its background server "
                                 "could not verify release of this conversation (PID "
                                 + ", ".join(map(str, remaining)) + f"): {exc}. {retry}") from exc
    # A frontend or an unchecked backend launched during the probe must block.
    app, remaining = _owners(bundle, _processes(run, retry))
    if app:
        raise ValueError("the Codex desktop app opened during the status check. " + _QUIT + retry)
    if set(remaining) - set(daemon):
        raise ValueError("a Codex background server started during the status check. " + retry)
