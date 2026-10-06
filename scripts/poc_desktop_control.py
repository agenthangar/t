#!/usr/bin/env python3
"""Disposable experiment: does CLI archiving unload a live app-server thread?

All state lives under a temporary HOME/XDG/CODEX_HOME. A synthetic user-history
item makes the thread durable, but no model turn is started. The script reports desktop protocol
references from app.asar without launching or controlling the desktop app.
"""

import argparse
import base64
import json
import os
import select
import secrets
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
from pathlib import Path


APP_ASAR = Path("/Applications/ChatGPT.app/Contents/Resources/app.asar")


def asar_main_references():
    if not APP_ASAR.is_file():
        return {"available": False}
    with APP_ASAR.open("rb") as stream:
        preamble = stream.read(16)
        header_block_size = struct.unpack("<I", preamble[4:8])[0]
        header_json_size = struct.unpack("<I", preamble[12:16])[0]
        header = json.loads(stream.read(header_json_size))
        item = header
        for part in (".vite", "build"):
            item = item["files"][part]
        js_name = next(name for name in item["files"] if name.startswith("main-") and name.endswith(".js"))
        item = item["files"][js_name]
        stream.seek(8 + header_block_size + int(item["offset"]))
        code = stream.read(item["size"]).decode("utf-8", "replace")
    keys = (
        "thread/unsubscribe",
        "thread/archive",
        "thread/closed",
        "thread/loaded/list",
        "CODEX_APP_SERVER_USE_LOCAL_DAEMON",
        "app-server proxy",
    )
    return {"available": True, "main_js": js_name, "references": {key: code.count(key) for key in keys}}


class StdioClient:
    def __init__(self, process):
        self.process = process
        self.pending = b""
        self.next_id = 0
        self.notifications = []

    def request(self, method, params, timeout=8):
        self.next_id += 1
        req_id = self.next_id
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                if not line:
                    continue
                event = json.loads(line)
                if event.get("id") == req_id:
                    return event
                self.notifications.append(event.get("method", "unmatched response"))
                continue
            ready, _, _ = select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                break
            self.pending += chunk
        return {"error": {"message": f"timeout or EOF waiting for {method}"}}

    def notify(self, method):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        self.process.stdin.flush()


class WebSocketClient:
    """Minimal local-only WebSocket JSON-RPC client for a disposable Unix server."""

    def __init__(self, endpoint):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(endpoint)
        self.sock.settimeout(8)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(
            (f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
             f"Sec-WebSocket-Key: {key}\r\n\r\n").encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        if not response.startswith(b"HTTP/1.1 101 "):
            raise RuntimeError(f"WebSocket upgrade failed: {response[:200]!r}")
        self.pending = response.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0
        self.notifications = []

    def _frame(self, payload):
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = b"\x81" + (
            bytes([0x80 | length]) if length < 126 else
            b"\xfe" + struct.pack("!H", length) if length < 65536 else
            b"\xff" + struct.pack("!Q", length)
        )
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, size):
        while len(self.pending) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("WebSocket closed")
            self.pending += chunk
        data, self.pending = self.pending[:size], self.pending[size:]
        return data

    def _event(self):
        first, second = self._read(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._read(8))[0]
        if second & 0x80:
            mask = self._read(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(size)))
        else:
            payload = self._read(size)
        if first & 0xF == 8:
            raise EOFError("WebSocket close frame")
        return json.loads(payload)

    def request(self, method, params, timeout=8):
        self.next_id += 1
        req_id = self.next_id
        self._frame(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                event = self._event()
            except (TimeoutError, EOFError):
                break
            if event.get("id") == req_id:
                return event
            self.notifications.append(event.get("method", "unmatched response"))
        return {"error": {"message": f"timeout or EOF waiting for {method}"}}

    def notify(self, method):
        self._frame(json.dumps({"jsonrpc": "2.0", "method": method}).encode())

    def close(self):
        self.sock.close()


def run_experiment(cli, remote=False, action="archive"):
    with tempfile.TemporaryDirectory(prefix="t-desktop-control-") as root:
        root = Path(root)
        for child in ("home", "xdg_config", "xdg_cache", "xdg_data", "xdg_state", "tmp", "codex", "work"):
            (root / child).mkdir()
        (root / "codex" / "config.toml").write_text(
            'model_provider = "offline_poc"\n'
            '[model_providers.offline_poc]\n'
            'name = "Offline POC"\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\n'
            '[features]\n'
            'plugins = false\n'
        )
        env = {key: os.environ[key] for key in ("PATH", "TERM", "LANG", "LC_ALL") if key in os.environ}
        env.update(
            HOME=str(root / "home"),
            XDG_CONFIG_HOME=str(root / "xdg_config"),
            XDG_CACHE_HOME=str(root / "xdg_cache"),
            XDG_DATA_HOME=str(root / "xdg_data"),
            XDG_STATE_HOME=str(root / "xdg_state"),
            TMPDIR=str(root / "tmp"),
            CODEX_HOME=str(root / "codex"),
        )
        endpoint = str(root / "server.sock") if remote else None
        server = subprocess.Popen(
            [cli, "app-server", "--listen", f"unix://{endpoint}" if remote else "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=root / "work",
            start_new_session=True,
        )
        rpc = None
        try:
            if remote:
                for _ in range(100):
                    if Path(endpoint).exists():
                        break
                    time.sleep(0.02)
                rpc = WebSocketClient(endpoint)
            else:
                rpc = StdioClient(server)
            initial = rpc.request("initialize", {"clientInfo": {"name": "t-desktop-control-poc", "version": "1"}})
            if "error" in initial:
                return {"initialize": initial}
            rpc.notify("initialized")
            started = rpc.request("thread/start", {"cwd": str(root / "work"), "ephemeral": False})
            if "error" in started:
                return {"thread_start": started}
            thread_id = started["result"]["thread"]["id"]
            injected = rpc.request("thread/inject_items", {"threadId": thread_id, "items": [
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Disposable local handoff probe; no model call."}
                ]}
            ]})
            if "error" in injected:
                return {"inject_items": injected}
            with sqlite3.connect(root / "codex" / "state_5.sqlite") as db:
                record = db.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
            rollout = Path(record[0]) if record and record[0] else None
            durable_rollout = bool(rollout and rollout.is_file()
                                   and b"Disposable local handoff probe" in rollout.read_bytes())
            if not durable_rollout:
                return {"durable_rollout": False, "thread_id": thread_id}
            before = rpc.request("thread/loaded/list", {})
            if action == "archive":
                archive = subprocess.run(
                    [cli, "archive", thread_id] + (["--remote", f"unix://{endpoint}"] if remote else []),
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=root / "work",
                    timeout=8,
                )
                action_result = {"exit": archive.returncode, "stdout": archive.stdout[-1000:], "stderr": archive.stderr[-1000:]}
            else:
                action_result = rpc.request("thread/unsubscribe", {"threadId": thread_id})
            after = rpc.request("thread/loaded/list", {})
            read = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            return {
                "thread_id": thread_id,
                "injected": injected.get("result"),
                "durable_rollout": durable_rollout,
                "remote": remote,
                "action": action,
                "before_loaded": before.get("result", {}).get("data"),
                "action_result": action_result,
                "after_loaded": after.get("result", {}).get("data"),
                "read_after": {"status": read.get("result", {}).get("thread", {}).get("status"), "error": read.get("error")},
                "notifications": rpc.notifications,
            }
        finally:
            if isinstance(rpc, WebSocketClient):
                rpc.close()
            server.terminate()
            try:
                server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)
            for stream in (server.stdin, server.stdout):
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default="codex")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--remote", action="store_true", help="connect CLI archive to isolated Unix app server")
    parser.add_argument("--action", choices=("archive", "unsubscribe"), default="archive")
    args = parser.parse_args()
    report = {"app_asar": asar_main_references()}
    if not args.audit_only:
        report["experiment"] = run_experiment(args.cli, remote=args.remote, action=args.action)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
