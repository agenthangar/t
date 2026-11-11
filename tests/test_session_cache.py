"""Single-owner discovery, RAM serving, persistence, expiry and lifecycle races."""
import copy
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading
import time

import pytest
from conftest import load_script, REPO_ROOT


@pytest.fixture
def cache(tmp_path, monkeypatch):
    module = load_script(REPO_ROOT / 'libexec/t_session_cache.py', 'session_cache_test')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    import tempfile
    directory = tempfile.TemporaryDirectory(prefix='t-cache-test-', dir='/tmp')
    monkeypatch.setattr(module, '_runtime', lambda: Path(directory.name))
    module._test_runtime = directory
    return module


@pytest.fixture
def engine(cache):
    jobs, saves = [], []
    now = [10000]
    memory = cache._Memory(collect=lambda spec: {'rows': 'sid\t/work\tapi-1\tdetached\tactive\ttask\tcodex\n', 'resume': []},
                           clock=lambda: now[0], persist=lambda: saves.append(True),
                           submit=lambda *job: jobs.append(job))
    memory.restore({})
    return memory, now, jobs, saves


def spec(target='local', host=None, context=None):
    return {'host': host or target, 'target': target, 'context': context or {}}


def discover(engine, *hosts):
    return engine.handle('discover', {'hosts': list(hosts) or [spec()]})


def drain(jobs):
    callback, *args = jobs.pop(0)
    callback(*args)


def test_serves_only_ram_and_one_refresh_per_host(engine):
    memory, now, jobs, saves = engine
    cold = discover(memory)
    assert cold['hosts'][0]['value'] is None and len(jobs) == 1
    discover(memory)
    assert len(jobs) == 1
    drain(jobs)
    assert saves
    memory.collect = lambda _: pytest.fail('fresh cache must not scan')
    for _ in range(3):
        fresh = discover(memory)
        assert fresh['hosts'][0]['value']['rows'].startswith('sid')
        assert not fresh['hosts'][0]['stale']
    assert not jobs
    fresh['hosts'][0]['value']['rows'] = 'caller mutation'
    assert discover(memory)['hosts'][0]['value']['rows'].startswith('sid')


def test_exact_hour_and_failed_host_retry(engine):
    memory, now, jobs, saves = engine
    discover(memory, spec('ssh-host', 'mini')); drain(jobs)
    original = discover(memory, spec('ssh-host', 'mini'))['hosts'][0]['value']
    now[0] += 3599
    assert not discover(memory, spec('ssh-host', 'other-alias'))['hosts'][0]['stale']
    assert not jobs
    now[0] += 1
    memory.collect = lambda _: (_ for _ in ()).throw(OSError('banner timeout'))
    stale = discover(memory, spec('ssh-host', 'mini'))
    assert stale['hosts'][0]['value'] == original and len(jobs) == 1
    drain(jobs)
    failed = discover(memory, spec('ssh-host', 'mini'))['hosts'][0]
    assert failed['error'] == 'banner timeout' and failed['value'] == original
    assert not jobs
    now[0] += 3599
    discover(memory, spec('ssh-host')); assert not jobs
    now[0] += 1
    discover(memory, spec('ssh-host')); assert len(jobs) == 1
    memory.collect = lambda _: {'rows': '', 'resume': []}
    drain(jobs)
    recovered = discover(memory, spec('ssh-host'))['hosts'][0]
    assert recovered['error'] == '' and recovered['value']['rows'] == ''
    assert not recovered['stale']


def test_invalidation_only_affected_host_and_rejects_old_refresh(engine):
    memory, now, jobs, saves = engine
    discover(memory, spec(), spec('remote')); drain(jobs); drain(jobs)
    before = memory.snapshot()
    memory.handle('event', {'targets': ['remote']})
    result = discover(memory, spec(), spec('remote'))
    assert not result['hosts'][0]['stale'] and result['hosts'][1]['stale']
    assert len(jobs) == 1
    memory.handle('event', {'targets': ['remote']})
    drain(jobs)
    remote = discover(memory, spec('remote'))['hosts'][0]
    assert remote['checked'] == 0 and len(jobs) == 1
    assert before['entries'] != memory.snapshot()['entries']


def test_restoration_does_not_win_over_lifecycle_event(engine, cache):
    original, now, jobs, _ = engine
    discover(original); drain(jobs)
    saved = original.snapshot()
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *args: None)
    assert discover(restored)['loading']
    assert not restored.entries
    restored.handle('event', {'targets': ['local']})
    restored.restore(saved)
    assert not discover(restored)['loading']
    assert discover(restored)['hosts'][0]['value'] is None
    assert restored.generations['local'] == 1


def test_restoration_preserves_age_clears_worker_and_handles_future(engine, cache):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    saved = memory.snapshot()
    key = next(iter(saved['entries']))
    saved['entries'][key]['refreshing'] = True
    other = cache._Memory(clock=lambda: now[0], submit=lambda *args: None)
    other.restore(saved)
    assert not other.entries[key]['refreshing']
    assert other.entries[key]['checked'] == 10000
    saved['entries'][key]['checked'] = now[0] + 10
    other = cache._Memory(clock=lambda: now[0], submit=lambda *args: None)
    other.restore(saved)
    assert other.entries[key]['checked'] == 0
    other.restore({'entries': {'bad': {}, 'bad2': 'wrong', 'bad3': {'spec': spec(), 'checked': 'bad'}}})
    assert len(other.entries) == 1


def test_event_inserts_verified_row_and_close_removes_it(engine):
    memory, _, jobs, _ = engine
    discover(memory); drain(jobs)
    row = 'new\t/work2\tapi-2\tdetached\tactive\tNew session\tclaude'
    memory.handle('event', {'row': row})
    assert row in discover(memory)['hosts'][0]['value']['rows']
    memory.handle('event', {'row': row, 'event': 'close'})
    assert row not in discover(memory)['hosts'][0]['value']['rows']
    memory.handle('event', {'row': 'malformed'})


def test_artifacts_and_invalid_operations(engine):
    memory, _, _, _ = engine
    assert memory.handle('artifact', {'key': 'file'}) == {'value': None}
    memory.handle('artifact', {'key': 'file', 'value': {'offset': 10}})
    assert memory.handle('artifact', {'key': 'file'})['value'] == {'offset': 10}
    assert memory.handle('status', {})['protocol'] == 1
    with pytest.raises(ValueError): memory.handle('arbitrary', {})
    with pytest.raises(ValueError): discover(memory, {'target': []})


def test_forget_clears_target_across_views_and_rejects_inflight(engine):
    memory, _, jobs, _ = engine
    discover(memory, spec(), spec('remote')); drain(jobs); drain(jobs)
    entry = next(e for e in memory.entries.values() if e['spec']['target'] == 'local')
    other = 'other\t/other\tapi-2\tdetached\tactive\tOther\tclaude'
    entry['value']['rows'] += other + '\n'
    entry['value']['resume'] = ['1\tapi\t1\tsid\t/work\tdate\ttitle\tlocal\tcodex\t-\torg/api\tNONE',
                                '1\tapi\t2\tother\t/other\tdate\ttitle\tlocal\tcodex\t-\torg/api\tNONE']
    entry['value']['catalog'] = {'conversations': [{'sid':'sid'}, {'sid':'other'}]}
    entry['value']['mcp'] = {'recs':[{'id':'sid'}, {'id':'other'}], 'live':{'sid':{},'other':{}}}
    memory.overlays['local'] = {'api-1':{'row':'sid\t/work\tapi-1', 'close':False}, 'api-2':{'row':other, 'close':False}}
    old = memory.generations.get('local', 0)
    result = memory.handle('forget', {'workspace':'/work', 'sid':'sid'})
    assert result['forgotten'] == 2
    assert entry['value']['rows'] == other + '\n'
    assert len(entry['value']['resume']) == 1
    assert entry['value']['catalog']['conversations'] == [{'sid':'other'}]
    assert entry['value']['mcp']['recs'] == [{'id':'other'}]
    assert entry['value']['mcp']['live'] == {'other':{}}
    assert set(memory.overlays['local']) == {'api-2'}
    key = next(k for k,e in memory.entries.items() if e is entry)
    memory.refresh(key, old)
    assert entry['value']['rows'] == other + '\n'
    remote = next(e for e in memory.entries.values() if e['spec']['target'] == 'remote')
    assert '/work' in remote['value']['rows']


def test_forget_by_workspace_and_validation(engine):
    memory, _, jobs, _ = engine
    discover(memory); drain(jobs)
    assert memory.handle('forget', {'workspace':'/work'})['forgotten'] == 1
    for payload in ({}, {'workspace':'relative'}, {'workspace':'/work', 'sid':[]}, {'workspace':'/work', 'target':None}):
        with pytest.raises(ValueError): memory.handle('forget', payload)
    memory.loading=True
    with pytest.raises(ValueError, match='loading'): memory.handle('forget', {'workspace':'/work'})


def test_configuration_namespace_and_alias_dedup(engine):
    memory, _, jobs, _ = engine
    discover(memory, spec('remote', 'one'), spec('remote', 'two'))
    assert len(jobs) == 1
    drain(jobs)
    discover(memory, spec('remote', context={'repos': {'new': '/new'}}))
    assert len(jobs) == 1
    assert len(memory.entries) == 2


def test_sqlite_restoration_and_asynchronous_persistence(cache, engine, tmp_path):
    memory, _, jobs, _ = engine
    discover(memory); drain(jobs)
    disk = cache._Persistence(memory)
    thread = threading.Thread(target=disk.run)
    thread.start()
    disk.wake.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(disk.database) as db:
                row = db.execute('select payload from snapshots').fetchone()
            if row: break
        except sqlite3.Error: pass
        time.sleep(.01)
    else: pytest.fail('snapshot not persisted')
    disk.stop.set(); disk.wake.set(); thread.join(3)
    assert not thread.is_alive()
    assert disk.database.stat().st_mode & 0o777 == 0o600
    saved = json.loads(row[0])
    restored = cache._Memory(clock=lambda: 10001, submit=lambda *args: pytest.fail('restored cache is fresh'))
    restored.restore(saved)
    # DB is gone after loading: every lookup still succeeds from RAM.
    disk.database.unlink()
    assert discover(restored)['hosts'][0]['value']['rows'].startswith('sid')


def test_failed_persistence_does_not_block_ram(cache, engine, monkeypatch):
    memory, _, jobs, _ = engine
    discover(memory); drain(jobs)
    disk = cache._Persistence(memory)
    monkeypatch.setattr(disk, '_open', lambda: (_ for _ in ()).throw(OSError('read only')))
    thread = threading.Thread(target=disk.run); thread.start()
    disk.wake.set()
    deadline = time.monotonic() + 2
    while not memory.persistence_error and time.monotonic() < deadline: time.sleep(.01)
    assert memory.persistence_error == 'read only'
    assert discover(memory)['hosts'][0]['value']['rows'].startswith('sid')
    disk.stop.set(); disk.wake.set(); thread.join(2)


def test_socket_clients_share_memory(cache, engine):
    memory, _, jobs, _ = engine
    cache._private(cache._runtime())
    with cache._Server(str(cache._runtime() / 'service.sock'), cache._Handler) as server:
        server.memory = memory
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            assert cache.request('status')['protocol'] == 1
            view = cache.request('discover', {'hosts': [spec()]})
            assert view['hosts'][0]['value'] is None
            drain(jobs)
            assert cache.request('discover', {'hosts': [spec()]})['hosts'][0]['value']['rows']
            with pytest.raises(ValueError): cache._wire('bad', {})
        finally:
            server.shutdown(); thread.join(2)


def test_client_unavailable_is_bounded_no_scanner_fallback(cache, monkeypatch):
    monkeypatch.setattr(cache, '_wire', lambda *a: (_ for _ in ()).throw(OSError()))
    launches = []
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: launches.append((a, k)))
    ticks = iter([0, .1, 2])
    monkeypatch.setattr(cache.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(cache.time, 'sleep', lambda _: None)
    monkeypatch.setenv('COV_CORE_SOURCE', 'must-not-leak')
    assert cache.request('discover')['unavailable']
    assert len(launches) == 1
    assert 'COV_CORE_SOURCE' not in launches[0][1]['env']
    assert launches[0][1]['stdin'] == subprocess.DEVNULL


def test_private_paths_and_collect_validation(cache, tmp_path, monkeypatch):
    unsafe = tmp_path / 'link'; unsafe.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError): cache._private(unsafe)
    disk = cache._Persistence(cache._Memory(), tmp_path / 'db')
    disk.database.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OSError): disk._open()
    class Result:
        returncode = 0; stdout = '{"rows":"","resume":[]}'; stderr = ''
    calls = []
    monkeypatch.setattr(cache.subprocess, 'run', lambda *a, **k: (calls.append(k), Result())[1])
    assert cache._collect(spec()) == {'rows': '', 'resume': []}
    assert calls[0]['env']['T_DISCOVERY_COLLECTOR'] == '1'
    Result.stdout = '{}'
    with pytest.raises(ValueError): cache._collect(spec())
    Result.returncode = 1; Result.stderr = 'timeout\n banner'
    with pytest.raises(ValueError, match='timeout banner'): cache._collect(spec())


def test_initial_request_schedules_after_restoration(cache):
    jobs = []
    memory = cache._Memory(submit=lambda *job: jobs.append(job))
    assert discover(memory)['loading']
    assert jobs == []
    memory.restore({})
    assert len(jobs) == 1


def test_field_projection_avoids_copying_unrequested_payload(engine):
    memory, _, jobs, _ = engine
    discover(memory); drain(jobs)
    entry = next(iter(memory.entries.values()))
    class NoCopy:
        def __deepcopy__(self, memo): raise AssertionError('copied unrelated MCP data')
    entry['value']['mcp'] = NoCopy()
    result = memory.handle('discover', {'hosts': [spec()], 'fields': ['rows']})
    assert set(result['hosts'][0]['value']) == {'rows'}


def test_private_legacy_import(cache, tmp_path, monkeypatch):
    root = tmp_path / 'cache/claude-sessions'
    (root / 'index').mkdir(parents=True)
    (root / 'index/good').write_text('{"v":1}')
    (root / 'index/bad').write_text('{')
    (root / 'index/list').write_text('[]')
    (root / 'index/link').symlink_to(root / 'index/good')
    (root / 'candidates-one.json').write_text('{"version":1}')
    result = cache._legacy_artifacts()
    assert len(result) == 2
    assert result[str(root / 'index/good')] == {'v': 1}


def test_actual_service_persists_restarts_and_serves_without_collector(tmp_path, monkeypatch):
    """Real IPC/process/disk boundary, with a synthetic collector and no user state."""
    import runpy
    import shutil
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'CODEX_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    module = load_script(REPO_ROOT / 'libexec/t_session_cache.py', 'cache_service_e2e')
    script = """
import runpy,sys,time
m=runpy.run_path(sys.argv[1])
def collect(spec):
    if sys.argv[2] == 'forbid': raise ValueError('collector must not run after restore')
    return {'rows':'sample\\t/work\\tapi-1\\tdetached\\tactive\\tSample\\tcodex\\n','resume':[]}
m['_Memory'].__init__.__defaults__=(collect,time.time,lambda:None,None)
m['_serve']()
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    runtime = module._runtime()
    for mode in ('collect', 'forbid'):
        process = subprocess.Popen([__import__('sys').executable, '-c', script, str(REPO_ROOT / 'libexec/t_session_cache.py'), mode], env=env)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    result = module._wire('discover', {'hosts': [spec()]})
                    if result['hosts'][0]['value'] is not None:
                        break
                except (OSError, ValueError):
                    pass
                assert time.monotonic() < deadline, 'service failed to load synthetic snapshot'
                time.sleep(.02)
            assert result['hosts'][0]['value']['rows'].startswith('sample')
            assert not result['hosts'][0]['error']
        finally:
            process.terminate()
            process.wait(timeout=5)
    assert module._database().exists()
    shutil.rmtree(runtime)


def test_cold_lifecycle_row_survives_restoration_and_restart(cache):
    jobs = []
    memory = cache._Memory(clock=lambda: 10000, submit=lambda *job: jobs.append(job))
    row = 'new\t/work\tapi-2\tdetached\tactive\tNew\tcodex'
    memory.handle('event', {'row': row})
    memory.restore({})
    assert row in discover(memory)['hosts'][0]['value']['rows']
    restored = cache._Memory(clock=lambda: 10000, submit=lambda *job: None)
    restored.restore(memory.snapshot())
    assert row in discover(restored)['hosts'][0]['value']['rows']
    memory.collect = lambda spec: {'rows': '', 'resume': []}
    drain(jobs)
    assert discover(memory)['hosts'][0]['value']['rows'] == ''


def test_service_upgrade_releases_socket_and_flushes(cache, monkeypatch):
    signals = {}
    revision = cache._revision()
    monkeypatch.setattr(cache, '_revision', lambda: revision)
    monkeypatch.setattr(cache.signal, 'signal', lambda number, handler: signals.setdefault(number, handler))
    # Avoid altering the test runner's process-wide umask.
    monkeypatch.setattr(cache.os, 'umask', lambda value: None)
    thread = threading.Thread(target=cache._serve)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while True:
            try:
                if not cache._wire('status', {})['loading']:
                    break
            except (OSError, ValueError):
                pass
            assert time.monotonic() < deadline
            time.sleep(.01)
        # A competing startup never replaces the owner's socket.
        contender = threading.Thread(target=cache._serve)
        contender.start(); contender.join(2)
        assert not contender.is_alive()
        cache._wire('artifact', {'key': 'restart', 'value': {'saved': True}})
        monkeypatch.setattr(cache, '_revision', lambda: ['new-source'])
        with pytest.raises(ValueError, match='version changed'):
            cache._wire('status', {})
        thread.join(3)
        assert not thread.is_alive()
        assert not (cache._runtime() / 'service.sock').exists()
        with sqlite3.connect(cache._database()) as db:
            saved = json.loads(db.execute('select payload from snapshots').fetchone()[0])
        assert saved['artifacts']['restart'] == {'saved': True}
    finally:
        if thread.is_alive():
            signals[cache.signal.SIGTERM]()
            thread.join(3)


def test_standalone_artifact_miss_never_spawns_service(cache, monkeypatch):
    monkeypatch.setattr(cache, '_wire', lambda *a: (_ for _ in ()).throw(OSError('offline')))
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: pytest.fail('artifact lookup started daemon'))
    assert cache.request('artifact', {'key': 'missing'}) == {'value': None}


def test_invalid_disk_snapshot_is_rebuilt_from_ram(cache):
    memory = cache._Memory(submit=lambda *a: None)
    disk = cache._Persistence(memory)
    db = disk._open()
    try:
        db.execute('insert into snapshots values (?,?,?)', (disk.namespace, cache.VERSION, '[]'))
        db.commit()
    finally:
        db.close()
    disk.stop.set(); disk.wake.set()
    disk.run()
    assert not memory.loading and not memory.persistence_error
    db = sqlite3.connect(disk.database)
    try:
        assert json.loads(db.execute('select payload from snapshots').fetchone()[0]) == memory.snapshot()
    finally:
        db.close()
    with pytest.raises(ValueError, match='invalid discovery snapshot'):
        memory.restore({'entries': []})
