#!/usr/bin/env python3
"""Exercise real install, setup, worktrees and tmux inside the disposable image."""

import errno
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
import time


def run(*command, check=True):
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError(f"{command!r}\n{result.stdout}\n{result.stderr}")
    return result


class Terminal:
    """A controlling terminal for the setup wizard and real tmux attach/detach."""

    def __init__(self, command):
        self.output = b""
        self.status = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.execvp("zsh", ["zsh", "-lic", command])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.status is None:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.fd)

    def read(self):
        if select.select([self.fd], [], [], 0.1)[0]:
            try:
                self.output += os.read(self.fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)

    def expect(self, text):
        deadline = time.monotonic() + 30
        while text.encode() not in self.output:
            self.read()
            if self.status is not None or time.monotonic() > deadline:
                raise RuntimeError(f"waiting for {text!r}:\n{self.output.decode(errors='replace')}")

    def send(self, keys):
        os.write(self.fd, keys)

    def finish(self):
        deadline = time.monotonic() + 30
        while self.status is None and time.monotonic() < deadline:
            self.read()
        if self.status != 0:
            raise RuntimeError(f"terminal exit {self.status}:\n{self.output.decode(errors='replace')}")


def main():
    home = Path.home()
    # This script creates accounts' settings and kills its tmux server. Refuse
    # accidental execution from a developer checkout on the host.
    if home != Path("/home/tester") or Path(__file__).parent != Path("/opt/playground"):
        raise SystemExit("Run smoke through python3 scripts/playground.py smoke")
    for key in list(os.environ):
        if key.startswith("COV_CORE_"):
            del os.environ[key]
    assert not (home / "bin/t").exists(), "smoke must start with t uninstalled"
    assert not (home / ".config/t").exists(), "smoke must start without t settings"
    assert not (home / ".claude").exists() and not (home / ".codex").exists()
    assert run("tmux", "list-sessions", check=False).returncode != 0
    print("PASS: fresh account, no t, agent configuration, or tmux sessions", flush=True)

    run("playground-install")
    shell_config = (home / ".zshrc").read_text()
    run("playground-install")
    assert (home / ".zshrc").read_text() == shell_config
    run("zsh", "-lic", "t --version && t update --relink && t update")
    assert (home / "bin/t").resolve() == home / "code/t/bin/t"
    guard = run("git", "-C", str(home / "code/t"), "commit", "--allow-empty", "-m", "blocked", check=False)
    assert guard.returncode != 0 and "live t checkout" in guard.stderr
    print("PASS: install, repeated install, login-shell loading, update, and live-main guard", flush=True)

    with Terminal('t setup "$HOME/code/playground" --no-hosts --no-instructions') as terminal:
        terminal.expect("REVIEW CHANGES")
        terminal.send(b" \r")
        terminal.expect("APPLY CHANGES")
        terminal.send(b"y")
        terminal.finish()
    registered = run("zsh", "-lic", 'print -r -- "${DEV_REPOS[playground]}"').stdout.strip()
    assert registered == str(home / "code/playground"), registered
    print("PASS: interactive t setup registers the sample repository", flush=True)

    run("playground-demo-agent")
    try:
        with Terminal("t open playground 1") as terminal:
            terminal.expect("Playground Claude stub")
            pane_pid = run("tmux", "display-message", "-p", "-t", "=dev-playground-1", "#{pane_pid}").stdout
            terminal.send(b"\x02d")
            terminal.finish()
        worktree = home / "code/.worktrees/playground/1"
        assert worktree.is_dir(), worktree
        assert run("git", "-C", str(worktree), "branch", "--show-current").stdout.strip() == "dev/playground-1"
        (worktree / "smoke.txt").write_text("Keep this work across reattachment.\n")
        assert run("git", "-C", str(home / "code/playground"), "status", "--porcelain").stdout == ""
        print("PASS: t open creates an isolated worktree and a live tmux session", flush=True)

        with Terminal("t open playground 1") as terminal:
            terminal.expect("Playground Claude stub")
            assert run("tmux", "display-message", "-p", "-t", "=dev-playground-1", "#{pane_pid}").stdout == pane_pid
            assert run("tmux", "list-sessions", "-F", "#{session_name}").stdout.strip() == "dev-playground-1"
            terminal.send(b"\x02d")
            terminal.finish()
        assert (worktree / "smoke.txt").read_text() == "Keep this work across reattachment.\n"
        assert "playground" in run("zsh", "-lic", "t ls").stdout
        assert "Playground Claude stub" in run("zsh", "-lic", "t read playground 1 --dump").stdout
        run("zsh", "-lic", "t kill playground 1 --yes")
        assert run("tmux", "has-session", "-t", "=dev-playground-1", check=False).returncode != 0
        assert (worktree / "smoke.txt").is_file()
        print("PASS: detach, reattach one owner, list, read, and kill preserve work", flush=True)
    finally:
        run("tmux", "kill-server", check=False)
    print("Playground smoke passed (demo agent; no real AI or authentication).", flush=True)


if __name__ == "__main__":
    main()
