"""Retained inventories, incremental updates, and recoverable discovery outages."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_session_cache import cache, engine, spec, discover, drain  # noqa: F401
from test_session_discovery import adapter, collector, view  # noqa: F401
from test_history_data import history, store, app, codex_rows, transcript_file  # noqa: F401


def test_inventory_reconciles_after_twenty_four_hours_and_keeps_rows(engine, cache):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    before = discover(memory)['hosts'][0]['value']
    now[0] += 86399
    assert not discover(memory)['hosts'][0]['stale']
    assert len(jobs) == 1
    now[0] += 1
    host = discover(memory)['hosts'][0]
    assert host['stale'] and host['value'] == before and len(jobs) == 1
    assert cache.TTL == 86400 and cache.FAILURE_TTL == 3600


def test_local_activity_observes_once_without_periodic_refresh(engine):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    before = next(iter(memory.entries.values()))['checked']
    memory.handle('activity', {'targets': ['local']})
    assert next(iter(memory.entries.values()))['checked'] == before
    memory.tick()
    assert len(jobs) == 1
    memory.tick()
    assert len(jobs) == 1
    drain(jobs)
    for _ in range(12):
        now[0] += 5
        memory.tick()
        assert not jobs
    discover(memory)
    assert len(jobs) == 1


def test_collector_bug_has_short_retry_and_cannot_stick_worker(engine, cache):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: (_ for _ in ()).throw(RuntimeError('programming error'))
    discover(memory); drain(jobs)
    host = discover(memory)['hosts'][0]
    assert host['error_kind'] == 'collector' and host['retry_at'] == now[0] + 5
    assert not memory.active_targets
    now[0] += 5
    memory.tick(); drain(jobs)
    assert memory.failures['local']['retry_at'] == now[0] + 15
    assert 'RuntimeError: programming error' in Path(host['diagnostic']).read_text()


def test_online_collector_failure_is_not_transport_cooldown(engine, cache):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: (_ for _ in ()).throw(cache.DiscoveryError('bad response', 'protocol'))
    discover(memory, spec('mini')); drain(jobs)
    assert memory.failures['mini']['retry_at'] == now[0] + 5
    memory.handle('reset-host', {'target': 'mini'})
    memory.collect = lambda _: (_ for _ in ()).throw(cache.DiscoveryError('SSH timed out', 'transport'))
    discover(memory, spec('mini')); drain(jobs)
    assert memory.failures['mini']['retry_at'] == now[0] + 3600


@pytest.mark.parametrize('command', ['open', 'resume'])
def test_warm_browsing_does_not_wait_for_remote_or_activity(adapter, monkeypatch, command):
    snapshot = view()
    snapshot['hosts'][0]['refreshing'] = True
    snapshot['hosts'].append(dict(host='mini', target='mini', value=None, error='timeout'))
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: snapshot)
    forbidden = lambda *a: pytest.fail('Warm browsing waited or prompted')
    if command == 'open':
        result, notice = adapter.open_snapshot(None, forbidden, wait=forbidden)
    else:
        result, notice = adapter.resume_snapshot(None, forbidden, choose=forbidden)
    assert result == snapshot and 'incomplete' in notice


@pytest.mark.parametrize('command', ['open', 'resume'])
def test_cold_service_failure_recovers_without_rerunning_command(adapter, monkeypatch, command):
    data = {'unavailable': True}
    calls = []
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: calls.append(k) or data)
    forbidden = lambda *a: pytest.fail('settled unavailable discovery entered a wait/retry rail')
    result, notice = (adapter.open_snapshot(None, forbidden, wait=forbidden) if command == 'open'
                      else adapter.resume_snapshot(None, forbidden, choose=forbidden))
    assert result is data and notice and len(calls) == 1


def test_cold_skip_and_cancel_never_creates_session(adapter, monkeypatch):
    cold = {'loading': True, 'hosts': []}
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: cold)
    monkeypatch.setattr(adapter, 'wait_for_refresh', lambda *a, **k: dict(cold, wait_timed_out=True))
    result, notice = adapter.open_snapshot(None, lambda *a: pytest.fail('timeout prompted'))
    assert result['wait_timed_out'] and notice


def test_remote_export_unchanged_and_one_record_delta(engine, adapter):
    memory, now, jobs, _ = engine
    catalog = dict(version=1, repositories=[], conversations=[dict(repo='o/r', agent='codex', sid=str(i), title='old') for i in range(1000)])
    memory.collect = lambda _: dict(rows='row', resume=[], catalog=copy.deepcopy(catalog), mcp={'private': 'never exported'})
    payload = {'spec': spec()}
    assert memory.handle('export', payload)['pending']
    drain(jobs)
    first = memory.handle('export', payload)
    payload['generation'] = first['generation']
    unchanged = memory.handle('export', payload)
    assert 'full' not in unchanged and 'delta' not in unchanged
    assert len(json.dumps(unchanged)) < 400 and 'mcp' not in first['full']
    catalog['conversations'][42]['title'] = 'new turn'
    memory.handle('event', {'targets': ['local']})
    discover(memory)
    memory.tick(); drain(jobs)
    delta = memory.handle('export', payload)
    assert len(delta['delta']['changed']) == 1 and not delta['delta']['removed']
    merged, changed = adapter.apply_remote(delta, {**first['full'], '_remote_generation': first['generation']})
    assert len(merged['catalog']['conversations']) == 1000 and changed[0]['sid'] == '42'
    assert len(json.dumps(delta)) < 1000


def test_deleted_remote_record_is_removed_from_snapshot(engine, adapter):
    memory, _, jobs, _ = engine
    catalog = dict(version=1, repositories=[], conversations=[dict(repo='o/r', agent='codex', sid='one')])
    memory.collect = lambda _: dict(rows='', resume=[], catalog=copy.deepcopy(catalog))
    payload = {'spec': spec()}
    memory.handle('export', payload); drain(jobs)
    first = memory.handle('export', payload)
    catalog['conversations'] = []
    memory.handle('event', {'targets': ['local']}); discover(memory); memory.tick(); drain(jobs)
    result = memory.handle('export', dict(payload, generation=first['generation']))
    merged, _ = adapter.apply_remote(result, {**first['full'], '_remote_generation': first['generation']})
    assert merged['catalog']['conversations'] == []


def test_incremental_catalog_parses_only_changed_file(app, history, store, monkeypatch):
    artifacts = {}
    def request(op, payload):
        assert op == 'artifact'
        if 'value' in payload:
            artifacts[payload['key']] = copy.deepcopy(payload['value'])
        return {'value': copy.deepcopy(artifacts.get(payload['key']))}
    monkeypatch.setattr(history, '_CACHE_REQUEST', request)
    monkeypatch.setattr(history, 'run', lambda *a, **k: pytest.fail('Display discovery contacted GitHub'))
    app.cache_discovery = True
    paths = [transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions' / f'rollout-{i}.jsonl',
                            codex_rows(app.cfg.repos['repo'], sid=str(i))) for i in range(3)]
    calls = []
    scan = store.scan
    monkeypatch.setattr(store, 'scan', lambda path, *a, **k: (calls.append(str(path)), scan(path, *a, **k))[1])
    first = app.local_catalog()
    assert len(first['conversations']) == 3 and len(calls) == 3
    calls.clear()
    assert app.local_catalog() == first and not calls
    with paths[1].open('a') as stream:
        stream.write(json.dumps({'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': 'one new turn'}]}}) + '\n')
    app.local_catalog()
    assert calls == [str(paths[1])]
    paths[2].unlink()
    assert len(app.local_catalog()['conversations']) == 2


def test_canonical_seed_source_is_one_way(cache, tmp_path, monkeypatch):
    canonical = tmp_path / 'canonical'
    (canonical / 'libexec').mkdir(parents=True)
    (canonical / 'libexec/t_session_cache.py').touch()
    gitdir = canonical / '.git/worktrees/slot'
    gitdir.mkdir(parents=True)
    worktree = tmp_path / 'slot'
    worktree.mkdir()
    (worktree / '.git').write_text(f'gitdir: {gitdir}\n')
    monkeypatch.setattr(cache, 'SOURCE', worktree)
    assert cache._canonical_source() == canonical
    monkeypatch.setattr(cache, 'SOURCE', canonical)
    assert cache._canonical_source() is None


def test_collector_error_retains_last_line_and_private_evidence(cache, monkeypatch):
    detail = 'Traceback\n' + 'frame\n' * 200 + 'KeyError: terminal cause'
    result = SimpleNamespace(returncode=1, stdout='', stderr=json.dumps(dict(kind='collector', message='KeyError: terminal cause', detail=detail)))
    monkeypatch.setattr(cache.subprocess, 'run', lambda *a, **k: result)
    with pytest.raises(cache.DiscoveryError, match='terminal cause') as caught:
        cache._collect({'target': 'mini'})
    path = Path(caught.value.diagnostic)
    assert path.read_text() == detail and path.stat().st_mode & 0o777 == 0o600


def test_restored_observation_leases_do_not_restart_unused_collectors(engine, cache):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    saved = memory.snapshot()
    for entry in saved['entries'].values():
        entry.update(pending=True, refreshing=True)
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *a: jobs.append(a))
    restored.restore(saved)
    now[0] += 30
    restored.tick()
    assert not jobs and all(not e['pending'] for e in restored.entries.values())
    discover(restored)
    restored.tick()
    assert len(jobs) == 1


def test_native_rename_refreshes_without_reparsing_unchanged_rollout(app, history, store, monkeypatch):
    import sqlite3
    import time
    artifacts = {}
    def request(op, payload):
        if 'value' in payload:
            artifacts[payload['key']] = copy.deepcopy(payload['value'])
        return {'value': copy.deepcopy(artifacts.get(payload['key']))}
    monkeypatch.setattr(history, '_CACHE_REQUEST', request)
    app.cache_discovery = True
    path = transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-one.jsonl',
                           codex_rows(app.cfg.repos['repo'], sid='one'))
    database = Path(os.environ['CODEX_HOME']) / 'state_5.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('create table threads (id text, cwd text, rollout_path text, name text, updated_at real)')
        db.execute('insert into threads values (?, ?, ?, ?, ?)',
                   ('one', app.cfg.repos['repo'], str(path), 'Original name', time.time() + 1))
    first = app.local_catalog()
    assert first['conversations'][0]['title'] == 'Original name'
    assert first['conversations'][0]['title_kind'] == 'name'
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'response_item', 'payload': {
            'role': 'user', 'content': [{'text': 'This should not replace the native name'}]}}) + '\n')
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert app.local_catalog()['conversations'][0]['title'] == 'Original name'
    monkeypatch.setattr(store, 'scan', lambda *a, **k: pytest.fail('Unchanged rollout was reparsed'))
    with sqlite3.connect(database) as db:
        db.execute('update threads set name=?, updated_at=?', ('Renamed', time.time() + 2))
    assert app.local_catalog()['conversations'][0]['title'] == 'Renamed'
    assert app.catalog_changed_paths == {str(path)}
    app.local_catalog()
    assert not app.catalog_changed_paths


def test_codex_session_index_names_exact_rollout_without_sqlite(app, history, monkeypatch):
    artifacts = {}
    def request(op, payload):
        if 'value' in payload:
            artifacts[payload['key']] = copy.deepcopy(payload['value'])
        return {'value': copy.deepcopy(artifacts.get(payload['key']))}
    monkeypatch.setattr(history, '_CACHE_REQUEST', request)
    app.cache_discovery = True
    codex = Path(os.environ['CODEX_HOME'])
    path = transcript_file(codex / 'sessions/rollout-one.jsonl', codex_rows(app.cfg.repos['repo'], sid='one'))
    before, = app.local_catalog()['conversations']
    assert before['title_kind'] == 'prompt'
    index = codex / 'session_index.jsonl'
    index.write_text(json.dumps(dict(id='one', thread_name='Native name', updated_at='2026-11-20T00:00:00Z')) + '\n')
    after, = app.local_catalog()['conversations']
    assert (after['title'], after['title_kind']) == ('Native name', 'name')
    assert app.catalog_changed_paths == {str(path)}
    assert app.local_catalog()['conversations'] == [after]
    assert not app.catalog_changed_paths


@pytest.mark.parametrize('legacy', [False, True])
def test_remote_generation_and_mixed_version_fallback(adapter, collector, monkeypatch, legacy):
    catalog = dict(version=1, repositories=[], conversations=[])
    full = dict(rows='', catalog=catalog)
    replies = [SimpleNamespace(returncode=0, stdout=json.dumps(full if legacy else
               dict(protocol=2, generation='one', full=full, health={'checked': 10000})), stderr='')]
    if legacy:
        replies.insert(0, SimpleNamespace(returncode=2, stdout='', stderr='unknown command __cache-export'))
    calls = []
    def remote(argv, **kwargs):
        calls.append((argv, kwargs))
        return replies.pop(0)
    monkeypatch.setattr(adapter.subprocess, 'run', remote)
    value = adapter.collect({'target': 'mini'}, collector)
    assert value['catalog'] == catalog
    assert 'zsh -lic' in calls[0][0][-1]
    if legacy:
        assert value['_legacy_remote'] and len(calls) == 2
    else:
        replies.append(SimpleNamespace(returncode=0, stdout=json.dumps(dict(protocol=2, generation='one',
                        health={'checked': 10001})), stderr=''))
        again = adapter.collect({'target': 'mini', 'previous': value}, collector)
        assert again['catalog'] == catalog and again['_remote_health']['checked'] == 10001
        assert json.loads(calls[-1][1]['input']) == {'generation': 'one'}


def test_shell_launch_error_preserves_bounded_cause_and_never_stdout(history, monkeypatch):
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stdout='private conversation output',
        stderr='x' * 1000 + '\n\x1b[31mt resume: selected owner changed (conversation)\x1b[0m'))
    with pytest.raises(history.HistoryError) as caught:
        history.run(['zsh', '-lic', 'launch'], launch_diagnostics=True)
    message = str(caught.value)
    assert 'selected owner changed (conversation)' in message
    assert 'private conversation output' not in message and '\x1b' not in message and len(message) < 900


def test_private_export_and_activity_routes(t_mod, monkeypatch, capsys):
    import io
    import sys
    cfg = SimpleNamespace(hosts={'mini': 'user@mini'})
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    calls = []
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: {
        'export_local': lambda c, p: {'protocol': 2, 'generation': p['generation']},
        'request': lambda op, p: calls.append((op, p))})
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"generation":"same"}'))
    assert t_mod._main(['__cache-export']) == 0
    assert json.loads(capsys.readouterr().out) == {'protocol': 2, 'generation': 'same'}
    assert t_mod._main(['__cache-event', 'mini', '--activity']) == 0
    assert calls == [('activity', {'targets': ['user@mini']})]


def test_private_collector_wraps_error_without_stdout(t_mod, monkeypatch, capsys):
    monkeypatch.setenv('T_DISCOVERY_COLLECTOR', '0')
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: {'collect': lambda *a:
                        (_ for _ in ()).throw(RuntimeError('collector cause'))})
    assert t_mod._main(['__cache-collect-local']) == 1
    output = capsys.readouterr()
    assert output.out == ''
    failure = json.loads(output.err)
    assert failure['kind'] == 'collector' and failure['message'] == 'collector cause'
    assert 'RuntimeError: collector cause' in failure['detail']


def test_live_rows_do_not_settle_cold_saved_discovery(engine, adapter):
    memory, now, jobs, _ = engine
    memory.collect = lambda spec: dict(rows='live-row', resume=[]) if spec.get('live_only') else dict(
        rows='live-row', resume=['saved-row'], catalog={'conversations': []})
    discover(memory); drain(jobs)
    first = discover(memory)
    assert first['hosts'][0]['refreshing']
    assert adapter._usable(first, ['rows'])
    assert not adapter._usable(first, ['rows', 'resume'])
    memory.tick(); drain(jobs)
    assert adapter._usable(discover(memory), ['rows', 'resume'])


def test_local_demand_is_due_after_five_seconds_and_observers_never_renew(engine):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    now[0] += 5
    discover(memory)
    assert not jobs
    now[0] += .01
    first = discover(memory)['hosts'][0]
    assert first['value'] and first['refreshing'] and len(jobs) == 1
    discover(memory); assert len(jobs) == 1
    drain(jobs)
    now[0] += 100
    for _ in range(3):
        host = memory.handle('discover', {'hosts': [spec()], 'fields': ['rows'], 'observe': True})['hosts'][0]
        memory.tick()
        assert host['value'] and not host['refreshing'] and not jobs


def test_cold_live_first_finishes_catalog_without_another_request(engine):
    memory, now, jobs, _ = engine
    calls = []
    def collect(spec):
        calls.append(spec['live_only'])
        return dict(rows='live', resume=[]) if spec['live_only'] else dict(rows='live', resume=['saved'], catalog={})
    memory.collect = collect
    discover(memory); drain(jobs)
    memory.tick(); assert len(jobs) == 1
    drain(jobs)
    now[0] += 60
    memory.tick()
    assert calls == [True, False] and not jobs


def test_local_pending_and_invalidated_workers_finish_original_demand(engine):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: {'_pending': True}
    discover(memory); drain(jobs)
    now[0] += 1
    memory.tick(); assert len(jobs) == 1
    memory.handle('event', {'targets': ['local']})
    memory.collect = lambda _: dict(rows='current', resume=[], catalog={})
    drain(jobs)  # Superseded generation cannot publish or consume demand.
    assert next(iter(memory.entries.values())).get('value') is None
    memory.tick(); drain(jobs)
    assert next(iter(memory.entries.values()))['value']['rows'] == 'current'
    now[0] += 60
    memory.tick(); assert not jobs


def test_local_failure_settles_until_new_demand_and_observers_do_not_retry(engine):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: (_ for _ in ()).throw(RuntimeError('broken'))
    discover(memory); drain(jobs)
    now[0] += 6
    memory.handle('discover', {'hosts': [spec()], 'observe': True})
    memory.tick(); assert not jobs
    discover(memory); assert len(jobs) == 1


def test_local_invalidation_and_activity_do_not_resurrect_unused_contexts(engine):
    memory, now, jobs, _ = engine
    discover(memory, spec(context={'repos': {'old': '/old'}})); drain(jobs)
    now[0] += 3601
    discover(memory, spec(context={'repos': {'new': '/new'}})); drain(jobs)
    memory.handle('event', {'targets': ['local']})
    memory.tick(); assert not jobs
    memory.handle('activity', {'targets': ['local']})
    memory.tick(); assert len(jobs) == 1
    drain(jobs)
    assert all(not entry.get('requested') for entry in memory.entries.values())


def test_remote_observation_lease_remains_periodic(engine):
    memory, now, jobs, _ = engine
    discover(memory, spec('remote')); drain(jobs)
    now[0] += 29
    memory.tick(); assert not jobs
    now[0] += 1
    memory.tick(); assert len(jobs) == 1
    drain(jobs)
