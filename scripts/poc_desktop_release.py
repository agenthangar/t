#!/usr/bin/env python3
"""Disposable two-server Codex app-server thread release experiment.

Run with an absolute Codex binary path. No auth, real sessions, or inference are used.
The script creates its own HOME/CODEX_HOME and synthetic threads.
It prints one JSON object so runs on different installed versions can be compared.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class Rpc:
    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, stderr=None):
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if stderr is None else stderr,
            env=env,
            cwd=cwd,
            start_new_session=True,
            bufsize=0,
        )
        self.pending = b""
        self.notifications: list[dict] = []
        self.next_id = 1

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.proc.stdin.flush()

    def receive(self, timeout: float = 10) -> dict:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            if self.proc.poll() is not None:
                raise RuntimeError(f"app-server client exited: {self.proc.returncode}")
            with selectors.DefaultSelector() as sel:
                sel.register(self.proc.stdout, selectors.EVENT_READ)
                if sel.select(max(0.01, deadline - time.monotonic())):
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("app-server closed connection")
                    self.pending += chunk
        raise TimeoutError("app-server response timed out")

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        msg: dict = {"id": request_id, "method": method, "params": params or {}}
        self.send(msg)
        while True:
            response = self.receive()
            if response.get("id") == request_id:
                if "error" in response:
                    raise RuntimeError(f"{method}: {response['error']}")
                return response.get("result", {})
            if "method" in response:
                self.notifications.append(response)

    def initialize(self, *, experimental: bool = False) -> None:
        self.request(
            "initialize",
            {"clientInfo": {"name": "t-release-poc", "version": "0.1"},
             "capabilities": {"experimentalApi": True} if experimental else {}},
        )
        self.send({"method": "initialized"})

    def drain(self, seconds: float = 0.15) -> list[dict]:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                msg = self.receive(max(0.01, end - time.monotonic()))
            except TimeoutError:
                break
            if "method" in msg:
                self.notifications.append(msg)
        found, self.notifications = self.notifications, []
        return found

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()


def methods(messages: list[dict], thread_id: str) -> list[str]:
    return [
        message["method"]
        for message in messages
        if message.get("params", {}).get("threadId") == thread_id
    ]


def run(codex: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="t-release-poc-") as scratch:
        root = Path(scratch)
        for name in ("home", "codex", "config", "cache", "data", "state", "tmp", "repo"):
            (root / name).mkdir()
        env = dict(
            PATH=os.defpath,
            TERM="xterm-256color",
            LANG="C",
            HOME=str(root / "home"),
            CODEX_HOME=str(root / "codex"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_CACHE_HOME=str(root / "cache"),
            XDG_DATA_HOME=str(root / "data"),
            XDG_STATE_HOME=str(root / "state"),
            TMPDIR=str(root / "tmp"),
        )
        (root / "codex" / "config.toml").write_text(
            'model_provider = "poc"\nmodel = "poc"\n'
            '[features]\nplugins = false\n'
            '[model_providers.poc]\nname = "No inference POC"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\n'
        )
        version = subprocess.check_output([str(codex), "--version"], env=env, cwd=root / "repo", text=True).strip()
        clients: list[Rpc] = []
        try:
            for _ in range(2):
                client = Rpc([str(codex), "app-server", "--stdio"], env, root / "repo")
                clients.append(client)
                client.initialize()
            desktop, observer = clients
            target = desktop.request("thread/start", {"cwd": str(root / "repo"), "approvalPolicy": "never"})["thread"]["id"]
            control = desktop.request("thread/start", {"cwd": str(root / "repo"), "approvalPolicy": "never"})["thread"]["id"]
            injected = None
            try:
                injected = desktop.request(
                    "thread/inject_items",
                    {"threadId": target, "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic handoff fixture"}]}]},
                )
            except RuntimeError as exc:
                injected = str(exc).replace(target, "<target>")
            before = desktop.request("thread/loaded/list")["data"]
            competing_resume = None
            try:
                competing_resume = observer.request("thread/resume", {"threadId": target})["thread"]["id"]
            except RuntimeError as exc:
                competing_resume = str(exc).replace(target, "<target>")
            unsubscribe = desktop.request("thread/unsubscribe", {"threadId": target})
            after_unsubscribe = desktop.request("thread/loaded/list")["data"]
            observer.drain()
            desktop.drain()
            archive = desktop.request("thread/archive", {"threadId": target})
            after_archive = desktop.request("thread/loaded/list")["data"]
            archived = desktop.request("thread/list", {"archived": True, "cwd": str(root / "repo")})
            active = desktop.request("thread/list", {"cwd": str(root / "repo")})
            archive_notifications = {
                "desktop": methods(desktop.drain(), target),
                "observer": methods(observer.drain(), target),
            }
            resume_while_archived = None
            try:
                resume_while_archived = observer.request("thread/resume", {"threadId": target})["thread"]["id"]
            except RuntimeError as exc:
                resume_while_archived = str(exc).replace(target, "<target>")
            read_while_archived = None
            try:
                read_while_archived = desktop.request("thread/read", {"threadId": target, "includeTurns": True})["thread"]["id"]
            except RuntimeError as exc:
                read_while_archived = str(exc).replace(target, "<target>")
            archive_files = list((root / "codex" / "archived_sessions").glob("*.jsonl"))
            fixture_in_archive = any(b"synthetic handoff fixture" in path.read_bytes() for path in archive_files)
            unarchive = observer.request("thread/unarchive", {"threadId": target})
            after_unarchive = desktop.request("thread/loaded/list")["data"]
            resumed = observer.request("thread/resume", {"threadId": target})["thread"]["id"]
            after_resume = observer.request("thread/loaded/list")["data"]
            resumed_files = list((root / "codex" / "sessions").rglob("*.jsonl"))
            fixture_after_resume = any(b"synthetic handoff fixture" in path.read_bytes() for path in resumed_files)
            # A stale client may try to rejoin after archive/unarchive; record whether
            # the API permits it, since that limits any one-live-owner claim.
            stale_client_resume = None
            try:
                stale_client_resume = desktop.request("thread/resume", {"threadId": target})["thread"]["id"]
            except RuntimeError as exc:
                stale_client_resume = str(exc).replace(target, "<target>")
            remote_target = desktop.request("thread/start", {"cwd": str(root / "repo"), "approvalPolicy": "never"})["thread"]["id"]
            desktop.request(
                "thread/inject_items",
                {"threadId": remote_target, "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic remote archive fixture"}]}]},
            )
            remote_archive = None
            try:
                remote_archive = observer.request("thread/archive", {"threadId": remote_target})
            except RuntimeError as exc:
                remote_archive = str(exc).replace(remote_target, "<remote_target>")
            remote_loaded = remote_target in desktop.request("thread/loaded/list")["data"]
            remote_notifications = methods(desktop.drain(), remote_target)
            return {
                "version": version,
                "inference_requested": False,
                "injected": injected,
                "competing_resume_before_release": competing_resume,
                "before": {"target_loaded": target in before, "control_loaded": control in before},
                "unsubscribe": {"status": unsubscribe.get("status"), "target_loaded": target in after_unsubscribe},
                "archive": {
                    "response": archive,
                    "target_loaded": target in after_archive,
                    "control_loaded": control in after_archive,
                    "in_archived_list": any(row.get("id") == target for row in archived.get("data", [])),
                    "in_active_list": any(row.get("id") == target for row in active.get("data", [])),
                    "notifications": archive_notifications,
                    "read_while_archived": "same_id" if read_while_archived == target else read_while_archived,
                    "resume_while_archived": "same_id" if resume_while_archived == target else resume_while_archived,
                    "fixture_preserved_in_archive": fixture_in_archive,
                },
                "unarchive": {
                    "same_id": unarchive.get("thread", {}).get("id") == target,
                    "loaded_before_resume": target in after_unarchive,
                    "resume_same_id": resumed == target,
                    "loaded_after_resume": target in after_resume,
                    "fixture_preserved_after_resume": fixture_after_resume,
                },
                "stale_client_resume_same_id": stale_client_resume == target,
                "stale_client_resume": "same_id" if stale_client_resume == target else stale_client_resume,
                "archive_from_other_engine": {
                    "response": remote_archive,
                    "still_loaded_in_original": remote_loaded,
                    "original_notifications": remote_notifications,
                },
            }
        finally:
            for client in clients:
                client.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} /absolute/path/to/codex")
    print(json.dumps(run(Path(sys.argv[1]).resolve()), sort_keys=True))
