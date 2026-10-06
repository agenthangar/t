#!/usr/bin/env python3
"""Probe whether two Codex backends can load one synthetic conversation.

This does not contact a model, read an existing Codex home, or touch a live
desktop process. Each run creates its own state and stops only its own children.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
import uuid


class RPC:
    def __init__(self, binary, env, cwd, errors):
        self.process = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"], env=env, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            start_new_session=True,
        )
        self.pending = b""
        self.next_id = 0

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 15
        while True:
            if b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                reply = json.loads(line)
                if reply.get("id") == request_id:
                    if "error" in reply:
                        raise RuntimeError(f"{method}: {reply['error']}")
                    return reply["result"]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError(method)
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(f"backend exited during {method}")
            self.pending += chunk

    def initialize(self):
        self.call("initialize", {"clientInfo": {"name": "t-duplicate-owner-poc", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})


def run(binary):
    with tempfile.TemporaryDirectory(prefix="t-duplicate-owner-poc-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        env = {"PATH": os.defpath, "TERM": "xterm-256color", "LANG": "C"}
        for key in ("HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                    "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR"):
            target = root / key.lower()
            target.mkdir()
            env[key] = str(target)
        home = Path(env["CODEX_HOME"])
        (home / "config.toml").write_text(
            'model_provider = "poc"\nmodel = "poc"\n'
            '[features]\nplugins = false\n'
            '[model_providers.poc]\nname = "No inference POC"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\n'
        )
        worktree = root / "worktree"
        worktree.mkdir()
        sid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        directory = home / "sessions" / now.strftime("%Y/%m/%d")
        directory.mkdir(parents=True)
        rollout = directory / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": sid, "session_id": sid,
                "timestamp": timestamp, "cwd": str(worktree), "originator": "codex-tui",
                "cli_version": "0.159.0", "source": "cli"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Synthetic handoff history."}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Synthetic saved answer."}]}},
        ]
        rollout.write_text("".join(json.dumps(dict(record, timestamp=timestamp)) + "\n"
                                   for record in records))
        servers = []
        for index in range(2):
            errors = stack.enter_context((root / f"server-{index}.log").open("wb"))
            rpc = RPC(binary, env, worktree, errors)
            stack.callback(rpc.close)
            rpc.initialize()
            servers.append(rpc)
        first, second = servers
        first_thread = first.call("thread/resume", {"threadId": sid, "path": str(rollout),
                                                    "cwd": str(worktree)})["thread"]
        report = {"binary_version": subprocess.run([binary, "--version"], env=env, cwd=worktree,
                   capture_output=True, text=True, check=True).stdout.strip(),
                  "first_resume_same_id": first_thread["id"] == sid}
        try:
            second_thread = second.call("thread/resume", {"threadId": sid, "path": str(rollout),
                                                         "cwd": str(worktree)})["thread"]
            report["second_resume_same_id"] = second_thread["id"] == sid
        except RuntimeError as exc:
            report["second_resume_error"] = str(exc).replace(str(root), "<temporary>").replace(sid, "<thread>")
        pids = [rpc.call("server/diagnostics", {})["process"]["id"] for rpc in servers]
        loaded = [sid in rpc.call("thread/loaded/list", {})["data"] for rpc in servers]
        report.update(distinct_backend_pids=pids[0] != pids[1],
                      target_loaded_in_first=loaded[0], target_loaded_in_second=loaded[1],
                      duplicate_live_contexts=pids[0] != pids[1] and all(loaded),
                      inference_requested=False)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"), help="Codex executable to probe")
    args = parser.parse_args()
    if not args.codex:
        parser.error("codex is not installed; pass --codex /path/to/codex")
    print(json.dumps(run(str(Path(args.codex).resolve())), indent=2))


if __name__ == "__main__":
    main()
