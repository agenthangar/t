"""Exercise real menu input and Cursor recovery on disposable tmux servers."""

import json
import fcntl
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import sys
import struct
import termios
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import load_script, REPO_ROOT

SID = "01234567-89ab-cdef-0123-456789abcdef"
pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("zsh"), reason="tmux and zsh required")


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "ZDOTDIR": str(tmp_path),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "TERM": "xterm-256color", "SHELL": shutil.which("zsh"),
           "T_RECOVERY_DISABLE": "1"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TMUX", raising=False)
    for key in list(os.environ):
        if key.startswith("COV_CORE_"):
            monkeypatch.delenv(key)
    (tmp_path / ".zshenv").write_text("skip_global_compinit=1\n")
    (tmp_path / ".zshrc").write_text("")
    command = [shutil.which("tmux"), "-L", "t-recovery-" + uuid.uuid4().hex, "-f", os.devnull]
    def tmux(*args):
        return subprocess.run(command + list(args), env=env, capture_output=True, text=True, timeout=5)
    mod = load_script(REPO_ROOT / "libexec/t_recovery.py", "recovery_real")
    try:
        yield mod, tmux, command, env, tmp_path
    finally:
        tmux("kill-server")


def until(check, timeout=5, detail=lambda: ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    pytest.fail("timed out waiting for isolated tmux state: " + detail())


@pytest.mark.parametrize("choice", ["q", "r"])
def test_real_dead_pane_menu_resumes_exact_thread(terminal, choice):
    mod, tmux, command, env, home = terminal
    bins = home / "bin"
    bins.mkdir()
    help_command = bins / "codex"
    help_command.write_text('#!/bin/sh\necho --no-daemon\n')
    help_command.chmod(0o755)
    (home / "dirty.txt").write_text("uncommitted edits")
    (home / ".zshrc").write_text(f'''
export PATH={shlex.quote(str(bins))}:$PATH
source {shlex.quote(str(REPO_ROOT / 'zsh/resume.zsh'))}
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{
  [[ $(tmux display-message -p -t '=dev-api-1:' '#{{pane_dead}}') == 0 ]] || return 1
  tmux display-message -p -t '=dev-api-1:' '#{{pane_pid}}'
}}
_dev_app_slot_reserved() {{ return 1; }}
_dev_agent_resume_cmd() {{ print -r -- "codex resume $2"; }}
codex() {{ print -r -- "$PWD|$*" > "$HOME/resumed"; sleep 60; }}
''')
    launch = "printf 'Working...\\nunsent draft\\n'; while [[ ! -e $HOME/exit-now ]]; do sleep 0.03; done; exit 143"
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "zsh", "-lc", launch).returncode == 0
    assert tmux("set-option", "-p", "-t", "=dev-api-1:", "remain-on-exit", "on").returncode == 0
    (home / "exit-now").touch()
    until(lambda: tmux("display-message", "-p", "-t", "=dev-api-1:", "#{pane_dead_status}").stdout.strip() == "143")
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    assert pane["pane_dead"] == "1"
    target = mod.owner(socket, pane)
    assert target["pid"] == ""
    watcher = mod.Watcher(socket)  # The monitor may first see an already-dead pane.
    watcher.poll(); watcher.poll()
    assert not list(mod.cache(socket).glob("*.offer"))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    client = subprocess.Popen(command + ["attach-session", "-t", "=dev-api-1"],
                              env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    try:
        until(lambda: mod.clients(socket, pane["pane"]))
        watcher.poll()
        visible = bytearray()
        def menu_visible():
            if select.select([master], [], [], 0.05)[0]:
                visible.extend(os.read(master, 65536))
            return b"AGENT EXITED" in visible and b"Save draft and restart" in visible
        until(menu_visible, detail=lambda: repr(bytes(visible[-1000:])))
        os.write(master, choice.encode())
        until(lambda: not list(mod.cache(socket).glob("*.offer")))
        if choice == "r":
            until(lambda: (home / "resumed").exists(), detail=lambda: tmux("capture-pane", "-p", "-t", pane["pane"]).stdout)
            assert (home / "resumed").read_text().strip() == f"{home}|resume {SID} --no-daemon"
            assert not mod.current(target)  # The old death is stale.
            until(lambda: not (home / "cache/t/restart/dev-api-1.lock").exists())
            saved = list((home / "cache/t/restart").iterdir())
            assert len(saved) == 1 and "unsent draft" in saved[0].read_text()
            assert saved[0].stat().st_mode & 0o777 == 0o600
            assert mod.panes(socket)[0]["pane_pid"] != pane["pane_pid"]
        else:
            assert not (home / "resumed").exists()
            assert mod.panes(socket)[0]["pane_dead"] == "1"
            watcher.poll()
            assert not list(mod.cache(socket).glob("*.offer"))
        assert mod.panes(socket)[0]["pane"] == pane["pane"]
        assert (home / "dirty.txt").read_text() == "uncommitted edits"
    finally:
        client.terminate()
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait(timeout=5)
        os.close(master)


@pytest.mark.parametrize("width", [40, 80])
@pytest.mark.parametrize("choice,restart_fails", [("q", False), ("r", False), ("\r", False), ("r", True)])
def test_real_menu_dismiss_or_accept_without_typing_into_agent(terminal, width, choice, restart_fails):
    mod, tmux, command, env, home = terminal
    restart_result = "print -u2 'restart refused'; return 1" if restart_fails else "print 'Saved visible draft'"
    (home / ".zshrc").write_text(f'''
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{ tmux display-message -p -t '=dev-api-1:' '#{{pane_pid}}'; }}
_t_restart_slot() {{ print -r -- "$*" > "$HOME/accepted"; {restart_result}; }}
''')
    launch = "printf 'Server connection could not be restored\\n'; exec sleep 60"
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "zsh", "-lc", launch).returncode == 0
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    until(lambda: "Server connection" in tmux("capture-pane", "-p", "-t", pane["pane"]).stdout)
    target = mod.owner(socket, pane)
    assert target["sid"] == SID
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    client = subprocess.Popen(command + ["attach-session", "-t", "=dev-api-1"],
                              env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    assert tmux("new-session", "-d", "-s", "unrelated", "sleep", "60").returncode == 0
    other_master, other_slave = pty.openpty()
    other_client = subprocess.Popen(command + ["attach-session", "-t", "=unrelated"],
                                    env=env, stdin=other_slave, stdout=other_slave, stderr=other_slave,
                                    start_new_session=True)
    os.close(other_slave)
    os.set_blocking(other_master, False)
    try:
        until(lambda: mod.clients(socket, pane["pane"]))
        until(lambda: len(tmux("list-clients").stdout.splitlines()) == 2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            showing = pool.submit(mod.offer, target, "Server connection could not be restored")
            assert showing.result(timeout=2)  # Monitoring continues while input is pending.
            visible = bytearray()
            def menu_visible():
                if select.select([master], [], [], 0.05)[0]:
                    visible.extend(os.read(master, 65536))
                return b"CONNECTION FAILED" in visible and b"Save draft and restart" in visible
            until(menu_visible, detail=lambda: repr(bytes(visible[-1000:])))
            assert b"Save draft and restart" not in os.read(other_master, 65536)
            offer = next(mod.cache(socket).glob("*.offer"))
            mod.write_json(offer, {**mod.read_json(offer), "created": time.time() - 3600})
            mod.Watcher(socket).poll()
            assert offer.exists()  # A menu left open must not silently expire.
            os.write(master, choice.encode())
        offers = lambda: list(mod.cache(socket).glob("*.offer"))
        until(lambda: not offers())
        if choice == "r":
            until(lambda: (home / "accepted").exists(), detail=lambda: os.read(master, 65536).decode(errors="replace"))
            assert (home / "accepted").read_text().split() == ["dev-api-1", str(home), SID, "codex", "restart", target["pid"]]
            message = b"t recovery: restart refused" if restart_fails else b"Saved visible draft"
            def result_visible():
                if select.select([master], [], [], 0.05)[0]:
                    visible.extend(os.read(master, 65536))
                return message in visible
            until(result_visible, detail=lambda: repr(bytes(visible[-1000:])))
            other_output = bytearray()
            while select.select([other_master], [], [], 0.05)[0]:
                other_output.extend(os.read(other_master, 65536))
            assert message not in other_output
        else:
            assert not (home / "accepted").exists()
        until(lambda: 't_recovery.py accept' not in tmux("show-messages", "-J").stdout)
        for session in ("dev-api-1", "unrelated"):
            assert tmux("display-message", "-p", "-t", "=" + session + ":", "#{pane_in_mode}").stdout.strip() == "0"
        # Menu keystrokes never reach the client process, which remains untouched.
        assert tmux("display-message", "-p", "-t", "=dev-api-1:", "#{pane_pid}").stdout.strip() == pane["pane_pid"]
    finally:
        client.terminate()
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait(timeout=5)
        os.close(master)
        other_client.terminate()
        other_client.wait(timeout=5)
        os.close(other_master)


@pytest.mark.parametrize("action", ["accept", "dismiss", "reopen"])
def test_obsolete_callback_never_covers_agent_with_tmux_output(terminal, action):
    mod, tmux, _, _, home = terminal
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "sleep", "60").returncode == 0
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    # Older menus carry no pane/viewer arguments, and can outlive their offer.
    args = [socket, "f" * 32] if action != "reopen" else [socket, "%99999", "absent"]
    callback = shlex.join([sys.executable, str(Path(mod.__file__)), action, *args])
    assert tmux("run-shell", "-t", pane["pane"], callback).returncode == 0
    assert tmux("display-message", "-p", "-t", pane["pane"], "#{pane_in_mode}").stdout.strip() == "0"
    assert mod.panes(socket)[0]["pane_pid"] == pane["pane_pid"]


@pytest.mark.parametrize("custom", [False, True])
def test_status_shortcut_reopens_dismissed_menu_and_clears_after_recovery(terminal, custom):
    mod, tmux, command, env, home = terminal
    (home / ".zshrc").write_text(f'''
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{ tmux display-message -p -t '=dev-api-1:' '#{{pane_pid}}'; }}
_t_restart_slot() {{ touch "$HOME/accepted"; }}
''')
    launch = "printf 'Server connection could not be restored\\n'; exec sleep 60"
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "zsh", "-lc", launch).returncode == 0
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    tmux("set-environment", "-t", "=dev-api-1", "DEV_AGENT", "codex")
    tmux("set-option", "-g", "status-left", "original ")
    tmux("set-option", "-g", "status-left-length", "10")
    if custom:
        tmux("set-option", "-t", "=dev-api-1:", "prefix", "C-a")
        tmux("set-option", "-t", "=dev-api-1:", "status-left", "custom ")
        tmux("set-option", "-t", "=dev-api-1:", "status-left-length", "7")
        tmux("bind-key", "-T", "prefix", "R", "display-message", "my existing binding")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
    client = subprocess.Popen(command + ["attach-session", "-t", "=dev-api-1"],
                              env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    try:
        until(lambda: mod.clients(socket, pane["pane"]))
        watcher = mod.Watcher(socket)
        watcher.poll(); watcher.poll()
        assert watcher.key == ("M-r" if custom else "R")
        assert mod.recovery_key(socket) == watcher.key  # Re-exec reuses our binding.
        hint = "Recovery: Ctrl-a Alt-r" if custom else "Recovery: Ctrl-b R"
        assert hint in tmux("show-options", "-v", "-t", "=dev-api-1:", "status-left").stdout
        visible = bytearray()
        def showing(text):
            if select.select([master], [], [], 0.05)[0]:
                visible.extend(os.read(master, 65536))
            return text in visible
        until(lambda: showing(b"CONNECTION FAILED"))
        os.write(master, b"q")
        until(lambda: not list(mod.cache(socket).glob("*.offer")))
        visible.clear()
        until(lambda: showing(hint.encode()))
        visible.clear()
        os.write(master, b"\x01\x1br" if custom else b"\x02R")
        until(lambda: showing(b"CONNECTION FAILED"), detail=lambda: repr(bytes(visible[-1000:])))
        assert len(list(mod.cache(socket).glob("*.offer"))) == 1
        assert not (home / "accepted").exists()
        os.write(master, b"q")
        until(lambda: not list(mod.cache(socket).glob("*.offer")))
        # Only the isolated fixture's fake agent is replaced to clear its error.
        tmux("respawn-pane", "-k", "-t", pane["pane"], "sh", "-c", "printf 'healthy\\n'; exec sleep 60")
        until(lambda: "healthy" in tmux("capture-pane", "-p", "-t", pane["pane"]).stdout)
        watcher.poll()
        assert tmux("show-options", "-A", "-v", "-t", "=dev-api-1:", "status-left").stdout == ("custom \n" if custom else "original \n")
        assert tmux("show-options", "-A", "-v", "-t", "=dev-api-1:", "status-left-length").stdout.strip() == ("7" if custom else "10")
        if custom:
            assert "my existing binding" in tmux("list-keys", "-T", "prefix").stdout
        else:
            assert not tmux("show-options", "-q", "-t", "=dev-api-1:", "status-left").stdout
        assert not mod.hint_path(socket, pane["pane"]).exists()
    finally:
        client.terminate()
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait(timeout=5)
        os.close(master)


@pytest.mark.parametrize("width", [40, 80])
def test_real_unverified_notice_is_visible_and_dismissible(terminal, width):
    mod, tmux, command, env, home = terminal
    launch = "printf 'Server connection could not be restored\\nunsent draft\\n'; exec sleep 60"
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "zsh", "-lc", launch).returncode == 0
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    until(lambda: "unsent draft" in tmux("capture-pane", "-p", "-t", pane["pane"]).stdout)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    client = subprocess.Popen(command + ["attach-session", "-t", "=dev-api-1"],
                              env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    try:
        until(lambda: mod.clients(socket, pane["pane"]))
        assert mod.unverified_notice(socket, pane["pane"])
        visible = bytearray()
        def menu_visible():
            if select.select([master], [], [], 0.05)[0]:
                visible.extend(os.read(master, 65536))
            return b"RECOVERY NEEDS ATTENTION" in visible and b"Dismiss" in visible
        until(menu_visible, detail=lambda: repr(bytes(visible[-1000:])))
        assert b"unsent draft" in visible
        visible.clear()
        os.write(master, b"q")
        def draft_redrawn():
            if select.select([master], [], [], 0.05)[0]:
                visible.extend(os.read(master, 65536))
            return b"unsent draft" in visible
        until(draft_redrawn, detail=lambda: repr(bytes(visible[-1000:])))
        assert not (home / "accepted").exists()
        assert tmux("display-message", "-p", "-t", "=dev-api-1:", "#{pane_pid}").stdout.strip() == pane["pane_pid"]
    finally:
        client.terminate()
        client.wait(timeout=5)
        os.close(master)


def test_real_verified_offer_follows_dismissed_unverified_notice(terminal, monkeypatch):
    mod, tmux, command, env, home = terminal
    (home / ".zshrc").write_text(f'''
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{ tmux display-message -p -t '=dev-api-1:' '#{{pane_pid}}'; }}
_t_restart_slot() {{ print -r -- "$*" > "$HOME/accepted"; }}
''')
    launch = "printf 'Server connection could not be restored\\nunsent draft\\n'; exec sleep 60"
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "zsh", "-lc", launch).returncode == 0
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    target = until(lambda: mod.owner(socket, pane))
    assert tmux("set-environment", "-t", "=dev-api-1", "DEV_AGENT", "codex").returncode == 0
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
    client = subprocess.Popen(command + ["attach-session", "-t", "=dev-api-1"],
                              env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    try:
        until(lambda: mod.clients(socket, pane["pane"]))
        known_owner = [False]
        monkeypatch.setattr(mod, "owner", lambda *a, **kw: target if known_owner[0] else {})
        watcher = mod.Watcher(socket)
        watcher.poll()
        watcher.poll()
        visible = bytearray()
        def visible_title(title):
            if select.select([master], [], [], 0.05)[0]:
                visible.extend(os.read(master, 65536))
            return title in visible
        until(lambda: visible_title(b"RECOVERY NEEDS ATTENTION"))
        visible.clear()
        known_owner[0] = True
        watcher.poll()
        assert not list(mod.cache(socket).glob("*.offer"))
        os.write(master, b"q")
        until(lambda: watcher.poll() and visible_title(b"CONNECTION FAILED"),
              detail=lambda: repr(bytes(visible[-1000:])))
        os.write(master, b"q")
        until(lambda: not list(mod.cache(socket).glob("*.offer")))
        assert not (home / "accepted").exists()
    finally:
        client.terminate()
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait(timeout=5)
        os.close(master)


def test_real_cursor_hook_restart_and_changed_chat_refusal(terminal):
    mod, tmux, _, env, home = terminal
    bins = home / "bin"
    bins.mkdir()
    executable = bins / "cursor-agent"
    executable.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys, time
subprocess.run([sys.executable, {str(REPO_ROOT / 'libexec/t_recovery.py')!r}, 'cursor-hook'],
               input=json.dumps({{"conversation_id": {SID!r}, "cwd": os.getcwd()}}), text=True, check=True)
print('Connection failed\\nunsent draft', flush=True)
time.sleep(60)
''')
    executable.chmod(0o755)
    (home / ".zshrc").write_text("export PATH=" + shlex.quote(str(bins)) + ":$PATH\n")
    session = "t-cursor-" + SID
    assert tmux("new-session", "-d", "-s", session, "-c", str(home), "zsh", "-lic", "exec cursor-agent --resume=" + SID).returncode == 0
    socket = tmux("display-message", "-p", "-t", "=" + session + ":", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    target = until(lambda: mod.owner(socket, pane), detail=lambda: tmux("capture-pane", "-p", "-t", pane["pane"]).stdout + repr(pane))
    until(lambda: "unsent draft" in tmux("capture-pane", "-p", "-t", pane["pane"]).stdout)
    (home / "dirty.txt").write_text("uncommitted edits")
    saved = Path(mod.cursor_restart(target))
    assert "unsent draft" in saved.read_text()
    assert saved.stat().st_mode & 0o777 == 0o600
    new_pane = mod.panes(socket)[0]
    resumed = until(lambda: mod.owner(socket, new_pane))
    assert resumed["pid"] != target["pid"]
    assert resumed["sid"] == SID and resumed["pane"] == target["pane"]
    assert (home / "dirty.txt").read_text() == "uncommitted edits"
    with pytest.raises(ValueError, match="changed"):
        mod.cursor_restart(target)


def test_real_codex_startup_fallback_preserves_pane_and_uses_no_daemon(terminal):
    mod, tmux, _, env, home = terminal
    bins = home / 'bin'
    bins.mkdir()
    executable = bins / 'codex'
    executable.write_text(f'''#!{sys.executable}
import os, pathlib, sys, time
if '--help' in sys.argv:
    print('--no-daemon')
    sys.exit(0)
pathlib.Path({str(home / 'agent-pid')!r}).write_text(str(os.getpid()))
if '--no-daemon' in sys.argv:
    pathlib.Path({str(home / 'resumed')!r}).write_text(' '.join(sys.argv[1:]))
    print('Ready without daemon', flush=True)
else:
    print('Cannot use the background server\\nExperimental feature request failed\\n1. Run without daemon this time\\nRestart cannot resolve this compatibility check.\\n2. Cancel', flush=True)
time.sleep(60)
''')
    executable.chmod(0o755)
    (home / '.zshrc').write_text(f'''
export PATH={shlex.quote(str(bins))}:$PATH
source {shlex.quote(str(REPO_ROOT / 'zsh/resume.zsh'))}
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ :; }}
_dev_session_claude_pid() {{ cat "$HOME/agent-pid"; }}
_dev_app_slot_reserved() {{ return 1; }}
_dev_agent_at_welcome() {{ return 0; }}
_dev_agent_new_cmd() {{ print 'codex --model chosen'; }}
''')
    assert tmux('new-session', '-d', '-s', 'dev-api-1', '-c', str(home), 'zsh', '-lic', 'exec codex --model chosen').returncode == 0
    socket = tmux('display-message', '-p', '-t', '=dev-api-1:', '#{socket_path}').stdout.strip()
    pane = mod.panes(socket)[0]
    until(lambda: '2. Cancel' in tmux('capture-pane', '-p', '-t', pane['pane']).stdout)
    restart_env = dict(env, TMUX=socket + ',0,0')
    agent_pid = (home / 'agent-pid').read_text()
    command = shlex.join(['_t_restart_slot', 'dev-api-1', str(home), '', 'codex', 'restart-no-daemon', agent_pid])
    restarted = subprocess.run(['zsh', '-lic', command], env=restart_env, capture_output=True, text=True, timeout=15)
    status = subprocess.run(['ps', '-p', agent_pid, '-o', 'pid=,ppid=,stat=,args='], capture_output=True, text=True)
    assert restarted.returncode == 0, restarted.stderr + status.stdout + tmux('display-message', '-p', '-t', pane['pane'], '#{pane_dead} #{pane_pid}').stdout
    until(lambda: (home / 'resumed').exists())
    assert (home / 'resumed').read_text() == '--model chosen --no-daemon'
    assert mod.panes(socket)[0]['pane'] == pane['pane']
    saved = list((home / 'cache/t/restart').iterdir())
    assert len(saved) == 1 and 'Experimental feature request failed' in saved[0].read_text()


@pytest.mark.parametrize("error", ["invalid_cwd", "connection"])
def test_real_recovery_resumes_exact_thread_without_daemon(terminal, error):
    mod, tmux, _, env, home = terminal
    error = mod.INVALID_CWD if error == "invalid_cwd" else "Server connection could not be restored"
    worktree = home / 'worktree with spaces'
    worktree.mkdir()
    (worktree / 'dirty.txt').write_text('uncommitted edits')
    bins = home / 'bin'
    bins.mkdir()
    codex = bins / 'codex'
    codex.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
if '--help' in sys.argv:
    print('--cd --no-daemon')
    sys.exit(0)
pathlib.Path({str(home / 'agent-pid')!r}).write_text(str(os.getpid()))
if '--no-daemon' in sys.argv:
    pathlib.Path({str(home / 'resumed.json')!r}).write_text(json.dumps(
        {{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'pid': os.getpid()}}))
    print('Resumed without daemon', flush=True)
else:
    print({('■ ' + error)!r}, flush=True)
time.sleep(60)
''')
    codex.chmod(0o755)
    (home / '.zshrc').write_text(f'''
export PATH={shlex.quote(str(bins))}:$PATH
source {shlex.quote(str(REPO_ROOT / 'zsh/resume.zsh'))}
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{ cat "$HOME/agent-pid"; }}
_dev_app_slot_reserved() {{ return 1; }}
_dev_agent_resume_cmd() {{ print -r -- "codex resume $2"; }}
''')
    daemon = subprocess.Popen(['sleep', '60'], env=env, cwd=home, start_new_session=True)
    try:
        assert tmux('new-session', '-d', '-s', 'dev-api-1', '-c', str(worktree),
                    'zsh', '-lic', 'exec codex resume ' + SID).returncode == 0
        socket = tmux('display-message', '-p', '-t', '=dev-api-1:', '#{socket_path}').stdout.strip()
        pane = mod.panes(socket)[0]
        assert tmux('new-session', '-d', '-s', 'dev-api-2', '-c', str(home),
                    'sh', '-c', "printf 'Other conversation connected\\n'; exec sleep 60").returncode == 0
        peer = next(p for p in mod.panes(socket) if p['session'] == 'dev-api-2')
        until(lambda: 'Other conversation connected' in tmux('capture-pane', '-p', '-t', peer['pane']).stdout)
        until(lambda: error in tmux('capture-pane', '-p', '-J', '-t', pane['pane']).stdout)
        target = until(lambda: mod.owner(socket, pane))
        assert target['sid'] == SID and target['cwd'] == str(worktree)
        old_pid = target['pid']
        token = 'b' * 32
        mod.write_json(mod.cache(socket) / (token + '.offer'),
                       {**target, 'error': error, 'created': time.time()})
        assert mod.respond(socket, token, True) == 0
        resumed = until(lambda: json.loads((home / 'resumed.json').read_text())
                        if (home / 'resumed.json').exists() else None)
        cd_args = ['--cd', str(worktree)] if error == mod.INVALID_CWD else []
        assert resumed['argv'] == ['resume', SID, *cd_args, '--no-daemon']
        assert resumed['cwd'] == str(worktree) and str(resumed['pid']) != old_pid
        assert mod.panes(socket)[0]['pane'] == pane['pane']
        assert daemon.poll() is None
        assert next(p for p in mod.panes(socket) if p['pane'] == peer['pane']) == peer
        assert 'Other conversation connected' in tmux('capture-pane', '-p', '-t', peer['pane']).stdout
        assert (worktree / 'dirty.txt').read_text() == 'uncommitted edits'
        saved = list((home / 'cache/t/restart').iterdir())
        assert len(saved) == 1 and error in saved[0].read_text()
        old_status = subprocess.run(['ps', '-p', old_pid, '-o', 'stat='],
                                    capture_output=True, text=True)
        assert not old_status.stdout.strip() or old_status.stdout.strip().startswith('Z')
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)
