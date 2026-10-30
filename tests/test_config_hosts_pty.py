"""Fresh Tailscale discovery and reviewed host edits in real, isolated terminals."""
import json
import sys

import pytest

from test_onboarding_pty import ROOT, run_terminal, sandbox


@pytest.mark.parametrize('mode', ['verify', 'refuse', 'offline', 'interrupt', 'later'])
def test_saved_host_verification_uses_native_cooked_terminal(tmp_path, mode):
    home, env = sandbox(tmp_path)
    local = home / 'local.zsh'
    local.write_text('# settings\n')
    env.update(T_LOCAL_RC=str(local), T_SETUP_SHIM='1', VERIFY_MODE=mode,
               VERIFY_LOG=str(tmp_path / 'ssh.jsonl'))
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    env['PATH'] = str(fake_bin) + ':' + env.get('PATH', '/usr/bin:/bin')
    ssh = fake_bin / 'ssh'
    ssh.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys, termios
local = pathlib.Path(os.environ['T_LOCAL_RC'])
assert 'REMOTE_HOSTS[first]=first.tail.test' in local.read_text(), 'SSH ran before save'
assert sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
attrs = termios.tcgetattr(0)
assert attrs[3] & termios.ECHO and attrs[3] & termios.ICANON, 'SSH inherited raw UI'
assert sys.argv[1:] == ['-T', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=no',
                        '-o', 'StrictHostKeyChecking=ask', '--', sys.argv[-2], 'true']
with open(os.environ['VERIFY_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[-2]) + '\\n')
if sys.argv[-2] == 'first.tail.test':
    mode = os.environ['VERIFY_MODE']
    if mode == 'offline':
        print('ssh: connection timed out', file=sys.stderr)
        sys.exit(255)
    if mode == 'interrupt':
        print('Native SSH verification waiting', flush=True)
        input()
    else:
        answer = input('Native SSH fingerprint SHA256:test. Trust this host? ')
        sys.exit(0 if answer == 'yes' else 255)
''')
    ssh.chmod(0o755)
    harness = tmp_path / 'verify.py'
    harness.write_text('''import copy, importlib.machinery, importlib.util, os, sys
loader = importlib.machinery.SourceFileLoader('entry', sys.argv[1])
spec = importlib.util.spec_from_loader('entry', loader)
t = importlib.util.module_from_spec(spec)
loader.exec_module(t)
t.CONFIG = os.environ['T_LOCAL_RC']
t.zsh_capture = lambda *args: ''
def wizard(cfg):
    edited = copy.deepcopy(cfg)
    edited.hosts = {'first': 'first.tail.test', 'second': 'second.tail.test'}
    return edited
t._config_wizard = wizard
raise SystemExit(t.main(['config', 'open']))
''')
    actions = [('Verify saved SSH hosts?', b'j\r' if mode == 'later' else b'\r')]
    if mode in ('verify', 'refuse'):
        actions += [('Trust this host?', b'yes\n' if mode == 'verify' else b'no\n')]
    elif mode == 'interrupt':
        actions += [('Native SSH verification waiting', b'\x03')]
    result, output = run_terminal([sys.executable, str(harness), str(ROOT / 'bin/t')],
                                  env, tmp_path, (44, 20), actions)
    assert result == (130 if mode == 'interrupt' else 0), output
    assert 'REMOTE_HOSTS[first]=first.tail.test' in local.read_text()
    log = tmp_path / 'ssh.jsonl'
    targets = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert targets == ([] if mode == 'later' else ['first.tail.test'] if mode == 'interrupt' else
                       ['first.tail.test', 'second.tail.test'])
    if mode in ('refuse', 'offline'):
        assert 'Registration is saved' in output
    if mode == 'interrupt':
        assert 'Settings saved; SSH verification canceled' in output
    assert not (home / '.ssh/known_hosts').exists()


@pytest.mark.parametrize('size', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('save', [False, True])
def test_config_add_host_refreshes_and_only_writes_after_review(tmp_path, size, save):
    home, env = sandbox(tmp_path)
    local = home / 'local.zsh'
    before = '# keep existing settings\nREMOTE_HOSTS[existing]=user@100.64.0.1\n'
    local.write_text(before)
    env['T_LOCAL_RC'] = str(local)
    env['T_SETUP_SHIM'] = '1'
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    env['PATH'] = str(fake_bin) + ':' + env.get('PATH', '/usr/bin:/bin')
    env['HOST_DISCOVERY_COUNT'] = str(tmp_path / 'calls')
    tailscale = fake_bin / 'tailscale'
    tailscale.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
assert sys.argv[1:] == ['status', '--json']
counter = pathlib.Path(os.environ['HOST_DISCOVERY_COUNT'])
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
peers = {'old': {'ID': 'old', 'Online': True, 'OS': 'linux', 'HostName': 'existing',
                 'DNSName': 'existing.tail.example.', 'TailscaleIPs': ['100.64.0.1']}}
if count > 1:
    peers['new'] = {'ID': 'new', 'Online': True, 'OS': 'linux', 'HostName': 'build',
                    'DNSName': 'build.tail.example.', 'TailscaleIPs': ['100.64.0.2']}
print(json.dumps({'BackendState': 'Running', 'Self': {'ID': 'self'}, 'Peer': peers}))
''')
    tailscale.chmod(0o755)
    harness = tmp_path / 'config.py'
    harness.write_text('''import importlib.machinery, importlib.util, json, os, pathlib, sys
loader = importlib.machinery.SourceFileLoader('entry', sys.argv[1])
spec = importlib.util.spec_from_loader('entry', loader)
t = importlib.util.module_from_spec(spec)
loader.exec_module(t)
local = pathlib.Path(os.environ['T_LOCAL_RC'])
t.CONFIG = str(local)
t.zsh_capture = lambda *args: ''
before = local.read_text()
seen = []
pick = t._RailUI.pick
def guarded_pick(self, label, rows, *args, **kwargs):
    if label != 'Verify saved SSH hosts?':
        assert local.read_text() == before, 'host saved before final review'
    if label == 'Add host':
        seen.append([key for key, _ in rows])
    return pick(self, label, rows, *args, **kwargs)
t._RailUI.pick = guarded_pick
result = t.main(['config', 'open'])
pathlib.Path(sys.argv[2]).write_text(json.dumps(seen))
raise SystemExit(result)
''')
    # Hosts follows session, agent/workflow and repository settings in the hub.
    actions = [('Local settings', b'j' * 10 + b'\r'), ('Hosts', b'j\r'),
               ('Add host', b'j\r'), ('Add host', b'\r'),
               ('Hosts', b'jj\r'), ('Add host', b'q'), ('Hosts', b'q'),
               ('Local settings', b's'), ('Review local settings', b'y' if save else b'q')]
    if save:
        actions += [('Verify saved SSH hosts?', b'j\r')]
    else:
        actions += [('Local settings', b'q'), ('unsaved change', b'jj\r')]
    trace = tmp_path / 'seen.json'
    result, output = run_terminal([sys.executable, str(harness), str(ROOT / 'bin/t'), str(trace)],
                                  env, tmp_path, size, actions)
    assert result == (0 if save else 130), output
    assert (tmp_path / 'calls').read_text() == '3'
    options = ['__manual__', '__refresh__', '__back__']
    assert json.loads(trace.read_text()) == [options, ['tailscale:build.tail.example', *options], options]
    if save:
        assert 'REMOTE_HOSTS[build]=build.tail.example' in local.read_text()
        assert local.read_text().startswith(before)
    else:
        assert local.read_text() == before
