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
        pending = repr(actions[index]) if index < len(actions) else 'process exit'
        diagnostic = (f'Completed {index}/{len(actions)} terminal actions; waiting for {pending}\n'
                      + output.decode(errors='replace'))
        assert process.poll() is not None, diagnostic
        assert index == len(actions), diagnostic
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
    command = [sys.executable, str(ROOT / 'bin/t')]
    ready = 'Welcome to t'
    if keys == b'\x03':
        # SIGINT can interrupt rendering as soon as the title becomes visible.
        # Signal at the input boundary, as the navigation cancellation tests do.
        harness = tmp_path / 'first_run.py'
        harness.write_text('''import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader('entry', sys.argv[1])
spec = importlib.util.spec_from_loader('entry', loader)
t = importlib.util.module_from_spec(spec)
loader.exec_module(t)
read_key = t._RailUI.read_key
def ready_read_key(self):
    self.out.write('<test-read-ready>')
    self.out.flush()
    return read_key(self)
t._RailUI.read_key = ready_read_key
raise SystemExit(t.main([]))
''')
        command = [sys.executable, str(harness), str(ROOT / 'bin/t')]
        ready = '<test-read-ready>'
    result, output = run_terminal(command, env, tmp_path, size, [(ready, keys)])
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


@pytest.mark.parametrize('size', [(80, 24), (32, 12)])
@pytest.mark.parametrize('login', [False, True])
def test_setup_logs_into_native_agent_outside_path(tmp_path, size, login):
    home, env = sandbox(tmp_path)
    # The native installer uses this location even before the user's next shell
    # has loaded its PATH changes. Exercise real probing and interactive login.
    env['PATH'] = '/usr/bin:/bin'
    agent = home / '.local/bin/claude'
    agent.parent.mkdir(parents=True)
    agent.write_text('''#!/bin/sh
case "$*" in
  --version) printf '1.0.0\\n' ;;
  'auth status') printf '{"loggedIn":false}\\n'; exit 1 ;;
  'auth login')
    printf 'Test sign-in: '
    read -r answer
    [ "$answer" = continue ] || exit 1
    printf '%s\\n' "$0" > "$HOME/login-executable"
    ;;
  *) exit 2 ;;
esac
''')
    agent.chmod(0o755)
    harness = tmp_path / 'entry.py'
    harness.write_text('''import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader('entry', sys.argv[1])
spec = importlib.util.spec_from_loader('entry', loader)
t = importlib.util.module_from_spec(spec)
loader.exec_module(t)
t.cmd_integrate = lambda *args: 0
helper = t._onboarding()
discovery = {
 'discover_local': lambda *args: [],
 'github_auth_state': lambda: 'authenticated', 'github_orgs': lambda: ([], []),
 'tailscale_hosts': lambda: ([], [])}
helper['run'].__globals__['_discovery'] = lambda api: discovery
t._onboarding = lambda: helper
raise SystemExit(t.main([]))
''')
    actions = [('Welcome to t', b'\r'), ('Choose agents', b'\r'),
               ('Select agent logins', b' \r' if login else b'\r'), ('Review setup', b'y')]
    if login:
        actions.append(('Test sign-in:', b'continue\r'))
    result, output = run_terminal([sys.executable, str(harness), str(ROOT / 'bin/t')],
                                  env, tmp_path, size, actions)
    assert result == 0, output
    assert 'Setup complete' in output
    if login:
        assert (home / 'login-executable').read_text().strip() == str(agent)
    else:
        assert not (home / 'login-executable').exists()
        assert 'Test sign-in:' not in output
    assert json.loads((home / '.config/t/onboarding.json').read_text())['version'] == 1


def navigation_harness(tmp_path, *, empty=False, agents=False, authenticated=()):
    """Real input/rendering with offline discovery and a captured apply boundary."""
    harness = tmp_path / 'navigation.py'
    harness.write_text('''import importlib.machinery, importlib.util, json, pathlib, sys
loader = importlib.machinery.SourceFileLoader('entry', sys.argv[1])
spec = importlib.util.spec_from_loader('entry', loader)
t = importlib.util.module_from_spec(spec)
loader.exec_module(t)
t._install_probe = lambda: {a: {
    'version': '1' if sys.argv[4] == 'agents' and a in t.AGENTS else '',
    'logged_in': a in sys.argv[5].split(',')} for a in t.INSTALL_AGENTS}
read_key = t._RailUI.read_key
def ready_read_key(self):
    self.test_read_number = getattr(self, 'test_read_number', 0) + 1
    self.out.write(f'<test-read-ready:{self.test_read_number}>')
    self.out.flush()
    return read_key(self)
t._RailUI.read_key = ready_read_key
helper = t._onboarding()
scope = helper['run'].__globals__
empty = sys.argv[3] == 'empty'
destination = pathlib.Path(sys.argv[2])
calls = []
def discover(name, value):
    calls.append(name)
    return value
discovery = {
 'discover_local': lambda *args: discover('local', [] if empty else [
     {'path': str(pathlib.Path.home() / 'code/existing'), 'identity': 'acme/existing'}]),
 'github_auth_state': lambda: 'authenticated',
 'github_orgs': lambda: discover('orgs', ([], []) if empty else (['acme'], [])),
 'github_repos': lambda org: discover('repos:' + org, ([
     {'full_name': 'acme/first'}, {'full_name': 'acme/second'}], [])),
 'clone_destination': lambda name: str(pathlib.Path.home() / 'code' / name.replace('/', '-')),
 'tailscale_hosts': lambda: discover('hosts', ([], []) if empty else ([
     {'alias': 'first-host', 'target': 'first.tail.example'},
     {'alias': 'second-host', 'target': 'second.tail.example'}], []))}
scope['_discovery'] = lambda api: discovery
review = scope['Dialog'].review
def guarded_review(self, lines):
    assert not destination.exists(), 'setup applied before final review'
    assert not list(pathlib.Path.home().iterdir()), 'navigation changed account settings before review'
    return review(self, lines)
scope['Dialog'].review = guarded_review
def apply(cfg, api, discovery, plan):
    destination.write_text(json.dumps({'plan': plan, 'discovery': calls}))
    return 0
scope['_apply'] = apply
t._onboarding = lambda: helper
raise SystemExit(t.main([]))
''')
    return [sys.executable, str(harness), str(ROOT / 'bin/t'),
            str(tmp_path / 'applied.json'), 'empty' if empty else 'full',
            'agents' if agents else 'none', ','.join(authenticated)]


@pytest.mark.parametrize('size', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('back_key', [b'q', b'\x1b'])
@pytest.mark.parametrize('accept', [False, True])
def test_hosts_back_preserves_pending_choices_until_review(tmp_path, size, back_key, accept):
    home, env = sandbox(tmp_path)
    command = navigation_harness(tmp_path)
    actions = [('Welcome to t', b'\r'), ('Choose agents', b'\r'),
               ('Register existing', b'\r'), ('Organizations', b' \r'),
               ('Clone repositories', b' \r'),
               # Back before Enter must still retain the newly marked host.
               ('Optional Tailscale', b' ' + back_key),
               ('Clone repositories', b'\r'), ('Optional Tailscale', b'\r'),
               ('Review setup', b'n'), ('Optional Tailscale', back_key),
               ('Clone repositories', b'\r'), ('Optional Tailscale', b'\r'),
               ('Review setup', b'y' if accept else b'q')]
    if accept:
        actions += [('Verify saved SSH hosts?', b'j\r')]
    result, output = run_terminal(command, env, tmp_path, size, actions)
    assert result == (0 if accept else 130), output
    applied = tmp_path / 'applied.json'
    if accept:
        captured = json.loads(applied.read_text())
        plan = captured['plan']
        assert plan['selected'] == []
        assert [r['path'] for r in plan['repos']] == [str(home / 'code/existing')]
        assert [r['full_name'] for r in plan['clones']] == ['acme/first']
        assert [h['target'] for h in plan['hosts']] == ['first.tail.example']
        assert sorted(captured['discovery']) == ['hosts', 'local', 'orgs', 'repos:acme']
        assert 'Setup complete' in output
    else:
        assert not applied.exists()
        assert 'Setup canceled' in output
        assert not list(home.iterdir()), 'navigation/cancel unexpectedly changed account settings'


@pytest.mark.parametrize('size', [(80, 24), (32, 12)])
@pytest.mark.parametrize('back_key', [b'n', b'\x1b'])
def test_review_back_skips_empty_steps_and_ctrl_c_cancels(tmp_path, size, back_key):
    home, env = sandbox(tmp_path)
    actions = [('Welcome to t', b'\r'), ('Choose agents', b'\r'),
               ('Review setup', back_key),
               # A title can arrive before the frame finishes rendering. Wait
               # for the next input read after Back before delivering SIGINT.
               ('<test-read-ready:4>', b'\x03')]
    result, output = run_terminal(navigation_harness(tmp_path, empty=True), env, tmp_path, size, actions)
    assert result == 130, output
    assert 'Setup canceled' in output
    assert not (tmp_path / 'applied.json').exists()
    assert not list(home.iterdir())


@pytest.mark.parametrize('size', [(80, 24), (32, 12)])
@pytest.mark.parametrize('keys,expected', [(b'\r', []), (b' \r', ['claude']),
                                          (b'j \r', ['codex']), (b'  \r', ['claude', 'codex'])])
@pytest.mark.parametrize('accept', [False, True])
def test_agent_login_selection_survives_review_back_and_can_be_skipped(tmp_path, size, keys, expected, accept):
    home, env = sandbox(tmp_path)
    actions = [('Welcome to t', b'\r'), ('Choose agents', b'\r'),
               ('Default agent', b'\r'), ('Select agent logins', keys),
               ('Review setup', b'n'), ('Select agent logins', b'\r'),
               ('Review setup', b'y' if accept else b'q')]
    result, output = run_terminal(navigation_harness(tmp_path, empty=True, agents=True),
                                  env, tmp_path, size, actions)
    assert result == (0 if accept else 130), output
    applied = tmp_path / 'applied.json'
    if accept:
        plan = json.loads(applied.read_text())['plan']
        assert plan['logins'] == expected
        assert [step['step'] for step in plan['steps']] == ['login:' + agent for agent in expected]
    else:
        assert not applied.exists()
        assert not list(home.iterdir())


@pytest.mark.parametrize('size', [(80, 24), (32, 12)])
@pytest.mark.parametrize('authenticated', [('codex',), ('claude', 'codex')])
@pytest.mark.parametrize('accept', [False, True])
def test_setup_shows_authenticated_codex_without_requesting_login(tmp_path, size, authenticated, accept):
    home, env = sandbox(tmp_path)
    actions = [('Welcome to t', b'\r'), ('codex · logged in', b'\r'),
               ('Default agent', b'\r')]
    if authenticated == ('codex',):
        actions.append(('ALREADY LOGGED IN: CODEX', b'\r'))
    actions.append(('Review setup', b'y' if accept else b'q'))
    command = navigation_harness(tmp_path, empty=True, agents=True, authenticated=authenticated)
    result, output = run_terminal(command, env, tmp_path, size, actions)
    assert result == (0 if accept else 130), output
    assert 'Already logged in:' in output
    assert ('Select agent logins' in output) == (authenticated == ('codex',))
    applied = tmp_path / 'applied.json'
    if accept:
        plan = json.loads(applied.read_text())['plan']
        assert plan['authenticated'] == list(authenticated)
        assert plan['logins'] == plan['steps'] == []
    else:
        assert not applied.exists()
        assert not list(home.iterdir())
