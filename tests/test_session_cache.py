"""Single-owner discovery, RAM serving, persistence, expiry and lifecycle races."""
import copy
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
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
    module._real_runtime = module._runtime
    monkeypatch.setattr(module, '_runtime', lambda: Path(directory.name))
    module._test_runtime = directory
    return module


@pytest.fixture
def engine(cache):
    jobs, saves = [], []
    now = [10000]
    memory = cache._Memory(collect=lambda spec: {'rows': 'sid\t/work\tapi-1\tdetached\tactive\ttask\tcodex\n', 'resume': [],
                                               'catalog': {'version': 1, 'repositories': [], 'conversations': []}},
                           clock=lambda: now[0], persist=lambda: saves.append(True),
                           submit=lambda *job: jobs.append(job))
    memory.restore({})
    return memory, now, jobs, saves


def test_invalidation_detaches_without_cache_io_or_stale_live_rows(cache, monkeypatch):
    calls = []
    monkeypatch.setenv('COV_CORE_SOURCE', 'measured-parent')
    monkeypatch.setattr(cache, 'request', lambda *a: pytest.fail('foreground cache request'))
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **kw: calls.append((a, kw)))
    cache.invalidate(['local', 'remote', 'local'])
    (argv,), options = calls.pop()
    assert argv[-2] == 'invalidate'
    assert json.loads(argv[-1]) == ['local', 'remote']
    assert options['start_new_session'] and options['close_fds']
    assert options['cwd'] == '/'
    assert all(options[key] == subprocess.DEVNULL for key in ('stdin', 'stdout', 'stderr'))
    assert not any(key.startswith('COV_CORE_') for key in options['env'])
    cache.invalidate()
    assert json.loads(calls.pop()[0][0][-1]) == ['local']
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **kw: (_ for _ in ()).throw(OSError('fork failed')))
    cache.invalidate()  # Optional discovery cannot fail a launch.


def test_invalidation_returns_while_cache_service_is_stalled(cache, monkeypatch):
    # Use the worker's real private namespace, derived from this fixture's HOME.
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/tmp')
    root = cache._real_runtime()
    cache._private(root)
    address = root / 'service.sock'
    received, release = threading.Event(), threading.Event()
    messages, children = [], []
    popen = subprocess.Popen
    def spawn(*a, **kw):
        child = popen(*a, **kw)
        children.append(child)
        return child
    monkeypatch.setattr(cache.subprocess, 'Popen', spawn)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address))
        listener.listen()
        listener.settimeout(5)
        def serve():
            with listener.accept()[0] as connection:
                with connection.makefile('rb') as stream:
                    messages.append(json.loads(stream.readline()))
                received.set()
                release.wait(5)
                try:
                    connection.sendall(b'{"invalidated":["local"]}\n')
                except OSError:
                    pass
        server = threading.Thread(target=serve)
        server.start()
        try:
            cache.invalidate()
            assert received.wait(3)
            assert children[0].poll() is None
            assert messages[0]['payload'] == {'targets': ['local']}
            # The caller has returned while the service deliberately withholds
            # acknowledgement. Only now permit the detached worker to finish.
            release.set()
            assert children[0].wait(timeout=5) == 0
        finally:
            release.set()
            server.join(6)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
            address.unlink(missing_ok=True)
            root.rmdir()


def spec(target='local', host=None, context=None):
    return {'host': host or target, 'target': target, 'context': context or {}}


def discover(engine, *hosts):
    return engine.handle('discover', {'hosts': list(hosts) or [spec()]})


def test_explicit_refresh_coalesces_without_evicting_retained_rows(engine, cache):
    memory, now, jobs, _ = engine
    host = spec('mini')
    discover(memory, host)
    drain(jobs)
    old = discover(memory, host)['hosts'][0]['value']
    assert old
    memory.failures['mini'] = dict(retry_at=now[0] + 60, error='unreachable', kind='transport')
    outcomes = iter([{'_pending': True}, {'rows': 'New rows', 'resume': []}])
    requests = []
    def collect(target):
        requests.append(target.get('refresh', False))
        return next(outcomes)
    memory.collect = collect
    for _ in range(3):
        latest = memory.handle('discover', {'hosts': [host], 'refresh': True})['hosts'][0]
        assert latest['refreshing'] and latest['value'] == old
        assert len(jobs) == 1
    drain(jobs)
    assert requests == [True] and discover(memory, host)['hosts'][0]['value'] == old
    now[0] += 1
    discover(memory, host)
    drain(jobs)
    assert requests == [True, False]
    refreshed = discover(memory, host)['hosts'][0]
    assert not refreshed['refreshing'] and refreshed['value']['rows'] == 'New rows'


def test_observing_discovery_neither_schedules_nor_renews_collectors(engine, cache):
    memory, now, jobs, _ = engine
    discover(memory)
    drain(jobs)
    existing = next(iter(memory.entries.values()))
    last_used = existing['last_used']
    now[0] += cache.TTL + 1
    def observe():
        return memory.handle('discover', {'hosts': [spec(), spec('new')], 'observe': True})
    saved = observe()['hosts']
    assert saved[0]['stale'] and saved[0]['value'] and saved[1]['value'] is None
    assert not jobs and len(memory.entries) == 1 and existing['last_used'] == last_used
    discover(memory)
    assert observe()['hosts'][0]['refreshing']
    assert len(jobs) == 1
    drain(jobs)
    assert not observe()['hosts'][0]['refreshing'] and not jobs


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


def test_daily_inventory_and_hourly_failed_host_retry(engine):
    memory, now, jobs, saves = engine
    discover(memory, spec('ssh-host', 'mini')); drain(jobs)
    original = discover(memory, spec('ssh-host', 'mini'))['hosts'][0]['value']
    now[0] += 86399
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
    assert remote['checked'] == 10000 and remote['stale'] and len(jobs) == 1
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
    assert memory.handle('status', {})['protocol'] == 2
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


def test_sqlite_restoration_and_asynchronous_persistence(cache, engine):
    memory, _, jobs, _ = engine
    disk = cache._Persistence(memory)
    discover(memory)
    drain(jobs)
    disk.stop.set()
    disk.run()
    assert not memory.persistence_error
    assert disk.database.stat().st_mode & 0o777 == 0o600
    restored = cache._Memory(clock=lambda: 10001, submit=lambda *args: pytest.fail('restored cache is fresh'))
    reader = cache._Persistence(restored)
    reader.stop.set()
    reader.run()
    disk.database.unlink()
    assert discover(restored)['hosts'][0]['value']['rows'].startswith('sid')


def test_small_artifact_update_only_writes_its_record(cache):
    import zlib
    memory = cache._Memory()
    disk = cache._Persistence(memory)
    identity = list(cache._client_context())
    identity[-1] += '-other'
    other = cache._Memory()
    disk.register(identity, other)
    for engine in (memory, other):
        engine.restore({})
        engine.handle('artifact', {'key': 'large', 'value': 'x' * 100_000})
        engine.handle('artifact', {'key': 'small', 'value': 1})
    disk.stop.set()
    disk.run()
    with sqlite3.connect(disk.database) as db:
        db.executescript("""
            create table updates (namespace text, path text);
            create trigger track_update after update on cache_records begin
                insert into updates values(new.namespace, new.path);
            end;
        """)
    other.handle('artifact', {'key': 'small', 'value': 2})
    disk.run()
    with sqlite3.connect(disk.database) as db:
        updates = db.execute('select namespace,path from updates').fetchall()
        assert [(ns, json.loads(path)) for ns, path in updates] == [(cache._digest(identity), ['artifacts', 'small'])]
        row = db.execute('select payload from cache_records where namespace=? and path=?', updates[0]).fetchone()
        assert json.loads(zlib.decompress(row[0])) == 2


def test_one_catalog_change_keeps_other_conversation_fragments(cache, engine):
    import zlib
    memory, _, jobs, _ = engine
    disk = cache._Persistence(memory)
    records = [{'sid': str(i), 'title': 'retained ' + str(i)} for i in range(4)]
    memory.collect = lambda spec: {'rows': '', 'catalog': {'conversations': list(records)}}
    discover(memory)
    drain(jobs)
    disk.stop.set()
    disk.run()
    with sqlite3.connect(disk.database) as db:
        assert db.execute('select count(*) from cache_fragments').fetchone()[0] == 4
        db.executescript("""
            create table edits (action text, payload blob);
            create trigger inserted after insert on cache_fragments begin
                insert into edits values('insert', new.payload);
            end;
            create trigger updated after update on cache_fragments begin
                insert into edits values('update', new.payload);
            end;
            create trigger removed after delete on cache_fragments begin
                insert into edits values('delete', old.payload);
            end;
        """)
    records[1] = {'sid': '1', 'title': 'updated'}
    records.append({'sid': '4', 'title': 'new'})
    memory.handle('discover', {'hosts': [spec()], 'refresh': True})
    drain(jobs)
    disk.run()
    with sqlite3.connect(disk.database) as db:
        edits = sorted((action, json.loads(zlib.decompress(raw))['title'])
                       for action, raw in db.execute('select action,payload from edits'))
        assert edits == [('delete', 'retained 1'), ('insert', 'new'), ('insert', 'updated')]
        saved = disk._load(db, disk.namespace, cache._client_context())
        value = next(iter(saved['entries'].values()))['value']
        assert value['catalog']['conversations'] == records


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
        server.configure()
        server.memories[tuple(cache._client_context())] = memory
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            assert cache.request('status')['protocol'] == 2
            view = cache.request('discover', {'hosts': [spec()]})
            assert view['hosts'][0]['value'] is None
            drain(jobs)
            assert cache.request('discover', {'hosts': [spec()]})['hosts'][0]['value']['rows']
            with pytest.raises(ValueError): cache._wire('bad', {})
        finally:
            server.shutdown(); thread.join(2)


@pytest.mark.parametrize('operation', ['discover', 'hydrate', 'live-view', 'inventory-changed', 'status',
                                       'service-status', 'doctor', 'provider', 'event', 'activity',
                                       'export', 'reset-host', 'catalog-changed', 'forget'])
def test_client_unavailable_is_bounded_no_scanner_fallback(cache, monkeypatch, operation):
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: pytest.fail('client spawned a process'))
    monkeypatch.setattr(cache.time, 'sleep', lambda *_: pytest.fail('client waited for startup'))
    result = cache.request(operation)
    assert result['unavailable'] and not result['loading'] and 'stopped' in result['message']
    assert list(cache._runtime().iterdir()) == []

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


def test_revised_client_preserves_service_and_explicit_stop_flushes(cache, monkeypatch):
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
        contender.start(); contender.join(4)
        assert not contender.is_alive()
        cache._wire('artifact', {'key': 'restart', 'value': {'saved': True}})
        monkeypatch.setattr(cache, '_revision', lambda: ['new-source'])
        assert cache._wire('status', {})['protocol'] == 2
        signals[cache.signal.SIGTERM]()
        thread.join(3)
        assert not thread.is_alive()
        assert not (cache._runtime() / 'service.sock').exists()
        with sqlite3.connect(cache._database()) as db:
            disk = cache._Persistence(cache._Memory())
            saved = disk._load(db, disk.namespace, cache._client_context())
        assert saved['artifacts']['restart'] == {'saved': True}
    finally:
        if thread.is_alive():
            signals[cache.signal.SIGTERM]()
            thread.join(3)


def test_stop_terminates_worker_process_group_and_rejects_new_work(cache, tmp_path):
    workers = cache._Workers()
    child_ready = tmp_path / 'child.pid'
    script = ("import pathlib,subprocess,sys,time; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)")
    result = []
    def collect():
        result.append(workers.run([sys.executable, '-c', script, str(child_ready)],
                                  input='', env=cache._service_env(), timeout=10))
    thread = threading.Thread(target=collect)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not child_ready.exists():
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        workers.close()
        # The grandchild inherited both pipe descriptors. communicate() can
        # return only after the grandchild exits or closes its descriptors.
        thread.join(2)
        assert not thread.is_alive() and result[0].returncode < 0
        with pytest.raises(OSError, match='stopping'):
            workers.run([sys.executable, '-c', 'pass'], input='', env={}, timeout=1)
    finally:
        workers.close()
        thread.join(3)


def test_standalone_artifact_miss_never_spawns_service(cache, monkeypatch):
    monkeypatch.setattr(cache, '_wire', lambda *a: (_ for _ in ()).throw(OSError('offline')))
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: pytest.fail('artifact lookup started daemon'))
    assert cache.request('artifact', {'key': 'missing'}) == {'value': None}


@pytest.mark.parametrize('operation', ['event', 'forget'])
def test_failed_target_cooldown_survives_invalidation_and_context_changes(engine, operation):
    memory, now, jobs, _ = engine
    remote = spec('u@mini', 'mini')
    discover(memory, remote); drain(jobs)
    original = discover(memory, remote)['hosts'][0]['value']
    memory.handle('event', {'targets': ['u@mini']})
    memory.collect = lambda _: (_ for _ in ()).throw(OSError('offline'))
    discover(memory, remote); drain(jobs)
    if operation == 'event':
        memory.handle(operation, {'targets': ['u@mini']})
    else:
        memory.handle(operation, {'target': 'u@mini', 'workspace': '/unrelated'})
    alternate = spec('u@mini', 'alias', context={'repos': {'new': '/new'}})
    for _ in range(3):
        hosts = discover(memory, remote, alternate)['hosts']
        assert all(h['error'] == 'offline' and h['retry_at'] == now[0] + 3600 for h in hosts)
        assert not any(h['refreshing'] for h in hosts) and not jobs
        assert hosts[0]['value'] == original
    now[0] += 3599
    discover(memory, alternate); assert not jobs
    now[0] += 1
    discover(memory, alternate, remote); assert len(jobs) == 1
    memory.collect = lambda _: {'rows': '', 'resume': []}
    drain(jobs)
    assert not memory.failures
    assert all(not e.get('error') for e in memory.entries.values())


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('invalidate_while_loading', [False, True])
def test_failed_target_cooldown_survives_restore(cache, engine, legacy, invalidate_while_loading):
    memory, now, jobs, _ = engine
    memory.collect = lambda _: (_ for _ in ()).throw(OSError('offline'))
    discover(memory, spec('remote')); drain(jobs)
    saved = memory.snapshot()
    if legacy:
        saved.pop('failures')
        saved['entries']['malformed'] = None
    restored_jobs = []
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *job: restored_jobs.append(job))
    if invalidate_while_loading:
        restored.handle('event', {'targets': ['remote']})
    restored.restore(saved)
    host = discover(restored, spec('remote', 'renamed', context={'repos': {'a': '/a'}}))['hosts'][0]
    assert host['error'] == 'offline' and host['retry_at'] == now[0] + 3600
    assert not restored_jobs


def test_reset_host_only_clears_target_preserves_rows_and_does_not_collect(engine):
    memory, now, jobs, saves = engine
    discover(memory, spec('remote'), spec('other')); drain(jobs); drain(jobs)
    memory.handle('event', {'targets': ['remote', 'other']})
    memory.collect = lambda _: (_ for _ in ()).throw(OSError('offline'))
    discover(memory, spec('remote'), spec('other')); drain(jobs); drain(jobs)
    alternate = spec('remote', 'alias', context={'repos': {'a': '/a'}})
    discover(memory, alternate)
    before = memory.snapshot()
    assert memory.handle('reset-host', {'target': 'remote'}) == {'reset': 'remote'}
    assert set(memory.failures) == {'other'} and not jobs and saves
    for key, entry in memory.entries.items():
        if entry['spec']['target'] == 'remote':
            assert entry['checked'] == entry['retry'] == 0 and not entry['error']
            assert entry.get('value') == before['entries'][key].get('value')
        else:
            assert entry == before['entries'][key]
    hosts = discover(memory, spec('remote'), alternate, spec('other'))['hosts']
    assert len(jobs) == 1 and hosts[0]['refreshing'] and hosts[1]['refreshing']
    assert not hosts[2]['refreshing'] and hosts[2]['error'] == 'offline'


@pytest.mark.parametrize('operation', ['event', 'forget', 'reset-host'])
@pytest.mark.parametrize('failure', [False, True])
def test_inflight_failure_cooldown_respects_only_explicit_reset(engine, operation, failure):
    memory, now, jobs, _ = engine
    def collect(_):
        if failure:
            raise OSError('offline')
        return {'rows': 'stale', 'resume': []}
    memory.collect = collect
    discover(memory, spec('remote'))
    payload = {'targets': ['remote']} if operation == 'event' else {'target': 'remote', 'workspace': '/work'}
    memory.handle(operation, payload)
    discover(memory, spec('remote'))
    assert len(jobs) == 1  # Reset does not start a concurrent collector.
    drain(jobs)
    host = discover(memory, spec('remote'))['hosts'][0]
    assert host['value'] is None
    cooling = failure and operation != 'reset-host'
    assert bool(host['error']) == cooling
    assert len(jobs) == (0 if cooling else 1)


def test_reset_host_validation_and_restoration_gate(engine):
    memory, _, jobs, _ = engine
    for payload in ({}, {'target': ''}, {'target': []}):
        with pytest.raises(ValueError, match='invalid'):
            memory.handle('reset-host', payload)
    memory.loading = True
    with pytest.raises(ValueError, match='loading'):
        memory.handle('reset-host', {'target': 'remote'})
    assert not jobs and not memory.reset_epochs


def test_restore_ignores_invalid_failure_metadata(cache):
    memory = cache._Memory(clock=lambda: 10000, submit=lambda *a: None)
    memory.restore({'failures': {'wrong': [], 'empty': {'retry_at': 10100, 'error': ''},
        'infinite': {'retry_at': float('inf'), 'error': 'bad'},
        'future': {'retry_at': 13601, 'error': 'bad'}, 'text': {'retry_at': 'soon', 'error': 'bad'}}})
    assert not memory.failures
    with pytest.raises(ValueError, match='invalid'):
        memory.restore({'failures': []})


def test_invalid_disk_snapshot_is_rebuilt_from_ram(cache):
    memory = cache._Memory(submit=lambda *a: None)
    disk = cache._Persistence(memory)
    with disk._open() as db:
        db.execute('insert into snapshots values (?,?,?)',
                   (cache._digest(cache._identity()), cache.VERSION, '[]'))
    disk.stop.set()
    disk.run()
    assert not memory.loading
    memory.handle('artifact', {'key': 'valid', 'value': True})
    disk.run()
    restored = cache._Memory()
    reader = cache._Persistence(restored)
    reader.stop.set()
    reader.run()
    assert restored.artifacts == {'valid': True} and not restored.persistence_error
    with pytest.raises(ValueError, match='invalid discovery snapshot'):
        memory.restore({'entries': []})

@pytest.mark.parametrize('replacement', [False, True])
def test_lost_socket_owner_exits_without_removing_replacement(cache, monkeypatch, replacement):
    signals = {}
    monkeypatch.setattr(cache.signal, 'signal', lambda number, handler: signals.setdefault(number, handler))
    monkeypatch.setattr(cache.os, 'umask', lambda value: None)
    thread = threading.Thread(target=cache._serve)
    thread.start()
    address = cache._runtime() / 'service.sock'
    try:
        deadline = time.monotonic() + 3
        while True:
            try:
                if not cache._wire('status', {})['loading']: break
            except (OSError, ValueError): pass
            assert time.monotonic() < deadline
            time.sleep(.01)
        cache._wire('artifact', {'key': 'retained', 'value': {'saved': True}})
        address.unlink()
        if replacement: address.write_text('replacement must survive old cleanup')
        thread.join(3)
        assert not thread.is_alive(), 'orphaned listener must release its lifetime lock'
        if replacement: assert address.read_text() == 'replacement must survive old cleanup'
        with sqlite3.connect(cache._database()) as db:
            disk = cache._Persistence(cache._Memory())
            saved = disk._load(db, disk.namespace, cache._client_context())
        assert saved['artifacts']['retained'] == {'saved': True}
    finally:
        if thread.is_alive():
            signals[cache.signal.SIGTERM]()
            thread.join(3)


def test_distinct_checkouts_share_owner_and_isolate_tmux_namespaces(cache, monkeypatch, tmp_path):
    with cache._Server(str(cache._runtime() / 'service.sock'), cache._Handler) as server:
        server.configure()
        writer = threading.Thread(target=server.disk.run)
        server_thread = threading.Thread(target=server.serve_forever)
        writer.start()
        server_thread.start()
        def ready():
            deadline = time.monotonic() + 4
            while cache._wire('status', {})['loading']:
                assert time.monotonic() < deadline
                time.sleep(.01)
        try:
            ready()
            status = cache._wire('service-status', {})
            endpoint = cache._real_runtime()
            cache._wire('artifact', {'key': 'one', 'value': 'original'})
            with monkeypatch.context() as env:
                env.setattr(cache, 'SOURCE', tmp_path / 'worktree')
                assert cache._real_runtime() == endpoint
                assert cache._wire('artifact', {'key': 'one'})['value'] == 'original'
                env.setenv('TMUX', '/tmp/synthetic-alternate-tmux,0,0')
                assert cache._real_runtime() == endpoint
                assert cache._wire('service-status', {})['pid'] == status['pid']
                ready()
                assert cache._wire('artifact', {'key': 'one'})['value'] is None
                cache._wire('artifact', {'key': 'one', 'value': 'other'})
            assert cache._wire('artifact', {'key': 'one'})['value'] == 'original'
            assert len(server.memories) == 2
        finally:
            server.shutdown()
            server_thread.join(3)
            server.disk.stop.set()
            server.disk.wake.set()
            writer.join(3)

def test_socket_loss_requires_explicit_restart_and_retains_snapshot(tmp_path, monkeypatch):
    import shutil
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'CODEX_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    module = load_script(REPO_ROOT / 'libexec/t_session_cache.py', 'cache_recovery_process_test')
    address = module._runtime() / 'service.sock'
    assert module.request('status')['unavailable']
    children = []
    def start():
        process = subprocess.Popen([sys.executable, module.__file__, 'serve'], env=module._service_env())
        children.append(process)
        deadline = time.monotonic() + 5
        while True:
            status = module.request('status')
            if not status.get('unavailable') and not status['loading']:
                return process
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
    try:
        first = start()
        module._wire('artifact', {'key': 'retained', 'value': {'saved': True}})
        address.unlink()
        assert module.request('status')['unavailable']
        assert first.wait(timeout=5) == 0
        for _ in range(3):
            assert module.request('discover')['unavailable']
            assert not address.exists()
        start()
        assert module._wire('artifact', {'key': 'retained'})['value'] == {'saved': True}
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        shutil.rmtree(module._runtime(), ignore_errors=True)

@pytest.mark.parametrize('selection', ['default', 'legacy', 'bridge'])
def test_identity_uses_effective_local_config(cache, monkeypatch, tmp_path, selection):
    monkeypatch.delenv('T_LOCAL_RC', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    root = tmp_path / 'config/t'
    root.mkdir(parents=True)
    local = root / 'local.zsh'
    if selection == 'legacy':
        local = tmp_path / '.zshrc.local'
    if selection == 'bridge':
        local = tmp_path / 'custom local.zsh'
        (root / 'config.sh').write_text('T_LOCAL_RC=' + __import__('shlex').quote(str(local)) + '\n')
    local.touch()
    implicit = cache._identity()
    monkeypatch.setenv('T_LOCAL_RC', str(local))
    assert cache._identity() == implicit
    alias = tmp_path / 'alias'; alias.symlink_to(local)
    monkeypatch.setenv('T_LOCAL_RC', str(alias))
    assert cache._identity() == implicit
    assert str(alias) in [identity[7] for identity in cache._compatible_identities()]
    assert '' not in [identity[7] for identity in cache._compatible_identities()]
    monkeypatch.setenv('T_LOCAL_RC', str(tmp_path / 'different'))
    assert cache._identity() != implicit
    assert '' not in [identity[7] for identity in cache._compatible_identities()]
    monkeypatch.setattr(cache, 'SOURCE', tmp_path / 'other-source')
    assert cache._identity()[0] != implicit[0]


def test_detached_workers_pin_relative_config_and_safe_legacy_identity(cache, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('T_LOCAL_RC', 'relative.zsh')
    identity = cache._identity()
    env = cache._service_env()
    assert env['T_LOCAL_RC'] == str(tmp_path / 'relative.zsh')
    legacy = list(identity); legacy[7] = 'relative.zsh'
    monkeypatch.setenv('T_LOCAL_RC', env['T_LOCAL_RC'])
    monkeypatch.chdir('/')
    assert cache._identity() == identity
    assert legacy not in list(cache._compatible_identities())


@pytest.mark.parametrize('target', ['local', 'mini'])
def test_demand_and_explicit_refresh_have_separate_collector_semantics(engine, cache, target):
    memory, now, jobs, _ = engine
    host = spec(target)
    discover(memory, host); drain(jobs)
    requests = []
    value = next(iter(memory.entries.values()))['value']
    memory.collect = lambda request: (requests.append(request.copy()), value.copy())[1]
    now[0] += cache.TTL + 1
    discover(memory, host); drain(jobs)
    assert not requests[-1].get('refresh')
    memory.failures[target] = dict(retry_at=now[0] + 60, error='unreachable', kind='transport')
    memory.handle('discover', {'hosts': [host], 'refresh': True, 'observe': True})
    assert not jobs
    memory.handle('discover', {'hosts': [host], 'refresh': True})
    assert len(jobs) == 1
    drain(jobs)
    assert requests[-1]['refresh'] and requests[-1]['reconcile']
    now[0] += cache.REMOTE_INTERVAL
    memory.tick()
    if target == 'local':
        assert not jobs
    else:
        assert len(jobs) == 1
        drain(jobs)
        assert not requests[-1].get('refresh')


def test_refresh_requested_while_loading_is_retained_separately_from_demand(engine, cache):
    memory, now, jobs, _ = engine
    discover(memory); drain(jobs)
    requests = []
    restored = cache._Memory(clock=lambda: now[0], submit=lambda *job: jobs.append(job),
                             collect=lambda request: (requests.append(request), dict(rows='new', resume=[], catalog={}))[1])
    restored.handle('discover', {'hosts': [spec()], 'fields': ['rows'], 'refresh': True})
    restored.handle('discover', {'hosts': [spec()], 'fields': ['catalog']})
    restored.restore(memory.snapshot())
    assert len(jobs) == 1
    drain(jobs)
    assert requests[0]['refresh'] and requests[0]['reconcile']


def test_migration_notifications_reach_all_contexts_sharing_history(cache):
    with cache._Server(str(cache._runtime() / 'service.sock'), cache._Handler) as server:
        server.configure()
        identities = [cache._client_context()]
        identities.append([*identities[0][:-1], '/tmp/another-tmux.sock'])
        other = list(identities[0])
        other[3] += '-different'
        identities.append(other)
        for identity in identities:
            server.dispatch(identity, 'status', {})
            memory = server.memories[tuple(identity)]
            memory.restore({})
            memory.entries['local'] = dict(spec=spec(), checked=1, retry=0, refreshing=False,
                                           last_used=memory.clock(), value={})
        server.dispatch(identities[0], 'catalog-changed', {'refresh_active': True})
        for identity, expected in zip(identities, (True, True, False)):
            entry = server.memories[tuple(identity)].entries['local']
            assert bool(entry.get('dirty')) is expected
            assert bool(entry.get('requested')) is expected


def test_migration_revision_recovers_after_loading_and_only_once(cache):
    with cache._Server(str(cache._runtime() / 'service.sock'), cache._Handler) as server:
        server.configure()
        identity = cache._client_context()
        memory = server.memory
        root = cache._migration_root(identity)
        root.mkdir(parents=True)
        (root / 'revision').touch()
        server.observe_migrations(memory)
        assert memory.migration_revision == 0  # Loading cannot consume the receipt.
        key = cache._digest({k: v for k, v in spec().items() if k != 'host'})
        memory.restore({'entries': {key: dict(spec=spec(), checked=1, retry=0,
                                             refreshing=False, value={})}})
        server.dispatch(identity, 'discover', {'hosts': [spec()], 'fields': [], 'observe': True})
        entry = memory.entries[key]
        assert entry['dirty']
        entry['dirty'] = False
        server.dispatch(identity, 'discover', {'hosts': [spec()], 'fields': [], 'observe': True})
        assert not entry['dirty']


def test_migration_priority_uses_requested_history_context(cache):
    with cache._Server(str(cache._runtime() / 'service.sock'), cache._Handler) as server:
        server.configure()
        identity = cache._client_context()
        identity[3] += '-alternate'
        root = cache._migration_root(identity)
        root.mkdir(parents=True)
        (root / 'worker.lock').touch()
        assert server.dispatch(identity, 'migration-priority', {'aliases': ['api'], 'slot': '7'})['scheduled']
        priority = json.loads((root / 'priority.json').read_text())
        assert priority['aliases'] == ['api'] and priority['slot'] == '7'
        assert not (cache._migration_root(server.default_identity) / 'priority.json').exists()
        with pytest.raises(ValueError, match='invalid migration priority'):
            server.dispatch(identity, 'migration-priority', {'aliases': 'api'})


def test_migration_receipt_reads_coalesce_across_contexts(cache, monkeypatch):
    reads = []
    monkeypatch.setattr(cache, '_migration_revision', lambda root: (reads.append(root), 42)[1])
    with cache._Server(str(cache._runtime() / 'service.sock'), cache._Handler) as server:
        server.configure()
        first = cache._client_context()
        second = [*first[:-1], '/tmp/second-tmux.sock']
        for identity in (first, second):
            server.dispatch(identity, 'status', {})
            server.memories[tuple(identity)].restore({})
        for _ in range(5):
            for memory in server.memories.values():
                server.observe_migrations(memory)
        assert reads == [server.memory.migration_root]
        assert all(memory.migration_revision == 42 for memory in server.memories.values())
        server.migration_receipts[server.memory.migration_root] = (time.monotonic() - 2, 42)
        server.observe_migrations(server.memory)
        assert reads == [server.memory.migration_root] * 2
