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
    a.update(created=10,started=20);b.update(created=15,started=25)
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
