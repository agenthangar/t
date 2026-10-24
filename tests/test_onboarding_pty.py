"""Real terminal entry, review, installation and cancellation in disposable homes."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import shutil
import struct
import subprocess
import sys
import termios
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def sandbox(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('T_', 'COV_CORE_', 'GIT_', 'XDG_', 'CODEX_', 'CLAUDE'))}
    env.update(HOME=str(home), ZDOTDIR=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_DATA_HOME=str(home / '.local/share'), XDG_CACHE_HOME=str(home / '.cache'),
               XDG_STATE_HOME=str(home / '.local/state'), TMUX='', TMUX_TMPDIR=str(tmp_path / 'tmux'),
               PYTHONDONTWRITEBYTECODE='1', TERM='xterm-256color',
               T_NO_MCP='1', T_NO_CODEX_HOOKS='1', T_NO_PERMISSIONS='1', T_NO_TRUST='1',
               T_NO_UPDATE_CHECK='1', T_RECOVERY_DISABLE='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_NOSYSTEM='1')
    Path(env['TMUX_TMPDIR']).mkdir()
    return home, env


def run_terminal(command, env, cwd, size, actions):
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', size[1], size[0], 0, 0))
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                               cwd=cwd, env=env, start_new_session=True)
    output, index, searched_from = b'', 0, 0
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                output += os.read(master, 65536)
            if index < len(actions):
                needle, keys = actions[index]
                if needle.encode() in output[searched_from:]:
                    searched_from = len(output)
                    if keys == b"\x03":
                        process.send_signal(signal.SIGINT)
                    else:
                        os.write(master, keys)
                    index += 1
            if process.poll() is not None:
                while select.select([master], [], [], 0)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                break
        assert process.poll() is not None, output.decode(errors='replace')
        assert index == len(actions), output.decode(errors='replace')
        after = termios.tcgetattr(slave)
        flags = termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
        assert after[3] & flags == before[3] & flags, 'terminal mode was not restored'
        return process.returncode, output.decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize('size', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('keys,code', [(b'q', 130), (b'\x03', 130), (b'j\r', 0)])
def test_bare_first_run_can_cancel_or_skip_at_terminal_sizes(tmp_path, size, keys, code):
    home, env = sandbox(tmp_path)
    result, output = run_terminal([sys.executable, str(ROOT / 'bin/t')], env, tmp_path,
                                  size, [('Welcome to t', keys)])
    assert result == code, output
    assert 'CONTINUE' in output
    assert not list(home.iterdir()), 'skip/cancel modified the fresh home'


@pytest.mark.parametrize('accept', [False, True])
def test_real_setup_review_is_required_before_install_and_registration(tmp_path, accept):
    home, env = sandbox(tmp_path)
    package = tmp_path / 'package'
    shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.coverage*'))
    (package / '.t-release-version').write_text('v0.6.2\n')
    checkout = home / 'code/project'
    checkout.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True, env=env)
    harness = tmp_path / 'entry.py'
    # Use the real installer and registration path, with remote discovery and
    # agent probes replaced so the test cannot contact services or install tools.
    harness.write_text('''import importlib.machinery, importlib.util, pathlib, sys
loader = importlib.machinery.SourceFileLoader('entry', sys.argv[1])
spec = importlib.util.spec_from_loader('entry', loader)
t = importlib.util.module_from_spec(spec)
loader.exec_module(t)
t._install_probe = lambda: {a: {'version': '', 'logged_in': False} for a in t.INSTALL_AGENTS}
helper = t._onboarding()
discovery = {
 'discover_local': lambda *args: [{'path': sys.argv[2], 'identity': 'acme/project'}],
 'github_auth_state': lambda: 'authenticated', 'github_orgs': lambda: ([], []),
 'tailscale_hosts': lambda: ([], [])}
helper['run'].__globals__['_discovery'] = lambda api: discovery
t._onboarding = lambda: helper
raise SystemExit(t.main([]))
''')
    actions = [('Welcome to t', b'\r'), ('Choose agents', b'\r'),
               ('Register existing', b'\r'), ('Review setup', b'y' if accept else b'q')]
    result, output = run_terminal([sys.executable, str(harness), str(package / 'bin/t'), str(checkout)],
                                  env, tmp_path, (44, 20), actions)
    if accept:
        assert result == 0, output
        assert 'Setup complete' in output
        assert (home / 'bin/t').resolve() == package / 'bin/t'
        assert 't shell integration' in (home / '.zshrc').read_text()
        local = (home / '.config/t/local.zsh').read_text()
        assert f'DEV_REPOS[project]={checkout}' in local
        assert json.loads((home / '.config/t/onboarding.json').read_text())['version'] == 1
        # Every explicit command remains usable without loading the parent shell.
        listing = subprocess.run([sys.executable, str(package / 'bin/t'), 'repo', 'list'],
                                 capture_output=True, text=True, env=env, check=True)
        assert str(checkout) in listing.stdout or '~/code/project' in listing.stdout
    else:
        assert result == 130, output
        assert sorted(p.name for p in home.iterdir()) == ['code']
