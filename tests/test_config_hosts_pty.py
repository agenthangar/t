"""Fresh Tailscale discovery and reviewed host edits in real, isolated terminals."""
import json
import sys

import pytest

from test_onboarding_pty import ROOT, run_terminal, sandbox


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
    assert local.read_text() == before, 'host saved before final review'
    if label == 'Add host':
        seen.append([key for key, _ in rows])
    return pick(self, label, rows, *args, **kwargs)
t._RailUI.pick = guarded_pick
result = t.main(['config', 'open'])
pathlib.Path(sys.argv[2]).write_text(json.dumps(seen))
raise SystemExit(result)
''')
    actions = [('Local settings', b'jjjjj\r'), ('Hosts', b'j\r'),
               ('Add host', b'j\r'), ('Add host', b'\r'),
               ('Hosts', b'jj\r'), ('Add host', b'q'), ('Hosts', b'q'),
               ('Local settings', b's'), ('Review local settings', b'y' if save else b'q')]
    if not save:
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
