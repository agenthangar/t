"""The coordinator is exercised with synthetic providers, never the developer's runtimes."""
import io
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import load_script


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv('T_LOCAL_RUNTIME_ROOT', str(tmp_path / 'runtime'))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    m = load_script(Path(__file__).parents[1] / 'libexec/t_local_manager.py', 'manager_test')
    m.private_directory(m.root())
    monkeypatch.setattr(m, 'process_identity', lambda pid: 'owned-process' if pid else None)
    monkeypatch.setattr(m, 'owner_identity', lambda *args: ('owner', True))
    return m


class Providers:
    def __init__(self, root, settings, persist):
        self.instances = {}
        self.external = []
        self.ollama_base = None
        self.settings = settings
        self.loaded = self.unloaded = 0
        self.fail_stop = False
    def get(self, chosen, pinned=()):
        key = chosen['model']
        if key not in self.instances:
            self.loaded += 1
            self.instances[key] = dict(provider=chosen['provider'], model=key, revision='abc', context=32768,
                alias='owned-' + key, base='http://127.0.0.1:9', owned=True, estimate=123, identity=[key], last_used=time.time())
        return self.instances[key]
    def present(self, instance): return True
    def unload(self, instance):
        if self.fail_stop: raise ValueError('cannot stop')
        self.unloaded += 1
        self.instances.pop(instance['model'], None)
    def expire(self, pinned): pass
    def close(self): self.instances.clear()


def chosen(model='model'):
    return dict(model=model, provider='ollama', context_tokens=32768, timeout_seconds=300, queue_timeout_seconds=300)


def register(engine, model='model', kind='worker'):
    return engine.control(dict(action='register', chosen=chosen(model), pid=os.getpid(), kind=kind))


def handler():
    return SimpleNamespace(command='POST', wfile=io.BytesIO(), send_response=lambda *a: None,
                           send_header=lambda *a: None, end_headers=lambda: None)


def test_model_test_reserves_exact_owners_and_cancels_only_reviewed_work(manager):
    m=manager;engine=m.Engine(Providers);engine.needs_recovery=False
    first=register(engine);second=register(engine,'another')
    jobs=[engine.submit(engine.clients[c['id']],'/v1/responses',{},handler()) for c in (first,second)]
    engine.active=jobs[0];jobs[0]['state']='loading'
    assert engine.control({'action':'test-preflight'})['activities'][first['id']]=='Loading'
    engine.active=None;jobs[0]['state']='queued'
    snapshot=engine.control({'action':'test-preflight'})
    token=engine.control(dict(action='test-reserve',generation=snapshot['generation'],owners=snapshot['owners'],pid=os.getpid()))['token']
    with pytest.raises(ValueError,match='changed'):
        engine.control(dict(action='test-reserve',generation=snapshot['generation'],owners=[],pid=os.getpid()))
    with pytest.raises(ValueError,match='expired'):
        engine.control(dict(action='test-ready',token='wrong'))
    assert engine.control(dict(action='test-interrupt',token=token))['interrupt_requested']
    assert all(job['state']=='cancelled' for job in jobs)
    assert all(c.get('interrupted') for c in engine.clients.values())
    with pytest.raises(ValueError,match='interrupted'):
        engine.submit(engine.clients[first['id']],'/v1/responses',{},handler())
    assert engine.control(dict(action='test-ready',token=token))['ready']
    test=engine.control(dict(action='register',chosen=chosen('test'),pid=os.getpid(),kind='verification',test_token=token))
    assert engine.test_lease['client']==test['id']
    with pytest.raises(ValueError,match='already in use'):
        engine.control(dict(action='register',chosen=chosen(),pid=os.getpid(),test_token=token))
    assert engine.control(dict(action='test-heartbeat',token=token))['reserved']
    assert engine.control(dict(action='test-release',token=token))['released']
    assert engine.test_lease is None
    engine.close()


def test_model_test_changed_owner_guard_pin_release_and_busy_state(manager,monkeypatch):
    m=manager;engine=m.Engine(Providers);lease=register(engine,kind='session');client=engine.clients[lease['id']]
    client['pinned_alias']='pinned';engine.responses['response']={'client':client['id'],'alias':'pinned'}
    snapshot=engine.control({'action':'test-preflight'})
    with pytest.raises(ValueError,match='changed'):
        engine.control(dict(action='test-reserve',generation='old',owners=snapshot['owners'],pid=os.getpid()))
    monkeypatch.setattr(m,'process_identity',lambda pid:None)
    with pytest.raises(ValueError,match='identity'):
        engine.control(dict(action='test-reserve',generation=engine.generation,owners=snapshot['owners'],pid=os.getpid()))
    monkeypatch.setattr(m,'process_identity',lambda pid:'identity')
    token=engine.control(dict(action='test-reserve',generation=engine.generation,owners=snapshot['owners'],pid=os.getpid()))['token']
    other=register(engine);job=engine.submit(engine.clients[other['id']],'/v1/responses',{},handler())
    with pytest.raises(ValueError,match='changed'):engine.control(dict(action='test-interrupt',token=token))
    engine.cancel(job)
    engine.control(dict(action='test-interrupt',token=token))
    assert not client.get('interrupted')  # Native session remains usable after its turn interruption.
    engine.active=job
    assert not engine.control(dict(action='test-ready',token=token))['ready']
    engine.active=None
    assert engine.control(dict(action='test-ready',token=token))['ready']
    assert not engine.responses and not client.get('pinned_alias')
    with pytest.raises(ValueError,match='Unknown model'):engine.control(dict(action='test-unknown',token=token))
    engine.control(dict(action='test-release',token=token));engine.close()


def test_model_test_reservation_stops_other_admission(manager,monkeypatch):
    m=manager;engine=m.Engine(Providers);engine.needs_recovery=False
    token=engine.control(dict(action='test-reserve',generation=engine.generation,owners=[],pid=os.getpid()))['token']
    other=register(engine);job=engine.submit(engine.clients[other['id']],'/v1/responses',{},handler())
    engine.thread.start()
    try:
        assert not job['done'].wait(.05)
        assert job['state']=='queued' and not engine.providers.loaded
    finally:
        engine.control(dict(action='test-release',token=token));engine.close()


def test_cancellation_during_model_load_never_starts_generation(manager,monkeypatch):
    engine=manager.Engine(Providers);engine.needs_recovery=False;lease=register(engine)
    job=engine.submit(engine.clients[lease['id']],'/v1/responses',{},handler())
    get=engine.providers.get
    def cancel_during_load(*args):
        engine.cancel(job)
        return get(*args)
    monkeypatch.setattr(engine.providers,'get',cancel_during_load)
    monkeypatch.setattr(engine,'forward',lambda *a:pytest.fail('cancelled load started inference'))
    engine.thread.start()
    try:
        assert job['done'].wait(2) and job['state']=='cancelled'
    finally:engine.close()


def test_test_client_cannot_register_before_release_or_after_expiry(manager):
    engine=manager.Engine(Providers)
    token=engine.control(dict(action='test-reserve',generation=engine.generation,owners=[],pid=os.getpid()))['token']
    with pytest.raises(ValueError,match='expired or already'):
        engine.control(dict(action='register',chosen=chosen(),pid=os.getpid(),test_token=token))
    engine.test_lease['heartbeat']=0
    engine.maintenance.start()
    deadline=time.monotonic()+3
    while engine.test_lease and time.monotonic()<deadline:time.sleep(.02)
    assert engine.test_lease is None
    engine.close()


def test_settings_private_files_and_stopped_status(manager):
    m = manager
    assert m.status()['manager'] == 'stopped'
    assert not (m.root() / 'control.sock').exists()
    m.write_json(m.root() / 'settings.json', m.DEFAULTS)
    assert m.settings()['memory_percent'] == 80
    for value in ([], {'extra': 1}, {'memory_percent': 90}, {'memory_percent': True}, {'idle_seconds': 0},
                  {'providers': {}}, {'providers': {'lmstudio': {}, 'ollama': {}}}):
        with pytest.raises(ValueError): m.settings(value)
    path = m.root() / 'bad'
    path.write_text('{}')
    path.chmod(0o644)
    with pytest.raises(ValueError): m.read_json(path)
    m.root().chmod(0o755)
    with pytest.raises(ValueError): m.private_directory(m.root())


def test_round_robin_fifo_and_blocked_owner(manager):
    scheduler = manager.Scheduler()
    def job(owner, number, **kw): return dict(owner=owner, client=owner, id=number, state='queued', **kw)
    a1, a2, b1, b2 = job('a', 1), job('a', 2), job('b', 3), job('b', 4)
    for item in (a1,a2,b1,b2): scheduler.add(item)
    assert [scheduler.take(10)['id'] for _ in range(4)] == [1,3,2,4]
    assert scheduler.take(10) is None
    scheduler.add(job('a', 5, retry_at=20)); scheduler.add(job('b', 6))
    assert scheduler.take(10)['id'] == 6
    assert scheduler.take(10) is None
    assert scheduler.take(20)['id'] == 5
    scheduler.add(dict(job('a', 7), state='cancelled')); scheduler.add(job('b', 8))
    assert scheduler.take(20, 'b')['id'] == 8


def automatic(engine):
    return engine.control(dict(action='register', chosen=dict(chosen(), mode='automatic'),
                               pid=os.getpid(), kind='worker'))


def test_automatic_admission_is_atomic_and_read_only(manager):
    engine = manager.Engine(Providers)
    assert engine.control({'action': 'availability'})['state'] == 'available'
    assert not engine.clients and not engine.jobs and engine.providers.loaded == 0
    gate, results = threading.Barrier(3), []
    def admit():
        gate.wait()
        results.append(automatic(engine))
    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads: thread.start()
    gate.wait()
    for thread in threads: thread.join(2)
    assert sum('id' in r for r in results) == 1
    refused = next(r for r in results if 'id' not in r)
    assert refused['admitted'] is False and refused['availability']['state'] == 'busy'
    assert refused['availability']['blockers'][0]['owner_verified']
    engine.close()


def test_automatic_admission_respects_reviewed_model_test_reservation(manager):
    engine = manager.Engine(Providers)
    lease = engine.control(dict(action='test-reserve', generation=engine.generation, owners=[], pid=os.getpid()))
    view = engine.control({'action': 'availability'})
    assert view['state'] == 'busy' and view['retry_after_seconds'] is None
    assert 'model test' in view['reason']
    assert automatic(engine)['admitted'] is False
    assert not engine.clients and engine.providers.loaded == 0
    engine.control(dict(action='test-release', token=lease['token']))
    assert 'id' in automatic(engine)
    engine.close()


def test_automatic_rechecks_other_requests_and_preserves_manual_queue(manager):
    engine = manager.Engine(Providers)
    first = automatic(engine)
    local = engine.clients[first['id']]
    manual = register(engine, model='other', kind='session')
    other = engine.clients[manual['id']]
    blocking = engine.submit(other, '/v1/responses', {}, handler())
    with pytest.raises(ValueError, match='busy'):
        engine.submit(local, '/v1/responses', {}, handler())
    assert engine.client_status(local)['outcome'] == 'busy'
    assert blocking['state'] == 'queued' and not blocking['cancel'].is_set()
    assert len(engine.jobs) == 1
    assert engine.submit(other, '/v1/responses', {}, handler())['state'] == 'queued'
    engine.close()


def test_availability_blockers_idle_models_and_release_evidence(manager, monkeypatch):
    engine = manager.Engine(Providers)
    engine.providers.get(chosen())
    assert engine.availability()['state'] == 'available'
    lease = register(engine)
    job = engine.submit(engine.clients[lease['id']], '/v1/responses', {}, handler())
    engine.active = job
    job.update(state='running', started=time.time()-1000)
    assert engine.availability()['retry_after_seconds'] is None
    job['backend_stopped'] = True
    assert engine.availability()['retry_after_seconds'] == 15
    engine.finish(job, 'completed'); engine.active = None
    monkeypatch.setattr(manager, 'legacy_busy', lambda: True)
    assert 'older worker' in engine.availability()['reason']
    monkeypatch.setattr(manager, 'legacy_busy', lambda: False)
    engine.providers.external = [{'memory_bytes': None}]
    assert engine.availability()['state'] == 'unknown'
    engine.providers.external = []
    engine.blocked = 'recovery required'
    assert engine.availability()['reason'] == 'recovery required'
    engine.close()


def test_automatic_memory_contention_returns_busy_without_retry(manager, monkeypatch):
    engine = manager.Engine(Providers)
    lease = automatic(engine)
    class WaitingForMemory(ValueError): pass
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise WaitingForMemory('memory busy')
    monkeypatch.setattr(engine.providers, 'get', unavailable)
    job = engine.submit(engine.clients[lease['id']], '/v1/responses', {}, handler())
    second = engine.submit(engine.clients[lease['id']], '/v1/responses', {}, handler())
    engine.thread.start()
    assert job['done'].wait(2) and job['state'] == 'busy'
    assert second['done'].wait(2) and second['state'] == 'busy'
    assert len(calls) == 1
    assert engine.client_status(engine.clients[lease['id']])['outcome'] == 'busy'
    assert engine.providers.loaded == 0
    engine.close()


def test_queue_loading_and_execution_are_separate_wall_time(manager, monkeypatch):
    engine = manager.Engine(Providers)
    lease = register(engine); client = engine.clients[lease['id']]
    job = engine.submit(client, '/v1/responses', {}, handler())
    job.update(created=10, started=20, execution_started=50, finished=70, state='completed')
    view = engine.client_status(client)
    assert (view['queue_seconds'], view['loading_seconds'], view['wait_seconds']) == (10, 30, 40)
    row = engine.snapshot()['requests'][0]
    assert (row['queue_seconds'], row['loading_seconds'], row['execution_seconds']) == (10, 30, 20)
    job.pop('execution_started'); job.pop('finished'); job['state'] = 'loading'
    monkeypatch.setattr(manager.time, 'time', lambda: 40)
    view = engine.client_status(client)
    assert view['wait_seconds'] == 30 and view['loading_seconds'] == 20
    assert engine.snapshot()['requests'][0]['execution_seconds'] == 0
    engine.close()


def test_preflight_never_starts_runtime_and_handles_stale_state(manager, monkeypatch):
    monkeypatch.setattr(manager, 'ensure', lambda: pytest.fail('preflight started manager'))
    monkeypatch.setattr(manager, 'status', lambda: {'manager': 'stopped'})
    assert manager.availability()['state'] == 'available'
    monkeypatch.setattr(manager, 'legacy_busy', lambda: True)
    assert manager.availability()['state'] == 'busy'
    monkeypatch.setattr(manager, 'status', lambda: {'manager': 'stale'})
    assert manager.availability()['state'] == 'unknown'
    monkeypatch.setattr(manager, 'status', lambda: {'manager': 'running', 'protocol': manager.VERSION})
    monkeypatch.setattr(manager, 'rpc', lambda action: {'state': 'busy', 'reason': 'known owner'})
    assert manager.availability()['reason'] == 'known owner'


@pytest.mark.parametrize('clients,requests,expected', [([], [], 'available'),
    ([{'id': 'live'}], [], 'unknown'), ([], [{'state': 'running'}], 'unknown')])
def test_older_manager_preflight_only_allows_idle_upgrade(manager, monkeypatch, clients, requests, expected):
    monkeypatch.setattr(manager, 'status', lambda: {'manager': 'running', 'protocol': 2,
                                                  'clients': clients, 'requests': requests})
    monkeypatch.setattr(manager, 'rpc', lambda *a, **k: pytest.fail('preflight restarted old manager'))
    assert manager.availability()['state'] == expected


def test_legacy_probe_does_not_create_or_release_another_lock(manager):
    import fcntl
    path = Path(os.environ['XDG_STATE_HOME']) / 't/local-worker/worker.lock'
    assert not manager.legacy_busy() and not path.exists()
    path.parent.mkdir(parents=True)
    with path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert manager.legacy_busy()
        # A second check still sees the owner: the probe did not release it.
        assert manager.legacy_busy()
    assert not manager.legacy_busy()


def test_client_admission_rejection_and_idempotent_enter(manager, monkeypatch):
    monkeypatch.setattr(manager, 'ensure', lambda: None)
    monkeypatch.setattr(manager, 'rpc', lambda *a, **k: {'admitted': False, 'availability': {'state': 'busy', 'reason': 'owner'}})
    client = manager.Client(dict(chosen(), mode='automatic'), 'worker')
    with pytest.raises(manager.AdmissionDenied) as error:
        client.__enter__()
    assert error.value.availability['state'] == 'busy' and client.info == {}
    assert not hasattr(client, 'thread')
    client.info = {'id': 'already-admitted'}
    assert client.__enter__() is client


def test_release_wait_rpc_is_capped_by_remaining_deadline(manager, monkeypatch):
    monkeypatch.setattr(manager.time, 'monotonic', lambda: 10)
    calls = []
    monkeypatch.setattr(manager, 'rpc', lambda action, **kwargs: calls.append(kwargs) or {'state': 'available'})
    assert manager.availability(deadline=10.25)['state'] == 'available'
    assert calls == [{'_timeout': .25}]
    assert manager.availability(deadline=10)['state'] == 'unknown'
    assert len(calls) == 1


def test_stopped_manager_with_unrecovered_instances_is_unknown(manager, monkeypatch):
    monkeypatch.setattr(manager, 'status', lambda: {'manager': 'stopped'})
    manager.write_json(manager.root()/'leases.json', {'instances': [{'alias': 'old-owner'}]})
    assert manager.availability()['state'] == 'unknown'
    assert 'recovery' in manager.availability()['reason']


def test_upgrade_wait_cannot_replace_a_manager_that_has_not_stopped(manager, monkeypatch):
    clock = [0]
    monkeypatch.setattr(manager.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(manager.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    calls = []
    def rpc(action):
        calls.append(action)
        return {'protocol': 2, 'clients': []}
    monkeypatch.setattr(manager, 'rpc', rpc)
    monkeypatch.setattr(manager.subprocess, 'Popen', lambda *a, **k: pytest.fail('replaced live manager'))
    with pytest.raises(ValueError, match='still shutting down'):
        manager.ensure()
    assert calls.count('shutdown') == 1 and 8 <= clock[0] < 8.2


def test_maintenance_reports_execution_deadline_without_cancelling_other_client(manager, monkeypatch):
    engine = manager.Engine(Providers)
    lease = register(engine)
    job = engine.submit(engine.clients[lease['id']], '/v1/responses', {}, handler())
    other = register(engine, model='other')
    queued = engine.submit(engine.clients[other['id']], '/v1/responses', {}, handler())
    job.update(state='running', started=10, execution_started=20)
    engine.active = job
    monkeypatch.setattr(manager.time, 'time', lambda: 400)
    class Once:
        calls = 0
        def wait(self, seconds):
            self.calls += 1
            return self.calls > 1
        def set(self): pass
    engine.stopping = Once()
    engine.maintain()
    assert job['cancel'].is_set() and job['timeout_phase'] == 'execution'
    assert not queued['cancel'].is_set()
    engine.finish(job, 'cancelled')
    assert job['state'] == 'timed_out'
    assert engine.client_status(engine.clients[lease['id']])['timeout_phase'] == 'execution'
    engine.active = None
    engine.close()


@pytest.mark.parametrize('kind,outcome,stopped', [('worker', 'busy', True), ('session', 'busy', False), ('worker', 'failed', False)])
def test_worker_busy_heartbeat_does_not_change_native_sessions(manager, monkeypatch, kind, outcome, stopped):
    client = manager.Client(chosen(), kind)
    class Once:
        count = 0
        def wait(self, seconds):
            self.count += 1
            return self.count > 1
    client.closed = Once()
    monkeypatch.setattr(client, 'call', lambda action: {'state': 'working', 'outcome': outcome})
    client.heartbeat()
    assert client.cancel_event.is_set() is stopped


def test_recovered_provider_failure_does_not_poison_later_requests(manager):
    engine = manager.Engine(Providers)
    lease = register(engine)
    client = engine.clients[lease['id']]
    first = engine.submit(client, '/v1/responses', {}, handler())
    engine.finish(first, 'failed', 'Provider rejected the request (HTTP 500)')
    assert engine.client_status(client)['outcome'] == 'failed'
    second = engine.submit(client, '/v1/responses', {}, handler())
    assert engine.client_status(client)['outcome'] is None
    engine.finish(second, 'completed')
    assert engine.client_status(client)['outcome'] is None
    # A budget failure is terminal for a worker even if its native client retries.
    third = engine.submit(client, '/v1/responses', {}, handler())
    third['timeout_phase'] = 'queue'
    engine.finish(third, 'timed_out', 'Queue budget expired')
    with pytest.raises(ValueError, match='task has ended'):
        engine.submit(client, '/v1/responses', {}, handler())
    view = engine.client_status(client)
    assert view['outcome'] == 'timed_out' and view['timeout_phase'] == 'queue'
    engine.close()


@pytest.mark.parametrize('reason', ['legacy', 'loading'])
def test_automatic_late_blockers_never_start_generation(manager, monkeypatch, reason):
    engine = manager.Engine(Providers)
    lease = automatic(engine)
    job = engine.submit(engine.clients[lease['id']], '/v1/responses', {}, handler())
    monkeypatch.setattr(engine, 'forward', lambda *a: pytest.fail('blocked request generated'))
    if reason == 'legacy':
        monkeypatch.setattr(engine, 'legacy_lock', lambda: False)
    else:
        get = engine.providers.get
        def expired(*args):
            job['deadline'] = time.time() - 1
            return get(*args)
        monkeypatch.setattr(engine.providers, 'get', expired)
    engine.thread.start()
    assert job['done'].wait(2)
    assert job['state'] == ('busy' if reason == 'legacy' else 'timed_out')
    if reason == 'loading': assert job['timeout_phase'] == 'loading'
    engine.close()


def test_clients_ownership_history_and_queue_cancellation(manager):
    engine = manager.Engine(Providers)
    lease = register(engine)
    client = engine.clients[lease['id']]
    assert lease['token'] not in json.dumps(engine.snapshot())
    with pytest.raises(ValueError): engine.control({'action':'heartbeat','client':lease['id'],'token':'wrong'})
    assert engine.control(dict(action='heartbeat',client=lease['id'],token=lease['token']))['state'] == 'working'
    job = engine.submit(client, '/v1/responses', {'input':'PRIVATE PROMPT'}, handler())
    assert 'PRIVATE PROMPT' not in json.dumps(engine.snapshot())
    engine.control(dict(action='cancel',request=job['id']))
    assert job['state'] == 'cancelled' and 'payload' not in job
    with pytest.raises(ValueError): engine.control(dict(action='cancel',request='wrong'))
    with pytest.raises(ValueError): engine.submit(client,'/v1/responses',{'previous_response_id':'foreign'},handler())
    engine.responses['same']={'client':client['id'],'alias':'owned'}
    with pytest.raises(ValueError, match='stateless'): engine.submit(client,'/v1/responses',{'previous_response_id':'same'},handler())
    engine.control(dict(action='close',client=lease['id'],token=lease['token']))
    assert not engine.clients and not engine.responses
    with pytest.raises(ValueError): engine.control({'action':'bad'})


def test_stream_tracker_keeps_only_ids_and_usage(manager):
    engine = manager.Engine(Providers)
    lease = register(engine)
    job = {'client':lease['id']}
    instance = {'provider':'lmstudio','alias':'exact'}
    tracker = manager.ResponseTracker(engine,job,instance,True,True)
    tracker.feed(b'data: {"response":{"id":"response-1", "output":"private", "usage":{"output_tokens":12}}}\n\n')
    tracker.feed(b'data: [DONE]\n')
    tracker.finish()
    assert engine.responses == {'response-1':{'client':lease['id'],'alias':'exact'}}
    assert job['tokens']==12 and 'private' not in json.dumps(engine.responses)
    tracker = manager.ResponseTracker(engine,job,instance,True)
    tracker.feed(b'{\n"id":"response-2",\n"usage":{"completion_tokens":4}\n}')
    tracker.finish()
    assert 'response-2' in engine.responses and job['tokens']==4
    tracker.accept([]); tracker.accept({'response':None}); tracker.accept({'usage':[]})
    tracker.feed(b'x'*(manager.LIMIT+1)); assert not tracker.buffer


def test_instance_reuse_and_active_cancellation(manager,monkeypatch):
    engine=manager.Engine(Providers)
    engine.port=123
    engine.needs_recovery=False
    leases=[register(engine) for _ in range(2)]
    observed=[]
    def forward(job,instance):
        observed.append(instance['alias'])
        job['headers_sent']=True
    monkeypatch.setattr(engine,'forward',forward)
    engine.thread.start()
    jobs=[engine.submit(engine.clients[c['id']],'/v1/responses',{'input':'hello'},handler()) for c in leases]
    for j in jobs: assert j['done'].wait(3)
    assert all(j['state']=='completed' for j in jobs)
    assert engine.providers.loaded==1 and observed==['owned-model']*2
    engine.close()


def test_unconfirmed_cancellation_blocks_next_generation(manager,monkeypatch):
    engine=manager.Engine(Providers); engine.needs_recovery=False
    lease=register(engine)
    client=engine.clients[lease['id']]
    def fail(job,instance):
        job['sent']=True
        engine.providers.fail_stop=True
        raise OSError('body must not leak')
    monkeypatch.setattr(engine,'forward',fail)
    engine.thread.start()
    first=engine.submit(client,'/v1/responses',{},handler())
    assert first['done'].wait(3) and first['state']=='failed'
    assert engine.blocked and 'body must not leak' not in json.dumps(engine.snapshot())
    with pytest.raises(ValueError): engine.submit(client,'/v1/responses',{},handler())
    engine.providers.fail_stop=False
    engine.close()


def test_memory_wait_does_not_block_other_owner(manager,monkeypatch):
    class WaitingForMemory(ValueError): pass
    engine=manager.Engine(Providers);engine.needs_recovery=False
    real=engine.providers.get
    def get(choice,pinned):
        if choice['model']=='large': raise WaitingForMemory('needs memory')
        return real(choice,pinned)
    monkeypatch.setattr(engine.providers,'get',get)
    monkeypatch.setattr(engine,'forward',lambda *a:None)
    a,b=register(engine,'large'),register(engine,'small')
    engine.clients[b['id']]['owner']='different'
    engine.thread.start()
    large=engine.submit(engine.clients[a['id']],'/v1/responses',{},handler())
    small=engine.submit(engine.clients[b['id']],'/v1/responses',{},handler())
    assert small['done'].wait(3) and small['state']=='completed'
    assert large['state']=='waiting_for_memory' and not large.get('started')
    engine.close()


def test_settings_release_and_crash_recovery(manager):
    engine=manager.Engine(Providers)
    lease=register(engine)
    with pytest.raises(ValueError): engine.control(dict(action='settings',value=manager.DEFAULTS))
    engine.control(dict(action='close',client=lease['id'],token=lease['token']))
    assert engine.control(dict(action='settings',value=manager.DEFAULTS))['saved']
    instance=engine.providers.get(chosen())
    engine.active={}
    # Empty dict is not active; an actual job object prevents release.
    engine.active={'id':'active'}
    with pytest.raises(ValueError): engine.control(dict(action='release',alias=instance['alias']))
    engine.active=None
    engine.control(dict(action='release',alias=instance['alias']))
    with pytest.raises(ValueError): engine.control(dict(action='release',alias=instance['alias']))
    manager.write_json(manager.root()/'leases.json',{'instances':[instance]})
    engine.recover()
    assert not engine.needs_recovery
    engine.providers.fail_stop=True
    manager.write_json(manager.root()/'leases.json',{'instances':[instance]})
    engine.recover()
    assert engine.blocked


def test_http_proxy_auth_streaming_history_and_serialization(manager,monkeypatch):
    """Real loopback HTTP on both sides; only the provider behavior is synthetic."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import http.client
    entered=threading.Event();release=threading.Event();received=[]
    class Backend(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(payload)
            if payload.get('input')=='slow':entered.set();release.wait(3)
            data=b'data: {"response":{"id":"response-1","usage":{"output_tokens":7}}}\n\ndata: [DONE]\n\n'
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
    bt=threading.Thread(target=backend.serve_forever);bt.start()
    engine=manager.Engine(Providers);engine.needs_recovery=False
    baseget=engine.providers.get
    def get(*a):
        item=baseget(*a);item['base']=f'http://127.0.0.1:{backend.server_port}';return item
    monkeypatch.setattr(engine.providers,'get',get)
    proxy=ThreadingHTTPServer(('127.0.0.1',0),manager.ProxyHandler);proxy.engine=engine
    engine.port=proxy.server_port
    pt=threading.Thread(target=proxy.serve_forever);pt.start();engine.start()
    lease=engine.control(dict(action='register',chosen=dict(chosen(),provider='lmstudio'),pid=os.getpid()))
    other=engine.control(dict(action='register',chosen=dict(chosen(),provider='lmstudio'),pid=os.getpid()))
    def call(path,payload=None,token=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=5)
        conn.request('POST' if payload is not None else 'GET','/'+(token or lease['token'])+path,
                     json.dumps(payload) if payload is not None else None,headers or {})
        response=conn.getresponse();result=(response.status,response.read());conn.close();return result
    try:
        assert call('/v1/models')[0]==200
        assert call('/api/tags')[0]==200
        assert call('/v1/models',token='bad')[0]==403
        assert call('/v1/models',headers={'Origin':'http://hostile.test'})[0]==403
        assert call('/bad',{})[0]==400
        assert call('/v1/responses',{'model':'other'})[0]==400
        assert call('/v1/responses',{'previous_response_id':'foreign'})[0]==400
        result=[]
        first=threading.Thread(target=lambda:result.append(call('/v1/responses',{'input':'slow'})))
        second=threading.Thread(target=lambda:result.append(call('/v1/responses',{'input':'second'},other['token'])))
        first.start();assert entered.wait(2);second.start()
        deadline=time.monotonic()+2
        while len(engine.jobs)<2 and time.monotonic()<deadline:time.sleep(.01)
        assert len(received)==1
        view=engine.snapshot();assert sum(x['state']=='running' for x in view['requests'])==1
        assert any(x['queue_position']==1 for x in view['requests'])
        release.set();first.join(3);second.join(3)
        assert len(result)==2 and all(x[0]==200 for x in result)
        assert all(x['model']=='owned-model' for x in received)
        assert engine.responses['response-1']['client']==other['id']
        assert call('/v1/responses',{'previous_response_id':'response-1'})[0]==400
        assert call('/v1/responses',{'previous_response_id':'response-1'},other['token'])[0]==200
        assert all('input' not in row for row in engine.snapshot()['requests'])
    finally:
        release.set();engine.close();proxy.shutdown();backend.shutdown();pt.join();bt.join()
        proxy.server_close();backend.server_close()


def test_cancel_shared_instance_drains_and_preserves_history(manager,monkeypatch):
    engine=manager.Engine(Providers);engine.needs_recovery=False
    a,b=register(engine),register(engine)
    engine.clients[a['id']]['pinned_alias']='owned-model'
    entered=threading.Event();release=threading.Event()
    def forward(job,item):
        assert not job['interruptible']
        job['sent']=True;entered.set();release.wait(3);job['backend_stopped']=True
    monkeypatch.setattr(engine,'forward',forward)
    engine.thread.start()
    job=engine.submit(engine.clients[b['id']],'/v1/responses',{},handler())
    assert entered.wait(2)
    engine.control(dict(action='cancel',request=job['id']))
    assert job['state']=='cancelling' and not job['done'].is_set()
    release.set();assert job['done'].wait(2)
    assert job['state']=='cancelled' and engine.providers.unloaded==0
    assert engine.clients[a['id']]['pinned_alias']=='owned-model'
    engine.close()


def test_queue_expiration_and_heartbeat_during_slow_maintenance(manager,monkeypatch):
    engine=manager.Engine(Providers);engine.needs_recovery=False
    lease=register(engine);client=engine.clients[lease['id']]
    entered=threading.Event();release=threading.Event()
    def expire(pins):entered.set();release.wait(3)
    monkeypatch.setattr(engine.providers,'expire',expire)
    job=engine.submit(client,'/v1/responses',{},handler());job['deadline']=0
    engine.maintenance.start()
    try:
        assert entered.wait(2) and job['done'].is_set() and job['state']=='timed_out'
        tick=time.monotonic()
        engine.control(dict(action='heartbeat',client=lease['id'],token=lease['token']))
        assert time.monotonic()-tick<.5
        with pytest.raises(ValueError):engine.control(dict(action='settings',value=manager.DEFAULTS))
    finally:release.set();engine.close()


def test_recovery_retains_uncertain_borrowed_journal(manager,monkeypatch):
    engine=manager.Engine(Providers)
    item=dict(engine.providers.get(chosen()),owned=False)
    manager.write_json(manager.root()/'leases.json',{'instances':[item], 'ollama_process':{'pid':99,'identity':None,'base':item['base']}})
    monkeypatch.setattr(manager.os,'killpg',lambda *a:pytest.fail('unverified process killed'))
    engine.recover()
    assert engine.blocked and engine.recovery_pending==[item]
    assert manager.read_json(manager.root()/'leases.json')['instances'][0]==item
    monkeypatch.setattr(engine.providers,'present',lambda x:False)
    engine.maintenance.start()
    deadline=time.monotonic()+3
    while engine.blocked and time.monotonic()<deadline:time.sleep(.02)
    assert not engine.blocked and not engine.recovery_pending
    engine.close()


def test_control_socket_and_client_heartbeat(manager,monkeypatch):
    engine=manager.Engine(Providers);engine.port=1234
    # Use the short autouse namespace: Darwin Unix socket paths are limited to 104 bytes.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='t-control-',dir='/tmp') as directory:
        monkeypatch.setenv('T_LOCAL_RUNTIME_ROOT',directory)
        with manager.ControlServer(str(manager.root()/'control.sock'),manager.ControlHandler) as server:
            server.engine=engine
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                assert manager.ensure()['protocol']==manager.VERSION
                with manager.Client(chosen()) as client:
                    deadline=time.monotonic()+2
                    while client.last['state']!='working' and time.monotonic()<deadline:time.sleep(.01)
                    assert client.base.startswith('http://127.0.0.1:1234/')
                    assert client.call('client')['owner_verified']
                    assert client.waited()==0
                    with pytest.raises(ValueError):manager.rpc('shutdown')
                assert not engine.clients
                assert manager.rpc('shutdown')['stopping']
                with pytest.raises(ValueError):manager.rpc('unknown')
            finally:server.shutdown();thread.join()


def test_serve_lifecycle_private_socket_no_replay(manager,monkeypatch):
    import tempfile
    with tempfile.TemporaryDirectory(prefix='t-serve-',dir='/tmp') as directory:
        monkeypatch.setenv('T_LOCAL_RUNTIME_ROOT',directory)
        engine=manager.Engine(Providers)
        monkeypatch.setattr(manager,'Engine',lambda:engine)
        monkeypatch.setattr(manager.signal,'signal',lambda *a:None)
        outcomes=[]
        def client():
            try:
                deadline=time.monotonic()+3
                while not (manager.root()/'control.sock').exists() and time.monotonic()<deadline:time.sleep(.01)
                view=manager.rpc('status');outcomes.append(view)
                assert (manager.root()/'control.sock').stat().st_mode & 0o777 == 0o600
            finally:
                manager.rpc('shutdown')
        thread=threading.Thread(target=client);thread.start()
        manager.serve();thread.join()
        assert outcomes[0]['manager']=='running'
        assert not (manager.root()/'control.sock').exists()
        assert not engine.jobs


def test_startup_retry_protocol_and_stale_status(manager,monkeypatch):
    m=manager;calls=[]
    def rpc(action,**kw):
        calls.append(action)
        if len(calls)<3:raise FileNotFoundError()
        return {'protocol':m.VERSION}
    monkeypatch.setattr(m,'rpc',rpc)
    monkeypatch.setattr(m.subprocess,'Popen',lambda argv,**kw: calls.append(kw))
    assert m.ensure()['protocol']==m.VERSION
    assert calls[1]['env']['T_LOCAL_RUNTIME_ROOT']==str(m.root())
    monkeypatch.setattr(m,'rpc',lambda *a,**k:{'protocol':-1})
    with pytest.raises(ValueError,match='protocol'):m.ensure()
    monkeypatch.setattr(m,'rpc',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    m.write_json(m.root()/'status.json',{'requests':[{'state':'running'},{'state':'completed'}]})
    view=m.status();assert view['manager']=='stale' and view['requests'][0]['state']=='unknown'
    assert view['requests'][1]['state']=='completed'


def test_owner_verification_process_identity_and_limits(manager,monkeypatch):
    m=manager
    # Check the real process helper separately from the manager fixture's synthetic identity.
    original=load_script(Path(__file__).parents[1]/'libexec/t_local_manager.py','identity_test')
    assert original.process_identity(os.getpid())
    assert original.process_identity(1) is None and original.process_identity('x') is None
    monkeypatch.setattr(original.subprocess,'check_output',lambda *a,**k:'999999 Mon Nov 1')
    assert original.process_identity(5) is None
    monkeypatch.setattr(original.subprocess,'check_output',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    assert original.process_identity(5) is None
    monkeypatch.setattr(original,'module',lambda n:{'pane_info':lambda *a:('pane','10'),'ancestry':lambda *a:True})
    monkeypatch.setattr(original,'process_identity',lambda pid:'same-start')
    assert original.owner_identity(10,'/tmp/private-tmux','%1')[1]
    assert original.owner_identity(10,'invalid','%1')[1] is False
    engine=m.Engine(Providers);lease=register(engine)
    for _ in range(63):register(engine)
    with pytest.raises(ValueError,match='client limit'):register(engine)
    client=engine.clients[lease['id']]
    for _ in range(128):engine.submit(client,'/v1/responses',{},handler())
    with pytest.raises(ValueError,match='queue is full'):engine.submit(client,'/v1/responses',{},handler())
    engine.close()


def test_overlapping_queue_wait_is_wall_time_not_sum(manager):
    engine=manager.Engine(Providers);lease=register(engine);client=engine.clients[lease['id']]
    a=engine.submit(client,'/v1/responses',{},handler());b=engine.submit(client,'/v1/responses',{},handler())
    a.update(created=10,started=20,execution_started=20);b.update(created=15,started=25,execution_started=25)
    assert engine.client_status(client)['wait_seconds']==15
    engine.close()


def test_cancel_owned_generation_unloads_and_invalidates_reused_alias(manager,monkeypatch):
    engine=manager.Engine(Providers);engine.needs_recovery=False
    lease=register(engine);client=engine.clients[lease['id']]
    entered=threading.Event();release=threading.Event()
    def forward(job,item):
        assert job['interruptible'];job['sent']=True
        engine.responses['old']={'client':client['id'],'alias':item['alias']}
        client['pinned_alias']=item['alias'];entered.set();release.wait(2)
    monkeypatch.setattr(engine,'forward',forward);engine.thread.start()
    job=engine.submit(client,'/v1/responses',{},handler());assert entered.wait(2)
    engine.control(dict(action='cancel',request=job['id']));release.set()
    assert job['done'].wait(2) and job['state']=='cancelled'
    assert engine.providers.unloaded==1 and not engine.responses and 'pinned_alias' not in client
    with pytest.raises(ValueError,match='history'):engine.submit(client,'/v1/responses',{'previous_response_id':'old'},handler())
    engine.close()


@pytest.mark.parametrize('state',['running','waiting_for_memory','waiting_for_legacy'])
def test_client_heartbeat_reports_activity_and_fails_closed(manager,monkeypatch,state):
    updates=[];client=manager.Client(chosen(),activity=SimpleNamespace(update=updates.append))
    class Once:
        count=0
        def wait(self,t):self.count+=1;return self.count>1
        def set(self):pass
    client.closed=Once()
    monkeypatch.setattr(client,'call',lambda action:{'state':state,'wait_seconds':3})
    client.heartbeat();assert updates==[{'running':'inferring','waiting_for_memory':'waiting','waiting_for_legacy':'queued'}[state]]
    assert client.waited()==3
    client.closed=Once();client.cancel_event.set();calls=[]
    monkeypatch.setattr(client,'call',lambda action:calls.append(action))
    client.heartbeat();assert calls==['close']
    client.closed=Once();client.cancel_event.clear()
    monkeypatch.setattr(client,'call',lambda action:(_ for _ in ()).throw(OSError('lost manager')))
    client.heartbeat();assert client.error=='lost manager' and client.cancel_event.is_set()
    client.thread=SimpleNamespace(join=lambda **kw:None);client.__exit__()


@pytest.mark.parametrize('provider,path,interruptible', [('ollama','/api/generate',True),('ollama','/v1/responses',False),('lmstudio','/v1/responses',True)])
def test_forward_cancel_and_provider_error(manager,monkeypatch,provider,path,interruptible):
    engine=manager.Engine(Providers);lease=register(engine);client=engine.clients[lease['id']]
    job=engine.submit(client,path,{'store':True},handler());job['interruptible']=interruptible
    instance=engine.providers.get(dict(chosen(),provider=provider));sent=[]
    class Response:
        status=200;count=0
        def getheader(self,*a):return 'application/json'
        def read1(self,size):
            self.count+=1
            if self.count==1:job['cancel'].set();return b'private response'
            return b''
    response=Response()
    class Connection:
        sock=None
        def request(self,*args):sent.append(args)
        def getresponse(self):return response
        def close(self):pass
    monkeypatch.setattr(manager.http.client,'HTTPConnection',lambda *a,**kw:Connection())
    engine.forward(job,instance)
    assert job['handler'].wfile.getvalue()==b''
    assert bool(job.get('backend_stopped')) is not interruptible
    payload=json.loads(sent[0][2]);assert payload['model']==instance['alias']
    if path.startswith('/api/'):assert payload['options']['num_ctx']==32768 and 'store' not in payload
    elif provider=='ollama':assert payload['store'] is False
    job['cancel'].clear();response.status=500
    with pytest.raises(ValueError,match='HTTP 500'):engine.forward(job,instance)
    job['cancel'].set();before=len(sent);engine.forward(job,instance);assert len(sent)==before
    engine.active=job
    job['connection']=SimpleNamespace(sock=SimpleNamespace(shutdown=lambda *a:(_ for _ in ()).throw(OSError())))
    engine.cancel(job);assert job['state']=='cancelling'
    engine.active=None;engine.close()


def test_legacy_lock_waits_and_measurement_sequence_is_exclusive(manager,monkeypatch):
    engine=manager.Engine(Providers)
    directory=Path(os.environ['XDG_STATE_HOME'])/'t/local-worker';directory.mkdir(parents=True)
    lease=register(engine,kind='measurement');client=engine.clients[lease['id']]
    monkeypatch.setattr(engine,'forward',lambda *a:None)
    with (directory/'worker.lock').open('a') as lock:
        manager.fcntl.flock(lock,manager.fcntl.LOCK_EX|manager.fcntl.LOCK_NB)
        assert not engine.legacy_lock();engine.thread.start()
        job=engine.submit(client,'/v1/responses',{},handler())
        deadline=time.monotonic()+2
        while job['state']=='queued' and time.monotonic()<deadline:time.sleep(.01)
        assert job['state']=='waiting_for_legacy'
    assert job['done'].wait(3) and engine.measurement_owner==client['id']
    engine.control(dict(action='close',client=lease['id'],token=lease['token']))
    assert engine.measurement_owner is None;engine.close()


def test_verified_private_daemon_recovery_and_unverified_client(manager,monkeypatch):
    engine=manager.Engine(Providers);item=engine.providers.get(chosen());calls=[]
    manager.write_json(manager.root()/'leases.json',{'instances':[item],'ollama_process':{'pid':99,'identity':'owned-process','base':item['base']}})
    monkeypatch.setattr(manager.os,'killpg',lambda *args:calls.append(args))
    engine.recover();assert calls==[(99,manager.signal.SIGKILL)]
    monkeypatch.setattr(manager,'process_identity',lambda pid:None)
    with pytest.raises(ValueError,match='identity'):register(engine)
    engine.close()


def test_maintenance_reaps_dead_clients_and_old_metadata(manager,monkeypatch):
    engine=manager.Engine(Providers);lease=register(engine);client=engine.clients[lease['id']]
    job=engine.submit(client,'/v1/responses',{},handler());engine.finish(job,'completed');job['finished']=0
    client['heartbeat']=0;engine.legacy_lock()
    engine.blocked='previously uncertain';engine.blocked_instance={'alias':'gone'}
    monkeypatch.setattr(engine.providers,'present',lambda item:False)
    engine.maintenance.start();deadline=time.monotonic()+3
    while engine.clients and time.monotonic()<deadline:time.sleep(.02)
    assert not engine.clients and not engine.jobs and not engine.blocked and engine.legacy is None
    engine.close()


def test_manager_startup_timeout_never_returns_direct_endpoint(manager,monkeypatch):
    monkeypatch.setattr(manager,'rpc',lambda *a,**kw:(_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(manager.subprocess,'Popen',lambda *a,**kw:None)
    ticks=iter([0,9]);monkeypatch.setattr(manager.time,'monotonic',lambda:next(ticks))
    with pytest.raises(ValueError,match='did not start'):manager.ensure()


def test_manager_refuses_unsafe_socket_and_duplicate_daemon(manager):
    path=manager.root()/'control.sock';path.write_text('not a socket')
    with pytest.raises(ValueError,match='Unsafe'):manager.serve()
    with (manager.root()/'manager.lock').open('a') as lock:
        manager.fcntl.flock(lock,manager.fcntl.LOCK_EX|manager.fcntl.LOCK_NB)
        assert manager.serve() is None


def test_recovery_tolerates_exited_verified_daemon(manager,monkeypatch):
    engine=manager.Engine(Providers);item=engine.providers.get(chosen())
    manager.write_json(manager.root()/'leases.json',{'instances':[item],'ollama_process':{'pid':99,'identity':'owned-process','base':item['base']}})
    monkeypatch.setattr(manager.os,'killpg',lambda *a:(_ for _ in ()).throw(ProcessLookupError()))
    engine.recover();assert not engine.recovery_pending and not engine.blocked
    engine.close()


def test_only_generation_updates_activity_not_polling(manager):
    engine=manager.Engine(Providers);lease=register(engine)
    tracker=manager.ResponseTracker(engine,{'client':lease['id']},{'provider':'lmstudio','alias':'a'},False,True)
    for value in ({'type':'response.created'}, {'type':'response.output_text.delta','delta':''},
                  {'choices':None}, {'choices':[None,{'delta':None}]}, {'usage':{'output_tokens':2}}):
        tracker.accept(value)
    client=engine.clients[lease['id']]
    assert engine.client_status(client)['generation_events']==0
    for value in ({'type':'response.reasoning_text.delta','delta':'private'},
                  {'choices':[{'delta':{'tool_calls':[{'id':'tool'}]}}]},
                  {'message':{'thinking':'private'}}):
        tracker.accept(value)
    assert engine.client_status(client)['generation_events']==3
    assert 'private' not in json.dumps(engine.snapshot())


def test_idle_old_manager_upgrades_without_interrupting_clients(manager,monkeypatch):
    calls=[]
    def rpc(action,**kw):
        calls.append(action)
        if calls==['status']:return {'protocol':1,'clients':[]}
        if action=='shutdown':return {'stopping':True}
        if len(calls)==3:raise FileNotFoundError()
        return {'protocol':manager.VERSION}
    monkeypatch.setattr(manager,'rpc',rpc)
    monkeypatch.setattr(manager.subprocess,'Popen',lambda *a,**kw:None)
    assert manager.ensure()['protocol']==manager.VERSION
    assert calls==['status','shutdown','status','status']
    monkeypatch.setattr(manager,'rpc',lambda *a,**kw:{'protocol':1,'clients':[{'id':'active'}]})
    with pytest.raises(ValueError,match='sessions finish'):manager.ensure()
