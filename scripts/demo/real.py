#!/usr/bin/env python3
"""Capture a disposable local t session with the authenticated Codex CLI.

Run only after the release is installed and `codex login status` says logged in.
This uses the real user's Codex login, but an isolated repo, t config, cache, and
tmux socket. It never copies credentials into the repo or generated assets.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from render import MEDIA, call, open_in_pty, render

PROMPT = "Reply with the exact phrase Session ready. Do not read or modify files."
TOOL_ROOT = Path(os.environ.get("T_DEMO_TOOL_ROOT", str((Path.home() / "bin/t").resolve().parents[1])))


def tmux(env, *args):
    return call(["tmux", *args], env=env)


def capture_real():
    auth = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=10)
    if auth.returncode or "Logged in" not in (auth.stdout + auth.stderr):
        raise RuntimeError("Codex CLI is not logged in on this machine")

    with tempfile.TemporaryDirectory(prefix="t-real-demo-") as directory:
        base = Path(directory)
        repo = base / "demo"
        repo.mkdir()
        call(["git", "init", "-q", "-b", "main"], cwd=repo)
        call(["git", "config", "user.email", "demo@example.invalid"], cwd=repo)
        call(["git", "config", "user.name", "Demo"], cwd=repo)
        (repo / "README.md").write_text("# Disposable t demo\n", encoding="utf-8")
        call(["git", "add", "."], cwd=repo)
        call(["git", "commit", "-qm", "Start demo"], cwd=repo)
        origin = base / "origin.git"
        call(["git", "init", "-q", "--bare", str(origin)])
        call(["git", "remote", "add", "origin", str(origin)], cwd=repo)
        call(["git", "push", "-qu", "origin", "main"], cwd=repo)

        local = base / "local.zsh"
        local.write_text(f'DEV_REPOS[demo]="{repo}"\nDEV_WORKTREE_ROOT="{base}/worktrees"\nDEV_AGENT[demo]=codex\n', encoding="utf-8")
        socket = base / "tmux"
        socket.mkdir()
        # The shared app server is unavailable in the isolated demo environment;
        # launch the same authenticated CLI directly with its supported fallback.
        codex_binary = shutil.which("codex")
        if not codex_binary:
            raise RuntimeError("Codex CLI is not installed")
        wrapper_dir = base / "bin"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "codex"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(codex_binary) + ' --no-daemon "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        zdotdir = base / "zsh"
        zdotdir.mkdir()
        (zdotdir / ".zshrc").write_text(f'export PATH="{wrapper_dir}:{TOOL_ROOT / "bin"}:$PATH"\nsource "{TOOL_ROOT}/t.plugin.zsh"\n', encoding="utf-8")
        env = {**os.environ, "T_LOCAL_RC": str(local), "XDG_CONFIG_HOME": str(base / "config"),
               "XDG_CACHE_HOME": str(base / "cache"), "XDG_STATE_HOME": str(base / "state"),
               "TMUX_TMPDIR": str(socket), "TMUX": "", "TERM": "xterm-256color",
               "ZDOTDIR": str(zdotdir), "T_NO_AUTORELOAD": "1",
               "PATH": f"{wrapper_dir}:{TOOL_ROOT / 'bin'}:{os.environ['PATH']}"}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        source = f'source "{TOOL_ROOT}/t.plugin.zsh"; '
        try:
            before = call(["zsh", "-fc", source + "t ls demo"], cwd=repo, env=env)
            opened = open_in_pty(source + "t open demo --codex --new --local", cwd=repo, env=env)
            tmux(env, "has-session", "-t", "=dev-demo-1")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                command = tmux(env, "list-panes", "-t", "dev-demo-1", "-F", "#{pane_current_command}").strip()
                if command == "codex":
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Codex did not start in the t tmux slot")

            pane = tmux(env, "capture-pane", "-p", "-t", "dev-demo-1")
            if "Trust this folder?" in pane:
                # This checkout was created above from our one-line README. Codex
                # asks once before opening any unfamiliar folder.
                for _ in range(3):
                    tmux(env, "send-keys", "-t", "dev-demo-1", "1")
                    time.sleep(0.4)
                    tmux(env, "send-keys", "-t", "dev-demo-1", "Enter")
                    time.sleep(1.5)
                    pane = tmux(env, "capture-pane", "-p", "-t", "dev-demo-1")
                    if "Trust this folder?" not in pane:
                        break
            if any(marker in pane for marker in ("Trust all and continue", "Trust this folder?", "Sign in", "Log in")):
                raise RuntimeError("Codex is waiting for an interactive trust or login decision in the demo slot")
            tmux(env, "send-keys", "-t", "dev-demo-1", PROMPT)
            time.sleep(1)
            tmux(env, "send-keys", "-t", "dev-demo-1", "Enter")
            time.sleep(2)
            pane = tmux(env, "capture-pane", "-p", "-t", "dev-demo-1")
            if "› " + PROMPT in pane:
                tmux(env, "send-keys", "-t", "dev-demo-1", "C-m")
            deadline = time.monotonic() + int(os.environ.get("T_DEMO_WAIT_SECONDS", "90"))
            answer = ""
            while time.monotonic() < deadline:
                pane = tmux(env, "capture-pane", "-p", "-t", "dev-demo-1")
                answer = next((line.strip() for line in pane.splitlines()
                               if line.strip().startswith("• Session ready.")), "")
                if answer:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Codex did not complete the harmless demo prompt; pane tail: " + repr(pane[-700:]))

            after = call(["zsh", "-fc", source + "t ls demo"], cwd=repo, env=env)
            worktree = call(["zsh", "-fc", source + "t cd demo 1; pwd"], cwd=repo, env=env).strip()
            if "demo-1" not in after or "(no active session)" in after:
                raise RuntimeError("t ls did not identify an active Codex conversation")
            if not (Path(worktree) / ".git").exists():
                raise RuntimeError("t cd did not enter the disposable worktree")
            opened_line = next((line for line in re.split(r"[\r\n]", opened)
                                if "Starting dev-demo-1" in line), "")
            opened_line = re.sub(r" in \S+ · codex \(logging to \S+\)",
                                 " in ~/worktrees/demo/1 · codex", opened_line)
            return {"before": before.strip(), "opened": opened_line, "after": after.strip(),
                    "worktree": worktree.replace(str(base), "~"), "response": answer}
        finally:
            subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true", help="check the workflow without overwriting launch assets")
    parser.add_argument("--render-capture", action="store_true", help="render saved verified output without another model request")
    args = parser.parse_args()
    capture_file = MEDIA / "demo-capture.json"
    if args.render_capture:
        outputs = json.loads(capture_file.read_text(encoding="utf-8"))
    else:
        outputs = capture_real()
        MEDIA.mkdir(parents=True, exist_ok=True)
        capture_file.write_text(json.dumps(outputs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for key in ("before", "opened", "response", "after", "worktree"):
        value = outputs[key]
        if key == "opened":
            value = next((line for line in re.split(r"[\r\n]", value) if "Starting dev-demo-1" in line), "")
        print(f"{key}: {value}")
    if not args.capture_only:
        render(outputs, real_agent=True)
