"""Exercise real menu input and Cursor recovery on disposable tmux servers."""

import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import sys
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
def test_real_menu_dismiss_or_accept_without_typing_into_agent(terminal, choice):
    mod, tmux, command, env, home = terminal
    (home / ".zshrc").write_text(f'''
_dev_agent_of_session() {{ print codex; }}
_dev_session_sid() {{ print {SID}; }}
_dev_session_claude_pid() {{ tmux display-message -p -t '=dev-api-1:' '#{{pane_pid}}'; }}
_t_restart_slot() {{ print -r -- "$*" > "$HOME/accepted"; print 'Saved visible draft'; }}
''')
    launch = "printf 'Server connection could not be restored\\n'; exec sleep 60"
    assert tmux("new-session", "-d", "-s", "dev-api-1", "-c", str(home), "zsh", "-lc", launch).returncode == 0
    socket = tmux("display-message", "-p", "-t", "=dev-api-1:", "#{socket_path}").stdout.strip()
    pane = mod.panes(socket)[0]
    until(lambda: "Server connection" in tmux("capture-pane", "-p", "-t", pane["pane"]).stdout)
    target = mod.owner(socket, pane)
    assert target["sid"] == SID
    master, slave = pty.openpty()
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
                return b"Save visible draft" in visible
            until(menu_visible)
            assert b"Save visible draft" not in os.read(other_master, 65536)
            os.write(master, choice.encode())
        offers = lambda: list(mod.cache(socket).glob("*.offer"))
        until(lambda: not offers())
        if choice == "r":
            until(lambda: (home / "accepted").exists(), detail=lambda: os.read(master, 65536).decode(errors="replace"))
            assert (home / "accepted").read_text().split() == ["dev-api-1", str(home), SID, "codex", "restart", target["pid"]]
        else:
            assert not (home / "accepted").exists()
        # Menu keystrokes never reach the client process, which remains untouched.
        assert tmux("display-message", "-p", "-t", "=dev-api-1:", "#{pane_pid}").stdout.strip() == pane["pane_pid"]
    finally:
        client.terminate()
        client.wait(timeout=5)
        os.close(master)
        other_client.terminate()
        other_client.wait(timeout=5)
        os.close(other_master)


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
