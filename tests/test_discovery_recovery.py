"""Collector integration boundaries: projections, peers, and restart recovery."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from test_session_cache import cache, engine, spec, discover, drain  # noqa: F401
from test_session_discovery import adapter, collector  # noqa: F401
from test_history_data import history, store, app, codex_rows, transcript_file  # noqa: F401


def test_local_collector_reuses_unchanged_projection_and_removes_missing_source(adapter, monkeypatch):
    artifacts, indexed, projected = {}, [], []
    records = {name: dict(repo='o/r', agent='codex', sid=name, title=name) for name in ('a', 'b')}
    changed = set(records)
    class History:
        def __init__(self, cfg, api):
            self.cfg, self.store = cfg, SimpleNamespace(close=lambda: None)
        def branch_prs(self, records, **kwargs):
            pass
        def pr_states(self, records):
            return {}
        def annotate_resume_work(self, records, owners):
            pass
        def local_catalog(self):
            self.catalog_paths = set(records)
            self.catalog_changed_paths = set(changed)
            return dict(version=1, repositories=[], conversations=copy.deepcopy(list(records.values())))
    original = adapter.runpy.run_path
    monkeypatch.setattr(adapter.runpy, 'run_path', lambda path, *a, **k:
                        {'HistoryCommand': History} if str(path).endswith('/t_history.py') else original(path, *a, **k))
    def request(op, payload):
        if 'value' in payload:
            artifacts[payload['key']] = copy.deepcopy(payload['value'])
        return {'value': copy.deepcopy(artifacts.get(payload['key']))}
    monkeypatch.setattr(adapter, 'request', request)
    monkeypatch.setattr(adapter.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='live', stderr=''))
    def candidates(catalog, history, host):
        for record in catalog['conversations']:
            projected.append(record['sid'])
            yield '\t'.join(['1', 'repo', '1', record['sid'], '/work', 'date', record['title'], host, 'codex', '-', 'o/r', 'NONE'])
    monkeypatch.setattr(adapter, '_catalog_candidates', candidates)
    def index(path, directory):
        indexed.append(path)
        return dict(path=path, sid=path) if path in records else None
    api = dict(Config=lambda: SimpleNamespace(worktree_root='/worktrees'), ZSH_PRELUDE='',
               _index_update=index, _index_dir=lambda: '/index', _index_dedupe=lambda r: r,
               _mcp_context=lambda cfg, state: dict(state, live={}, now=0, cache_root=None),
               _session_row=lambda cfg, rec, *a: dict(sid=rec['sid'], title=records[rec['sid']]['title']))
    assert adapter.collect({'target': 'local', 'live_only': True}, api)['rows'] == 'live'
    assert not indexed and not projected
    first = adapter.collect({'target': 'local'}, api)
    assert len(first['resume']) == 2 and set(indexed) == set(projected) == {'a', 'b'}
    indexed.clear(); projected.clear(); changed.clear()
    assert adapter.collect({'target': 'local'}, api)['resume'] == first['resume']
    assert not indexed and not projected
    records['a']['title'] = 'new turn'; del records['b']; changed.update(('a', 'b'))
    updated = adapter.collect({'target': 'local'}, api)
    assert len(updated['resume']) == 1 and 'new turn' in updated['resume'][0]
    assert projected == ['a'] and set(indexed) == {'a', 'b'}
    assert [rec['sid'] for rec in updated['mcp']['recs']] == ['a']


def test_remote_delta_preserves_unchanged_candidates_and_replaces_only_changed(adapter, collector, monkeypatch):
    def row(sid): return dict(repo='o/r', agent='codex', sid=sid)
    def candidate(sid): return '\t'.join(['1', 'repo', '1', sid, '/work', 'date', sid, 'mini', 'codex', '-', 'o/r', 'NONE'])
    previous = dict(rows='', resume=[candidate(s) for s in ('a', 'b', 'c')],
                    catalog=dict(version=1, repositories=[], conversations=[row(s) for s in ('a', 'b', 'c')]),
                    _remote_generation='old')
    delta = dict(protocol=2, generation='new', health={}, delta=dict(fields={},
                 catalog=dict(version=1, repositories=[]), changed=[row('a')],
                 removed=[adapter._CACHE['record_key'](row('b'))]))
    monkeypatch.setattr(adapter.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(delta), stderr=''))
    calls = []
    def project(catalog, *a):
        calls.append(catalog['conversations'])
        return [candidate('a') + '-updated']
    monkeypatch.setattr(adapter, '_catalog_candidates', project)
    value = adapter.collect({'target': 'mini', 'previous': previous}, collector)
    assert value['resume'] == [candidate('c'), candidate('a') + '-updated']
    assert calls == [[row('a')]]
    assert previous['resume'] == [candidate(s) for s in ('a', 'b', 'c')]


@pytest.mark.parametrize('response,kind', [
    ('pending', None), ('pending-error', 'collector'), ('invalid-json', 'protocol'), ('timeout', 'timeout')])
def test_remote_cold_and_malformed_responses_have_distinct_outcomes(adapter, collector, monkeypatch, response, kind):
    def remote(*a, **k):
        if response == 'timeout':
            raise adapter.subprocess.TimeoutExpired('ssh', 30)
        health = {'error': 'collector broke', 'error_kind': 'collector'} if response == 'pending-error' else {}
        return SimpleNamespace(returncode=0, stdout='broken' if response == 'invalid-json' else
                               json.dumps(dict(protocol=2, pending=True, health=health)), stderr='')
    monkeypatch.setattr(adapter.subprocess, 'run', remote)
    if kind is None:
        assert adapter.collect({'target': 'mini'}, collector) == {'_pending': True}
    else:
        with pytest.raises(adapter._CACHE['DiscoveryError']) as caught:
            adapter.collect({'target': 'mini'}, collector)
        assert caught.value.kind == kind
    with pytest.raises(adapter._CACHE['DiscoveryError'], match='baseline'):
        adapter.apply_remote({'protocol': 2, 'generation': 'x'}, {})


def test_old_peer_without_any_cache_endpoint_uses_login_shell_fallback(adapter, collector, monkeypatch):
    replies = iter([SimpleNamespace(returncode=2, stdout='', stderr='unknown command')] * 2 + [
        SimpleNamespace(returncode=0, stdout=json.dumps(dict(rows='', catalog=dict(
            version=1, repositories=[], conversations=[]))), stderr='')])
    calls = []
    monkeypatch.setattr(adapter.subprocess, 'run', lambda argv, **kw: (calls.append(argv), next(replies))[1])
    value = adapter.collect({'target': 'mini'}, collector)
    assert value['_legacy_remote'] and len(calls) == 3
    assert all('zsh -lic' in argv[-1] for argv in calls)
    assert '__history-catalog' in calls[-1][-1]


def test_remote_pending_then_failed_health_retains_inventory(engine):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: {'_pending': True}
    discover(memory, spec('mini')); drain(jobs)
    assert discover(memory, spec('mini'))['hosts'][0]['refreshing']
    now[0] += 1
    memory.collect = lambda _: dict(rows='retained', resume=[], _legacy_remote=True,
        _remote_health={'checked': 9999, 'error': 'collector failed', 'error_kind': 'collector'})
    memory.tick(); drain(jobs)
    host = discover(memory, spec('mini'))['hosts'][0]
    assert host['value']['rows'] == 'retained' and host['error_kind'] == 'collector'
    assert host['checked'] == 9999 and host['retry_at'] == now[0] + 5


def test_worktree_seeds_only_matching_canonical_namespace(cache, engine, tmp_path, monkeypatch):
    memory, _, jobs, _ = engine
    discover(memory); drain(jobs)
    canonical = tmp_path / 'canonical'
    disk = cache._Persistence(memory)
    identity = cache._identity(); identity[0] = str(canonical)
    with closing(disk._open()) as db, db:
        db.execute('insert into snapshots values (?,?,?)', (cache._digest(identity), 1, json.dumps(memory.snapshot())))
    restored = cache._Memory(submit=lambda *a: pytest.fail('Seed resumed an unused observer'))
    monkeypatch.setattr(cache, '_canonical_source', lambda: canonical)
    reader = cache._Persistence(restored)
    thread = threading.Thread(target=reader.run); thread.start()
    try:
        deadline = time.monotonic() + 3
        while restored.loading and time.monotonic() < deadline:
            time.sleep(.01)
        assert not restored.loading and not restored.persistence_error
        assert len(restored.entries) == 1
        assert next(iter(restored.entries.values()))['value']['rows'].startswith('sid')
        restored.tick()
    finally:
        reader.stop.set(); reader.wake.set(); thread.join(3)
    assert not thread.is_alive()


def test_incremental_catalog_tolerates_damaged_optional_sources(app, history, store, tmp_path, monkeypatch):
    monkeypatch.setattr(history, '_CACHE_REQUEST', lambda *a: {'value': None})
    app.cache_discovery = True
    root = Path(os.environ['CODEX_HOME'])
    valid = transcript_file(tmp_path / 'external/rollout-good.jsonl', codex_rows(app.cfg.repos['repo'], sid='good'))
    transcript_file(root / 'sessions/rollout-subagent.jsonl', [
        {'type': 'session_meta', 'payload': {'id': 'subagent', 'thread_source': 'subagent'}}])
    broken = root / 'sessions/rollout-broken.jsonl'; broken.write_text('partial header')
    transcript_file(root / 'sessions/rollout-orphan.jsonl', codex_rows('/unregistered', sid='orphan'))
    transcript_file(root / 'sessions/rollout-no-id.jsonl', [{'type': 'session_meta', 'payload': {'cwd': app.cfg.repos['repo']}}])
    failed = transcript_file(root / 'sessions/rollout-read-error.jsonl', codex_rows(app.cfg.repos['repo'], sid='failed'))
    scan = store.scan
    def guarded(path, *a, **k):
        if path == failed:
            raise history.HistoryError('source changed during observation')
        return scan(path, *a, **k)
    monkeypatch.setattr(store, 'scan', guarded)
    (root / 'state_6.sqlite').write_text('unreadable optional index')
    with closing(sqlite3.connect(root / 'state_5.sqlite')) as db, db:
        db.execute('create table threads (id text, cwd text, rollout_path text, source text)')
        db.executemany('insert into threads values (?,?,?,?)', [
            ('good', app.cfg.repos['repo'], str(valid), 'cli'),
            ('missing', app.cfg.repos['repo'], str(tmp_path / 'missing'), 'cli'),
            ('child', app.cfg.repos['repo'], str(tmp_path / 'child'), '{"subagent":true}')])
    catalog = app.local_catalog()
    assert [row['sid'] for row in catalog['conversations']] == ['good']
    assert str(valid) in app.catalog_paths


def test_candidate_election_throttles_failed_starts_but_rearms_after_recovery(cache, monkeypatch):
    starts = []
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: starts.append(a))
    cache._private(cache._runtime())
    (cache._runtime() / 'start.lock').write_text('partial marker')
    cache._start_candidate()
    cache._start_candidate()
    assert len(starts) == 1
    cache._start_candidate(ready=True)
    cache._start_candidate()
    assert len(starts) == 2


def test_unwritable_diagnostics_cannot_leave_a_refresh_stuck(engine, cache, monkeypatch):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: (_ for _ in ()).throw(RuntimeError('collector bug'))
    monkeypatch.setattr(cache, '_diagnostic', lambda *a: (_ for _ in ()).throw(OSError('read only')))
    discover(memory); drain(jobs)
    host = discover(memory)['hosts'][0]
    assert host['error'] == 'collector bug' and host['retry_at'] == now[0] + 5
    assert not memory.active_targets and not host['diagnostic']


def test_open_retry_rechecks_network_before_resetting_shared_host(adapter, monkeypatch):
    import errno
    import socket
    from test_offline_discovery import network
    network(monkeypatch, {socket.AF_INET: errno.EPERM, socket.AF_INET6: errno.EPERM})
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: pytest.fail('offline request reached discovery'))
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: pytest.fail('offline request reset shared host'))
    with pytest.raises(OSError, match='network unavailable'):
        adapter.open_snapshot(SimpleNamespace(hosts={'mini': 'mini'}), lambda *a: pytest.fail('offline retry rail'))


@pytest.mark.parametrize('old_key', ['absolute', 'empty', 'relative', 'different', 'other-source'])
def test_legacy_identity_import_requires_proven_config_and_source(cache, engine, tmp_path, monkeypatch, old_key):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    local = tmp_path / 'config.zsh'; local.touch()
    alias = tmp_path / 'alias.zsh'; alias.symlink_to(local)
    monkeypatch.setenv('T_LOCAL_RC', str(alias))
    monkeypatch.setattr(cache, '_canonical_source', lambda: None)
    disk = cache._Persistence(memory)
    identity = cache._identity()
    identity[7] = {'absolute': str(alias), 'empty': '', 'relative': './alias.zsh',
                   'different': str(tmp_path / 'different.zsh'), 'other-source': str(alias)}[old_key]
    if old_key == 'other-source':
        identity[0] = str(tmp_path / 'other-source')
    snapshot = memory.snapshot()
    snapshot['failures']['mini'] = dict(error='offline', retry_at=now[0] + 100, kind='transport')
    with closing(disk._open()) as db, db:
        db.execute('insert into snapshots values (?,?,?)', (cache._digest(identity), 1, json.dumps(snapshot)))
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *a: pytest.fail('Restoration scheduled collection'))
    reader = cache._Persistence(restored)
    reader.stop.set()
    reader.run()
    assert not restored.loading and not restored.persistence_error
    assert bool(restored.entries) == (old_key == 'absolute')
    if old_key == 'absolute':
        assert next(iter(restored.entries.values()))['checked'] == now[0]
        assert restored.failures['mini']['retry_at'] == now[0] + 100


def test_loading_demand_retains_requested_missing_fields(cache, engine):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    snapshot = memory.snapshot()
    entry = next(iter(snapshot['entries'].values()))
    entry['value'] = dict(rows='live', resume=[])
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *job: jobs.append(job))
    restored.handle('discover', {'hosts': [spec()], 'fields': ['rows', 'catalog']})
    restored.restore(snapshot)
    assert next(iter(restored.entries.values()))['requested']
    now[0] += 5
    restored.tick()
    assert len(jobs) == 1


def test_observer_after_service_replacement_does_not_invent_missing_work(cache):
    jobs = []
    memory = cache._Memory(submit=lambda *job: jobs.append(job))
    memory.handle('discover', {'hosts': [spec()], 'fields': ['catalog'], 'observe': True})
    memory.restore({})
    host = memory.handle('discover', {'hosts': [spec()], 'fields': ['catalog'], 'observe': True})['hosts'][0]
    assert not host['refreshing'] and not jobs and host['stale']


def test_browse_resumes_restored_live_only_stage_without_idle_restart(cache, engine):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: dict(rows='live', resume=[])
    discover(memory); drain(jobs)
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *job: jobs.append(job))
    restored.restore(memory.snapshot())
    restored.tick(); assert not jobs
    host = restored.handle('discover', {'hosts': [spec()], 'fields': ['rows', 'resume']})['hosts'][0]
    assert host['refreshing'] and len(jobs) == 1
