#!/usr/bin/env python3
"""Isolated Codex desktop/CLI shared-backend experiment; no turns or model calls.

Run with ``python3 scripts/poc_desktop_shared_backend.py`` (or set
``T_POC_CODEX_BIN`` for a different installed version). All state, the Unix
socket, and the server/CLI processes live under one disposable TemporaryDirectory.
The JSON-RPC client stands in for a desktop client; it is not the real app.
"""

import json
import base64
import fcntl
import os
import pty
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path


class Rpc:
    def __init__(self, sock):
        self.sock = sock
        self.seq = 0

    def send(self, message):
        payload = json.dumps(message).encode()
        mask = os.urandom(4)
        size = len(payload)
        header = bytearray([0x81])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.extend([0x80 | 126])
            header.extend(struct.pack("!H", size))
        else:
            header.extend([0x80 | 127])
            header.extend(struct.pack("!Q", size))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def exact(self, size):
        data = b""
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise RuntimeError("server closed the RPC connection")
            data += part
        return data

    def receive(self):
        first, second = self.exact(2)
        opcode = first & 0x0f
        length = second & 0x7f
        if length == 126:
            length = struct.unpack("!H", self.exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.exact(8))[0]
        if second & 0x80:
            mask = self.exact(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self.exact(length)))
        else:
            payload = self.exact(length)
        if opcode == 9:
            return self.receive()
        if opcode != 1:
            raise RuntimeError(f"unexpected WebSocket opcode {opcode}")
        return json.loads(payload)

    def request(self, method, params=None, timeout=8):
        self.seq += 1
        req_id = self.seq
        message = {"id": req_id, "method": method, "params": params if params is not None else {}}
        self.send(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            response = self.receive()
            if response.get("id") != req_id:
                continue
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            return response["result"]
        raise TimeoutError(method)

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)


def connect(path, name):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
                  "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    headers = b""
    while b"\r\n\r\n" not in headers:
        headers += sock.recv(4096)
    if not headers.startswith(b"HTTP/1.1 101 ") and not headers.startswith(b"HTTP/1.1 101\r"):
        raise RuntimeError("WebSocket upgrade failed: " + repr(headers[:300]))
    client = Rpc(sock)
    client.request("initialize", {"clientInfo": {"name": name, "title": name, "version": "0"}})
    client.notify("initialized", {})
    return client


def drain_pty(fd, seconds):
    end = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], min(0.2, end - time.monotonic()))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def main():
    codex = os.environ.get("T_POC_CODEX_BIN") or shutil.which("codex")
    if not codex:
        raise SystemExit("codex executable missing")
    with tempfile.TemporaryDirectory(prefix="t-shared-codex-") as root:
        base = Path(root)
        for relative in ("home", "codex", "xdg_config", "xdg_cache", "xdg_state", "xdg_data", "tmp", "work"):
            (base / relative).mkdir()
        path = base / "app.sock"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            "HOME": str(base / "home"),
            "CODEX_HOME": str(base / "codex"),
            "XDG_CONFIG_HOME": str(base / "xdg_config"),
            "XDG_CACHE_HOME": str(base / "xdg_cache"),
            "XDG_STATE_HOME": str(base / "xdg_state"),
            "XDG_DATA_HOME": str(base / "xdg_data"),
            "TMPDIR": str(base / "tmp"),
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "TERM": "xterm-256color",
        }
        subprocess.run(["git", "init", "-q", str(base / "work")],
                       check=True, env=env, capture_output=True)
        # CLI checks for login before resuming, even against an already running
        # app-server. A deliberately invalid key clears only that local UI gate;
        # the POC never starts a turn and makes no model request.
        login = subprocess.run(
            [codex, "login", "--with-api-key"], input="sk-poc-never-used\n",
            text=True, cwd=base / "work", env=env, capture_output=True, timeout=8,
        )
        if login.returncode:
            raise RuntimeError("isolated fake-key login failed: " + login.stderr)
        server = subprocess.Popen(
            [codex, "app-server", "--disable", "plugins", "--listen", f"unix://{path}"],
            cwd=base / "work", env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        cli = None
        master = None
        desktop = observer = None
        try:
            for _ in range(100):
                if path.exists():
                    break
                if server.poll() is not None:
                    raise RuntimeError("app-server exited: " + server.stderr.read().decode(errors="replace"))
                time.sleep(0.05)
            else:
                raise RuntimeError("app-server did not create the Unix socket")
            desktop = connect(str(path), "t_poc_desktop")
            observer = connect(str(path), "t_poc_observer")
            started = desktop.request("thread/start", {"cwd": str(base / "work")})
            sid = started["thread"]["id"]
            injected = desktop.request("thread/inject_items", {
                "threadId": sid,
                "items": [{"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": "POC saved history."}]}],
            })
            print("seed_history", injected)
            print("server_pid", server.pid)
            print("thread_id", sid)
            print("desktop_created_status", observer.request("thread/read", {"threadId": sid, "includeTurns": False})["thread"]["status"])
            print("loaded_before_cli", observer.request("thread/loaded/list")["data"])

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            cli = subprocess.Popen(
                [codex, "resume", "--remote", f"unix://{path}", sid,
                 "--cd", str(base / "work"), "--no-alt-screen", "--disable", "plugins"],
                cwd=base / "work", env=env,
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            tui = drain_pty(master, 0.4)
            os.write(master, b"\x1b[1;1R\x1b]10;rgb:ffff/ffff/ffff\x07"
                     b"\x1b]11;rgb:0000/0000/0000\x07\x1b[?1;2c")
            tui += drain_pty(master, 0.5)
            if b"Trust" in tui:
                os.write(master, b"\r")
            tui += drain_pty(master, 2.1)
            print("cli_pid", cli.pid)
            print("cli_alive", cli.poll() is None)
            print("cli_rendered_codex_tui", b"OpenAI Codex" in tui)
            print("cli_reported_resume_error", b"Failed to resume session" in tui)
            loaded = observer.request("thread/loaded/list")["data"]
            print("loaded_with_both_clients", loaded)
            print("desktop_observed_status", desktop.request("thread/read", {"threadId": sid, "includeTurns": False})["thread"]["status"])
            desktop_resumed = desktop.request("thread/resume", {"threadId": sid})
            print("desktop_second_resume_while_cli_open", desktop_resumed["thread"]["id"])
            print("server_still_same_pid", server.poll() is None, server.pid)
            print("desktop_unsubscribe", desktop.request("thread/unsubscribe", {"threadId": sid}))
            print("after_unsubscribe_status", observer.request("thread/read", {"threadId": sid, "includeTurns": False})["thread"]["status"])
            print("loaded_after_unsubscribe", observer.request("thread/loaded/list")["data"])
            print("desktop_can_rejoin_after_unsubscribe", desktop.request("thread/resume", {"threadId": sid})["thread"]["id"] == sid)
            print("desktop_unsubscribe_again", desktop.request("thread/unsubscribe", {"threadId": sid}))
            os.close(master)
            master = None
            stop(cli)
            print("loaded_after_both_ui_clients_leave", observer.request("thread/loaded/list")["data"])
            print("status_after_both_ui_clients_leave", observer.request("thread/read", {"threadId": sid, "includeTurns": False})["thread"]["status"])
            turns = observer.request("thread/read", {"threadId": sid, "includeTurns": True})["thread"].get("turns", [])
            print("turns_started", len(turns))
            assert cli.returncode is not None and b"OpenAI Codex" in tui
            assert b"Failed to resume session" not in tui
            assert loaded == [sid] and desktop_resumed["thread"]["id"] == sid
            assert not turns, "POC must never start a model turn"
        finally:
            if desktop is not None:
                desktop.sock.close()
            if observer is not None:
                observer.sock.close()
            if master is not None:
                os.close(master)
            stop(cli)
            stop(server)
            server.stderr.close()


if __name__ == "__main__":
    main()
