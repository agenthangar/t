"""MCP inspection stays local and session policy never copies credentials."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from types import SimpleNamespace

import pytest

from conftest import load_script


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.setenv('CURSOR_CONFIG_DIR', str(home / '.cursor'))
    monkeypatch.setenv('XDG_STATE_HOME', str(home / '.local/state'))
    api = load_script(Path(__file__).parents[1] / 'libexec/t_mcp_policy.py', 'mcp_policy_test')
    monkeypatch.setattr(api.shutil, 'which', lambda name: str(api.ROOT / 'bin/t') if name == 't' else None)
    monkeypatch.setattr(api, 'CODEX_SYSTEM_PATHS', (tmp_path / 'etc/config.toml', tmp_path / 'etc/requirements.toml'))
    return api


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    return path


def sample(api, **values):
    return api._row('codex', 'docs', 'user config', True, **values)


def test_inventory_empty_and_safe_config_reads(mcp, tmp_path):
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / '.git').mkdir()
    assert mcp.inventory(cwd=str(workspace)) == []
    path = write(mcp._home('codex') / 'config.toml',
        '[mcp_servers.docs]\ncommand="server"\n[mcp_servers.docs.env]\nSECRET="do-not-print"\n')
    write(Path.home() / '.claude.json', {'mcpServers': {'sessions': {'command': 't', 'args': ['mcp']}},
                                      'disabledMcpServers': ['sessions']})
    write(mcp._home('cursor') / 'mcp.json', {'mcpServers': {'browser': {'url': 'https://secret-token.example'}}})
    rows = mcp.inventory(cwd=str(workspace))
    assert {(row['client'], row['name']) for row in rows} == {('codex', 'docs'), ('claude', 'sessions'), ('cursor', 'browser')}
    assert all(row['observed_status'] == 'Not measured' and row['startup_seconds'] is None for row in rows)
    assert next(row for row in rows if row['client'] == 'claude')['sessions']
    assert next(row for row in rows if row['client'] == 'claude')['enabled'] is False
    assert next(row for row in rows if row['client'] == 'codex')['source'] == str(path)
    assert 'secret' not in json.dumps(rows).lower() and 'do-not-print' not in json.dumps(rows)
    with pytest.raises(ValueError, match='Unsupported'):
        mcp.inventory('unknown')
    assert mcp.dependency_paths('unknown') == []


def test_native_codex_listing_is_configuration_only(mcp, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp.shutil, 'which', lambda _: '/fake/codex')
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {'name': 'sessions', 'enabled': True, 'transport': {'type': 'stdio', 'command': str(mcp.ROOT / 'bin/t'), 'args': ['mcp'], 'env': {'TOKEN': 'private'}}},
            {'name': 'docs', 'enabled': True, 'disabled_reason': 'managed policy', 'transport': {'url': 'https://private-token.example'}},
            {'name': 'other', 'transport': []}]))
    monkeypatch.setattr(mcp.subprocess, 'run', run)
    rows = mcp.inventory('codex', str(tmp_path))
    assert len(calls) == 1 and calls[0][0] == ['/fake/codex', 'mcp', 'list', '--json']
    assert calls[0][1]['stdin'] == subprocess.DEVNULL
    assert rows[0]['enabled'] is False and rows[1]['enabled'] is None
    assert rows[2]['sessions'] and rows[2]['enabled'] is True
    assert 'private' not in json.dumps(rows)


def test_claude_custom_store(mcp, monkeypatch, tmp_path):
    custom = tmp_path / 'custom-claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(custom))
    source = write(custom / '.claude.json', {'mcpServers': {'custom': {'command': 'server'}}})
    write(Path.home() / '.claude.json', {'mcpServers': {'ignored': {'command': 'server'}}})
    row, = mcp.inventory('claude')
    assert row['name'] == 'custom' and row['source'] == str(source)
    assert source in mcp.dependency_paths('claude')


def test_custom_sessions_name_never_claims_t_session_tools(mcp):
    write(mcp._home('codex') / 'config.toml', '[mcp_servers.sessions]\ncommand="/custom/server"\nargs=["mcp"]\n')
    write(Path.home() / '.claude.json', {'mcpServers': {'sessions': {'command': '/custom/server', 'args': ['mcp']}}})
    assert not any(row['sessions'] for row in mcp.inventory())
    assert not mcp._session_server({'command': 'not-installed', 'args': ['mcp']})
    assert not mcp._session_server({'command': str(mcp.ROOT / 'bin/t'), 'args': ['mcp'], 'url': 'https://example.invalid'})
    assert not mcp._session_server({'command': str(mcp.ROOT / 'bin/t'), 'args': ['other']})


@pytest.mark.parametrize('response,rc', [('{}', 0), ('[{}]', 0), ('bad', 0), ('[]', 1)])
def test_bad_native_inventory_is_not_empty_success(mcp, monkeypatch, response, rc):
    monkeypatch.setattr(mcp.shutil, 'which', lambda _: '/fake/codex')
    monkeypatch.setattr(mcp.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=rc, stdout=response))
    with pytest.raises(ValueError):
        mcp.inventory('codex')


def test_project_settings_require_native_trust(mcp, tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    project = write(repo / '.codex/config.toml', '[mcp_servers.project]\ncommand="local"\n')
    user = mcp._home('codex') / 'config.toml'
    write(user, '')
    assert mcp.inventory('codex', str(repo)) == []
    write(user, '[projects.' + json.dumps(str(repo)) + ']\ntrust_level="trusted"\n')
    assert mcp.inventory('codex', str(repo))[0]['source'] == str(project)
    child = repo / 'src'
    child.mkdir()
    assert project in mcp.dependency_paths('codex', child)
    write(repo / '.mcp.json', {'mcpServers': {'project': {'command': 'project'}}})
    write(Path.home() / '.claude.json', {'projects': {str(repo): {
        'mcpServers': {'local': {'command': 'local'}}, 'disabledMcpServers': ['project']}}})
    rows = mcp.inventory('claude', child)
    assert {row['name']: row['enabled'] for row in rows} == {'project': False, 'local': True}


def test_plugin_inventory_versions_overrides_and_dependencies(mcp):
    user = write(mcp._home('codex') / 'config.toml',
        '[plugins."demo@market"]\nenabled=true\n[plugins."demo@market".mcp_servers.docs]\nenabled=false\n'
        '[plugins."missing@market".mcp_servers.remote]\nenabled=true\n')
    cache = mcp._home('codex') / 'plugins/cache/market/demo'
    for version, name in [('0.9', 'old'), ('0.10', 'docs')]:
        root = cache / version
        write(root / '.codex-plugin/plugin.json', {'name': 'demo', 'mcpServers': '.mcp.json'})
        write(root / '.mcp.json', {'mcpServers': {name: {'url': 'https://do-not-display'}}})
    rows = mcp.inventory('codex')
    assert [(row['plugin'], row['name'], row['enabled']) for row in rows] == [
        ('demo@market', 'docs', False), ('missing@market', 'remote', True)]
    before = mcp.configuration_fingerprint('codex')
    write(cache / '0.10/.mcp.json', {'mcpServers': {'docs': {'command': 'changed'}}})
    assert mcp.configuration_fingerprint('codex') != before
    assert user in mcp.dependency_paths('codex')
    assert cache / '0.10/.mcp.json' in mcp.dependency_paths('codex')
    write(user, '[plugins."demo@market"]\nenabled=false\n')
    assert mcp.inventory('codex')[0]['enabled'] is False


def test_plugin_embedded_servers_and_safe_manifest_paths(mcp, tmp_path):
    root = mcp._home('codex') / 'plugins/cache/market/demo/1.0'
    manifest = write(root / '.codex-plugin/plugin.json', {'name': 'demo', 'mcpServers': {'embedded': {'command': 'server'}}})
    row, = mcp.inventory('codex')
    assert row['plugin'] == 'demo@market' and row['enabled'] is None
    write(manifest, {'name': 'demo', 'mcpServers': str(tmp_path / 'outside.json')})
    assert tmp_path / 'outside.json' not in mcp._plugin_files(manifest)
    write(manifest, {'name': 'demo', 'mcpServers': '../../../../outside.json'})
    assert all(path.is_relative_to(root) for path in mcp._plugin_files(manifest))


@pytest.mark.parametrize('value', [[], {'unknown': {}}, {'servers': []}, {'servers': {'name': 'yes'}},
    {'servers': {'': True}}, {'servers': {'bad\nname': True}}, {'servers': {'bad\x7fname': False}}, {'plugins': []},
    {'plugins': {'': {}}}, {'plugins': {'plugin': {'name': 1}}}, {'servers': {str(i): True for i in range(257)}}])
def test_policy_validation(mcp, value):
    with pytest.raises(ValueError):
        mcp.validate(value)


def test_policy_inheritance_and_native_overrides(mcp):
    write(mcp._home('codex') / 'config.toml', '[mcp_servers.docs]\ncommand="server"\n')
    policy = {'enabled': True}
    direct, plugin = sample(mcp), sample(mcp, plugin='demo@market')
    assert mcp.summary(policy) == 'Inherit client settings' and mcp.launch_args(policy, 'codex') == []
    changed = mcp.update(policy, direct, False)
    changed = mcp.update(changed, plugin, True)
    assert mcp.choice(changed, direct) is False and mcp.choice(changed, plugin) is True
    assert mcp.summary(changed) == '1 enabled · 1 disabled'
    assert mcp.launch_args(changed, 'codex') == ['-c', 'mcp_servers."docs".enabled=false',
        '-c', 'plugins."demo@market".mcp_servers."docs".enabled=true']
    assert mcp.launch_args({**changed, 'enabled': False}, 'codex') == []
    with pytest.raises(ValueError, match='not supported'):
        mcp.launch_args(changed, 'claude')
    assert mcp.update(mcp.update(changed, direct, None), plugin, None) == policy
    assert policy == {'enabled': True}
    with pytest.raises(ValueError, match='inherit'):
        mcp.update(policy, direct, 'enabled')


def test_absent_direct_server_is_not_created_without_transport(mcp, monkeypatch):
    policy = {'enabled': True, 'mcp': {'servers': {'missing': False}}}
    assert mcp.launch_args(policy, 'codex') == []
    assert not mcp.cacheable(policy, 'codex')
    policy['mcp']['servers']['missing'] = True
    with pytest.raises(ValueError, match='not configured'):
        mcp.launch_args(policy, 'codex')
    monkeypatch.setattr(mcp, '_native_entries', lambda *a: [{'name': 'missing', 'enabled': True}])
    assert mcp.launch_args(policy, 'codex') == ['-c', 'mcp_servers."missing".enabled=true']


def test_system_server_and_requirements_invalidation(mcp):
    write(mcp.CODEX_SYSTEM_PATHS[0], '[mcp_servers.system]\ncommand="server"\n')
    policy = {'enabled': True, 'mcp': {'servers': {'system': False}}}
    assert mcp.launch_args(policy, 'codex') == ['-c', 'mcp_servers."system".enabled=false']
    assert mcp.cacheable(policy, 'codex')
    before = mcp.configuration_fingerprint('codex')
    write(mcp.CODEX_SYSTEM_PATHS[1], '[mcp_servers.system]\nidentity="managed"\n')
    assert mcp.configuration_fingerprint('codex') != before


@pytest.mark.parametrize('error', [OSError('unavailable'), subprocess.TimeoutExpired('codex', 10)])
def test_native_configuration_errors(mcp, monkeypatch, error):
    monkeypatch.setattr(mcp.shutil, 'which', lambda _: '/fake/codex')
    def fail(*a, **k):
        raise error
    monkeypatch.setattr(mcp.subprocess, 'run', fail)
    with pytest.raises(ValueError, match='unavailable'):
        mcp.inventory('codex')


def test_emitted_absent_disabled_policy_parses_with_native_codex(mcp):
    binary = next((Path(directory) / 'codex' for directory in os.get_exec_path()
                   if (Path(directory) / 'codex').is_file() and os.access(Path(directory) / 'codex', os.X_OK)), None)
    if binary is None:
        pytest.skip('Native Codex is not installed')
    version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=10)
    if not version.stdout.startswith('codex-cli '):
        pytest.skip('Native Codex is not installed')
    write(mcp._home('codex') / 'config.toml', '')
    args = mcp.launch_args({'enabled': True, 'mcp': {'servers': {'missing': False}}}, 'codex')
    result = subprocess.run([str(binary), *args, 'mcp', 'list', '--json'],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        pytest.fail('Native Codex rejected the emitted local MCP configuration')
    assert not any(row['name'] == 'missing' for row in json.loads(result.stdout))


def test_local_only_preserves_choices_without_querying_or_overrides(t_mod, mcp, monkeypatch):
    from test_config import Menu
    policy = {'enabled': True, 'inference': {'mode': 'local-only'}, 'mcp': {'servers': {'missing': True}}}
    def unexpected(*a):
        pytest.fail('Local-only policy must not inspect native MCP configuration')
    monkeypatch.setattr(mcp, '_codex_configuration', unexpected)
    assert mcp.launch_args(policy, 'codex') == [] and mcp.cacheable(policy, 'codex')
    assert mcp.summary(policy) == 'Disabled · local-only runtime'
    monkeypatch.setattr(t_mod, '_mcp_policy', lambda: vars(mcp))
    assert t_mod._workflow_mcp_menu(Menu([], pages=['enter']), policy, 'codex') == policy


@pytest.mark.parametrize('kind', ['symlink', 'large', 'json-list', 'bad-json', 'bad-toml', 'bad-mapping', 'bad-server', 'bad-project', 'bad-disabled'])
def test_configuration_failures_are_visible(mcp, tmp_path, kind):
    path = Path.home() / '.claude.json'
    if kind == 'symlink':
        other = write(tmp_path / 'other', '{}')
        path.symlink_to(other)
    elif kind == 'large':
        write(path, ' ' * (mcp.MAX_BYTES + 1))
    elif kind == 'json-list':
        write(path, [])
    elif kind == 'bad-json':
        write(path, 'not JSON')
    elif kind == 'bad-toml':
        write(mcp._home('codex') / 'config.toml', 'invalid = [')
    elif kind == 'bad-mapping':
        write(path, {'mcpServers': []})
    elif kind == 'bad-server':
        write(path, {'mcpServers': {'name': 1}})
    elif kind == 'bad-disabled':
        write(path, {'disabledMcpServers': None})
    else:
        write(path, {'projects': {str(mcp._root(None)): []}})
    with pytest.raises(ValueError):
        mcp.inventory()


def event(kind, **values):
    return json.dumps({'type': 'event_msg', 'timestamp': values.pop('timestamp'), 'payload': {'type': kind, **values}}) + '\n'


def test_explicit_observations_and_config_invalidation(mcp, tmp_path, monkeypatch):
    write(mcp._home('codex') / 'config.toml', '[mcp_servers.docs]\ncommand="server"\n')
    monkeypatch.setattr(mcp, '_latest_configuration_change', lambda *a: 0)
    path = write(tmp_path / 'rollout.jsonl', json.dumps({'type': 'session_meta', 'payload': {'id': 'session', 'cwd': str(tmp_path)}}) + '\n' +
        'invalid\n[]\n{"payload":[]}\n' + event('ignored', timestamp=1) +
        event('mcp_startup_update', server='docs', status='starting', timestamp=100) +
        event('mcp_startup_update', server='docs', status='ready', timestamp=100.25))
    assert mcp.collect_observations('codex', str(tmp_path), 'session', str(path)) == 1
    row, = mcp.inventory('codex', str(tmp_path))
    assert row['observed_status'] == 'Ready' and row['startup_seconds'] == .25
    assert row['provenance']['path'] == str(path) and row['observed_at'] == 100.25
    assert stat.S_IMODE(mcp.observations_path().stat().st_mode) == 0o600
    write(mcp._home('codex') / 'config.toml', '[mcp_servers.docs]\ncommand="changed"\n')
    assert mcp.inventory('codex', str(tmp_path))[0]['observed_status'] == 'Not measured'
    monkeypatch.setattr(mcp, '_latest_configuration_change', lambda *a: 200)
    assert mcp.collect_observations('codex', str(tmp_path), 'session', str(path)) == 0


def test_observations_require_identity_timestamps_and_explicit_status(mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, '_latest_configuration_change', lambda *a: 0)
    assert mcp.collect_observations('claude', str(tmp_path), 'session') == 0
    assert mcp.collect_observations('codex', str(tmp_path), '') == 0
    path = write(tmp_path / 'native.jsonl', event('mcp_startup_update', server='docs', status='ready', timestamp=1))
    assert mcp.collect_observations('codex', str(tmp_path), 'session', str(path)) == 0
    write(path, json.dumps({'type': 'session_meta', 'payload': {'id': 'other'}}) + '\n' +
        event('mcp_startup_update', server='docs', status='ready', timestamp=1))
    assert mcp.collect_observations('codex', str(tmp_path), 'session', str(path)) == 0
    write(path, json.dumps({'method': 'mcpServer/startupStatus/updated', 'timestamp': '2026-11-12T00:00:00Z',
        'params': {'threadId': 'session', 'cwd': str(tmp_path), 'server': 'docs', 'status': {'type': 'failed', 'error': 'private-token'}}}) + '\n')
    assert mcp.collect_observations('codex', str(tmp_path), 'session', log_path=str(path)) == 1
    stored = mcp.observations_path().read_text()
    assert 'Failed' in stored and 'private-token' not in stored and '"startup_seconds": null' in stored
    assert mcp._timestamp('bad') is None and mcp._timestamp(float('inf')) is None and mcp._timestamp(1e300) is None
    assert mcp._timestamp('2026-11-12T00:00:00Z') is not None
    assert mcp.collect_observations('codex', str(tmp_path), 'session', str(tmp_path / 'missing')) == 0
    link = tmp_path / 'link'; link.symlink_to(path)
    assert mcp.collect_observations('codex', str(tmp_path), 'session', str(link)) == 0


def test_observation_scope_order_and_equivalent_worktrees(mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, '_latest_configuration_change', lambda *a: 0)
    main = tmp_path / 'repo'
    write(main / '.git/config', '')
    trees = [tmp_path / 'one', tmp_path / 'two']
    for tree in trees:
        meta = main / '.git/worktrees' / tree.name
        write(meta / 'commondir', '../..')
        write(tree / '.git', 'gitdir: ' + str(meta))
    write(mcp._home('codex') / 'config.toml', '[mcp_servers.docs]\ncommand="server"\n')
    assert mcp.configuration_fingerprint('codex', trees[0]) == mcp.configuration_fingerprint('codex', trees[1])
    transcript = write(tmp_path / 'rollout.jsonl', json.dumps({'type': 'session_meta',
        'payload': {'id': 'session', 'cwd': str(trees[0])}}) + '\n' +
        event('mcp_startup_update', server='docs', status='starting', timestamp=100) +
        event('mcp_startup_update', server='docs', status='ready', timestamp=101))
    older = write(tmp_path / 'older.jsonl', json.dumps({'method': 'mcpServer/startupStatus/updated', 'timestamp': 99,
        'params': {'threadId': 'session', 'cwd': str(trees[0]), 'server': 'docs', 'status': 'failed'}}) + '\n')
    assert mcp.collect_observations('codex', trees[0], 'session', str(transcript), str(older)) == 1
    assert mcp.inventory('codex', trees[1])[0]['observed_status'] == 'Ready'
    assert mcp.inventory('codex', trees[1])[0]['observed_workspace'] == str(trees[0])
    assert mcp.collect_observations('codex', trees[1], 'session', str(transcript)) == 0
    for tree in trees:
        write(tree / '.codex/config.toml', '[mcp_servers.project]\ncommand="same"\n')
    assert mcp.configuration_fingerprint('codex', trees[0]) == mcp.configuration_fingerprint('codex', trees[1])
    write(trees[1] / '.codex/config.toml', '[mcp_servers.project]\ncommand="changed"\n')
    assert mcp.configuration_fingerprint('codex', trees[0]) != mcp.configuration_fingerprint('codex', trees[1])


def test_corrupt_observation_cache_is_not_configuration_failure(mcp, tmp_path):
    write(mcp._home('codex') / 'config.toml', '[mcp_servers.docs]\ncommand="server"\n')
    write(mcp.observations_path(), 'bad')
    assert mcp.inventory('codex')[0]['observed_status'] == 'Not measured'
    write(mcp.observations_path(), {'servers': {'codex:' + sample(mcp)['id']: []}})
    assert mcp.inventory('codex')[0]['observed_status'] == 'Not measured'


def test_inventory_command_contract(t_mod, mcp, monkeypatch, capsys):
    rows = [sample(mcp)]
    monkeypatch.setattr(t_mod, '_mcp_policy', lambda: {'inventory': lambda: rows})
    for words in (['mcp', 'list'], ['mcp', 'list', '--json']):
        assert t_mod._canonical_error(words) is None
        assert t_mod._command_readonly(words, terminal=True)
        assert t_mod._command_readonly(words, terminal=False)
        assert t_mod.cmd_mcp(None, t_mod.build_parser().parse_args(words)) == 0
        output = capsys.readouterr().out
        assert ('"servers"' in output if '--json' in words else
                all(heading in output for heading in ('CLIENT', 'SERVER', 'CONFIGURED', 'LAST OBSERVED', 'STARTUP')))
    rows.clear()
    assert t_mod.cmd_mcp(None, t_mod.build_parser().parse_args(['mcp', 'list'])) == 0
    assert 'No MCP servers configured' in capsys.readouterr().out
    def fail():
        raise ValueError('invalid configuration')
    monkeypatch.setattr(t_mod, '_mcp_policy', lambda: {'inventory': fail})
    assert t_mod.cmd_mcp(None, t_mod.build_parser().parse_args(['mcp', 'list'])) == 1
    assert 'invalid configuration' in capsys.readouterr().err


def test_config_inventory_and_workflow_controls(t_mod, mcp, monkeypatch):
    from test_config import Menu
    row = sample(mcp)
    class RecordingMenu(Menu):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = []
        def pick(self, label, rows, **kwargs):
            self.calls.append((label, rows, kwargs))
            return super().pick(label, rows, **kwargs)
    api = {**vars(mcp), 'inventory': lambda *a: [row]}
    monkeypatch.setattr(t_mod, '_mcp_policy', lambda: api)
    ui = RecordingMenu(['server:0', 'refresh', 'back'], pages=['enter'])
    t_mod._config_mcp(ui, 0)
    assert all(not kwargs.get('primary') and not kwargs.get('sections') for label, rows, kwargs in ui.calls)
    policy = {'enabled': True}
    ui = RecordingMenu([row['id'], 'disabled', 'back'])
    changed = t_mod._workflow_mcp_menu(ui, policy, 'codex')
    assert changed['mcp']['servers']['docs'] is False and policy == {'enabled': True}
    assert all(not kwargs.get('primary') for label, rows, kwargs in ui.calls)
    ui = RecordingMenu(['reset', 'back'])
    assert t_mod._workflow_mcp_menu(ui, changed, 'codex') == policy
    assert t_mod._workflow_mcp_menu(Menu([], pages=['enter']), changed, 'claude') == changed
    row['sessions'] = True
    calls = []
    monkeypatch.setattr(t_mod, '_config_sessions_mcp', lambda ui, pending, context=(): calls.append((pending, context)))
    ui = RecordingMenu(['server:0', 'back'])
    t_mod._config_mcp(ui, 0)
    assert calls and any('t session tools' in label for key, label in ui.calls[0][1])


def test_workflow_schema_review_and_reset(t_mod, mcp):
    helper = t_mod._workflow()
    document = helper['defaults']()
    policy = document['workflows']['standard']
    changed = mcp.update(policy, sample(mcp), False)
    document = helper['update'](document, changed, name='standard')
    helper['validate'](document)
    assert helper['reset_policy'](changed, 'mcp') == policy
    assert 'MCP servers: 0 enabled · 1 disabled' in helper['summary_lines'](changed)
    before = helper['defaults']()
    assert any('MCP · docs: Inherit client settings → Disabled' in line for line in t_mod._workflow_review(document, before))
    assert 'mcp' not in helper['_legacy_claude_catalogue'](document)['workflows']['standard']
