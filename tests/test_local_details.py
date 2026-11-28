"""Foreground detail reads preserve only matching cached conversation metadata."""
from pathlib import Path
import runpy
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_local_details_use_scoped_scanner_and_preserve_matching_extras(monkeypatch):
    api = runpy.run_path(str(ROOT / 'libexec/t_session_discovery.py'))
    hydrate = api['_hydrate_local']
    namespace = hydrate.__globals__
    real_run_path = namespace['runpy'].run_path
    identity = dict(terminal='123/$1', cwd='/repo', state='detached', agent='codex', stamp='id')
    monkeypatch.setattr(namespace['runpy'], 'run_path', lambda path: (
        {'scan': lambda: ('/socket', {'repo-1': identity})}
        if str(path).endswith('t_live_inventory.py') else real_run_path(path)))
    def run(args, **kwargs):
        assert '_dev_session_rows --lightweight' in args[-1]
        assert kwargs['env']['T_DISCOVERY_SCOPE_ROOTS'] == '/repo'
        assert kwargs['env']['T_DISCOVERY_SCOPE_SLOT'] == '1'
        return SimpleNamespace(returncode=0, stderr='', stdout='id\t/repo\trepo-1\tdetached\tactive\tNew title\tcodex\tmodel\t2026-11-27T20:00:00Z\n')
    monkeypatch.setattr(namespace['subprocess'], 'run', run)
    old = 'id\t/repo\trepo-1\tdetached\tactive\tOld title\tcodex\told model\t-\t{"lead_model":"model"}\n'
    view = {'hosts': [{'host': 'local', 'value': {'rows': old}}]}
    result = hydrate(SimpleNamespace(), view, ['/repo'], '1')
    fields = result['hosts'][0]['value']['rows'].strip().split('\t')
    assert fields[5:9] == ['New title', 'codex', 'model', '2026-11-27T20:00:00Z']
    assert fields[9] == '{"lead_model":"model"}'


def test_legacy_rollout_details_are_scoped_and_share_one_directory_scan(tmp_path, monkeypatch):
    import json
    module = runpy.run_path(str(ROOT / 'libexec/t_session_details.py'))
    home = tmp_path / 'home'
    codex = home / '.codex'
    directory = codex / 'sessions/2026/11/27'
    directory.mkdir(parents=True)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    rows = []
    for number in range(1, 4):
        sid = f'11111111-1111-4111-8111-{number:012d}'
        records = [dict(type='session_meta', payload=dict(id=sid, cwd='/repo' if number < 3 else '/other', source='cli')),
                   dict(type='response_item', payload=dict(role='user', content=[dict(type='input_text', text=f'Legacy title {number}') ])),
                   dict(type='turn_context', payload=dict(model='legacy-model')),
                   dict(type='event_msg', timestamp='2026-11-27T20:00:00Z', payload=dict(type='task_started'))]
        (directory / f'rollout-test-{sid}.jsonl').write_text('\n'.join(map(json.dumps, records)) + '\n')
        rows.append(f'{sid}\t/repo\tapi-{number}\tdetached\tactive\t-\tcodex')
    original = Path.glob
    calls = []
    def glob(path, pattern):
        calls.append((path, pattern))
        return original(path, pattern)
    monkeypatch.setattr(Path, 'glob', glob)
    result = module['enrich'](rows, home, codex, lightweight=True)
    for number in (1, 2):
        assert result[number-1].split('\t')[5:9] == [f'Legacy title {number}', 'codex', 'legacy-model', '2026-11-27T20:00:00Z']
    assert result[2].split('\t')[5:9] == ['-', 'codex', '-', '-']
    assert [call for call in calls if call[0] == codex / 'sessions'] == [(codex / 'sessions', '*/*/*/rollout-*.jsonl')]


def test_indexed_legacy_title_uses_validated_opening_prompt(tmp_path, monkeypatch):
    import json
    import sqlite3
    module = runpy.run_path(str(ROOT / 'libexec/t_session_details.py'))
    home = tmp_path / 'home'
    codex = home / '.codex'
    codex.mkdir(parents=True)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    sid = '11111111-1111-4111-8111-000000000001'
    path = codex / 'legacy.jsonl'
    path.write_text('\n'.join(map(json.dumps, [
        dict(type='session_meta', payload=dict(id=sid, cwd='/repo', source='cli')),
        dict(type='event_msg', payload=dict(type='user_message', message='Opening prompt title')),
        dict(type='turn_context', payload=dict(model='recorded-model')),
        dict(type='event_msg', timestamp='2026-11-27T20:00:00Z', payload=dict(type='task_started')),
    ])) + '\n')
    with sqlite3.connect(codex / 'state_5.sqlite') as db:
        db.execute('create table threads (id, cwd, rollout_path)')
        db.execute('insert into threads values (?,?,?)', (sid, '/repo', str(path)))
    row = f'{sid}\t/repo\tapi-1\tdetached\tactive\t-\tcodex'
    result = module['enrich']([row], home, codex, lightweight=True)[0].split('\t')
    assert result[5:9] == ['Opening prompt title', 'codex', 'recorded-model', '2026-11-27T20:00:00Z']


def test_stale_sidecar_and_supplied_rollout_cannot_leak_metadata(tmp_path, monkeypatch):
    import json
    module = runpy.run_path(str(ROOT / 'libexec/t_session_details.py'))
    home = tmp_path / 'home'
    codex = home / '.codex'
    directory = codex / 'sessions/2026/11/27'
    directory.mkdir(parents=True)
    cache = tmp_path / 'cache'
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache))
    sid = '11111111-1111-4111-8111-000000000001'
    wrong = directory / 'wrong.jsonl'
    right = directory / f'rollout-test-{sid}.jsonl'
    for path, identity, title in [(wrong, 'different-id', 'Wrong title'), (right, sid, 'Right title')]:
        path.write_text('\n'.join(map(json.dumps, [
            dict(type='session_meta', payload=dict(id=identity, cwd='/repo', source='cli')),
            dict(type='event_msg', payload=dict(type='user_message', message=title)),
            dict(type='turn_context', payload=dict(model=title)),
            dict(type='event_msg', timestamp='2026-11-27T20:00:00Z', payload=dict(type='task_started')),
        ])) + '\n')
    sidecar = cache / 'claude-sessions/rollouts' / sid
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(str(wrong))
    row = f'{sid}\t/repo\tapi-1\tdetached\tactive\t-\tcodex\t{wrong}'
    result = module['enrich']([row], home, codex, lightweight=True)[0].split('\t')
    assert result[5:9] == ['Right title', 'codex', 'Right title', '2026-11-27T20:00:00Z']
    right.unlink()
    result = module['enrich']([row], home, codex, lightweight=True)[0].split('\t')
    assert result[5:9] == ['-', 'codex', '-', '-']


def test_public_hydration_uses_cache_api_and_retains_rows_when_stopped(monkeypatch):
    api = runpy.run_path(str(ROOT / 'libexec/t_session_discovery.py'))
    hydrate = api['hydrate_local']
    calls = []
    def request(operation, payload):
        calls.append((operation, payload))
        return {'unavailable': True, 'message': 'Session cache is stopped.'}
    monkeypatch.setitem(hydrate.__globals__, 'request', request)
    view = {'hosts': [{'host': 'local', 'value': {'rows': 'retained'}}]}
    result = hydrate(SimpleNamespace(), view, ['/repo'], '1')
    assert result['hosts'] == view['hosts']
    assert result['unavailable'] and result['message'] == 'Session cache is stopped.'
    assert calls == [('hydrate', {'view': view, 'roots': ['/repo'], 'slot': '1'})]
