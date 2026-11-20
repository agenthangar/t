"""Memory recovery uses simulated processes and isolated coordinator state."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import shlex
import subprocess
import sys
import tempfile

import pytest
from conftest import load_script
from test_local_manager import manager, Providers, register, handler
from test_local_runtime import runtime, provider
from test_local_runtime_ui import UI

ROOT = Path(__file__).parents[1]


@pytest.fixture
def memory(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path/'state'))
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.delenv('TMUX_PANE', raising=False)
    return load_script(ROOT/'libexec/t_memory_recovery.py', 'memory_recovery_test')


def test_live_shortfall_uses_physical_and_budget_constraints(runtime, provider, monkeypatch):
    p, _, _, _, _ = provider
    monkeypatch.setattr(p, 'observe', lambda: None)
    monkeypatch.setattr(runtime, 'available_memory', lambda: 17.8*runtime.GIB)
    with pytest.raises(runtime.WaitingForMemory) as exc:
        p.room({'estimate': 10*runtime.GIB})
    assert exc.value.memory['shortfall_bytes'] == pytest.approx(5*runtime.GIB, abs=1)
    assert exc.value.memory['available_bytes'] == 17.8*runtime.GIB
    assert exc.value.memory['resource'] == 'physical'
    p.machine['accelerator']='NVIDIA';p.machine['vram_bytes']=32*runtime.GIB
    monkeypatch.setattr(runtime, 'available_memory', lambda: 100*runtime.GIB)
    monkeypatch.setattr(runtime, 'gpu_available', lambda: 5*runtime.GIB)
    with pytest.raises(runtime.WaitingForMemory) as exc:
        p.room({'estimate': 10*runtime.GIB})
    assert exc.value.memory['resource']=='gpu'


def test_two_waiting_clients_overlap_and_share_one_load(manager, runtime, monkeypatch):
    engine=manager.Engine(Providers);engine.needs_recovery=False
    available=[False];real=engine.providers.get
    def get(choice,pinned):
        if not available[0]:
            raise runtime.WaitingForMemory('Physical memory headroom', shortfall_bytes=5*runtime.GIB)
        return real(choice,pinned)
    monkeypatch.setattr(engine.providers,'get',get)
    monkeypatch.setattr(engine,'forward',lambda *args:None)
    leases=[register(engine,kind='session') for _ in range(2)]
    for index,lease in enumerate(leases):engine.clients[lease['id']]['owner']=str(index)
    jobs=[engine.submit(engine.clients[c['id']],'/v1/responses',{},handler()) for c in leases]
    engine.thread.start()
    try:
        until=time.monotonic()+3
        while not all(j.get('memory') for j in jobs) and time.monotonic()<until:time.sleep(.02)
        assert all(j.get('memory',{}).get('shortfall_bytes')==5*runtime.GIB for j in jobs)
        auxiliary=engine.submit(engine.clients[leases[0]['id']],'/v1/responses',{},handler())
        assert not engine.snapshot()['models'] and engine.providers.loaded==0
        for c in leases:
            status=engine.client_status(engine.clients[c['id']])
            assert status['state']=='waiting_for_memory' and status['memory']['shortfall_bytes']==5*runtime.GIB
        assert engine.client_status(engine.clients[leases[0]['id']])['request_id']==jobs[0]['id']
        available[0]=True
        assert all(j['done'].wait(4) for j in [*jobs,auxiliary])
        assert all(j['state']=='completed' for j in [*jobs,auxiliary])
        assert engine.providers.loaded==1 and all(not j.get('memory') for j in jobs)
    finally:engine.close()


def test_timeout_retains_memory_even_after_auxiliary_finishes(manager):
    engine=manager.Engine(Providers);engine.needs_recovery=False
    lease=register(engine,kind='session');client=engine.clients[lease['id']]
    job=engine.submit(client,'/v1/responses',{},handler())
    auxiliary=engine.submit(client,'/v1/responses',{},handler())
    job.update(memory={'reason':'Physical memory headroom','shortfall_bytes':5*1024**3},timeout_phase='queue')
    engine.finish(job,'cancelled')
    assert engine.client_status(client)['memory']==job['memory']
    engine.finish(auxiliary,'completed')
    view=engine.client_status(client)
    assert view['state']=='failed' and view['memory']['shortfall_bytes']==5*1024**3
    assert 'Physical memory headroom' in view['failure_reason']
    assert view['deadline']==job['deadline']
    retry=engine.submit(client,'/v1/responses',{},handler())
    engine.finish(retry,'completed')
    assert engine.client_status(client)['memory'] is None
    engine.close()


def test_only_idle_owned_unpinned_models_are_released(runtime, provider, monkeypatch):
    p, loaded, _, _, _ = provider
    chosen={'provider':'lmstudio','model':'artifact','context_tokens':32768}
    base=p.get(chosen)
    idle=dict(base,model='idle',alias='idle',identity=('idle',),last_used=0)
    pinned=dict(base,model='pinned',alias='pinned',identity=('pinned',),last_used=1)
    external=dict(base,model='external',alias='external',identity=('external',),owned=False,last_used=-1)
    p.instances={x['identity']:x for x in (idle,pinned,external)}
    released=[]
    def unload(row):released.append(row['alias']);p.instances.pop(row['identity'])
    monkeypatch.setattr(p,'unload',unload)
    def room(description):
        if not released:raise runtime.WaitingForMemory('waiting')
    monkeypatch.setattr(p,'room',room)
    # Borrow detection must not return the previous loaded fixture.
    loaded.clear()
    p.get(chosen, {'pinned'})
    assert released==['idle']
    assert [x['model'] for x in p.released]==['idle']


def test_app_inventory_excludes_terminals_services_and_foreign_users(memory, monkeypatch):
    m=memory;monkeypatch.setattr(m.sys,'platform','darwin');uid=os.getuid()
    table={10:dict(pid=10,parent=1,uid=uid,bytes=100,identity='a'),
           11:dict(pid=11,parent=10,uid=uid,bytes=200,identity='b'),
           12:dict(pid=12,parent=1,uid=uid+1,bytes=900,identity='c'),
           13:dict(pid=13,parent=1,uid=uid,bytes=1000,identity='d')}
    monkeypatch.setattr(m,'processes',lambda:table)
    apps=[dict(pid=10,name='Editor',bundle='sample.editor'),dict(pid=12,name='Other user',bundle='sample.other'),
          dict(pid=13,name='Terminal',bundle='com.apple.Terminal')]
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:json.dumps(apps))
    result=m.running_apps()
    assert len(result)==1 and result[0]['estimated_bytes']==300
    assert 'activationPolicy!==0' in m.INVENTORY


def test_quit_revalidates_pid_and_never_force_kills(memory,monkeypatch):
    m=memory;app=dict(pid=33,name='Editor',bundle='sample.editor',identity='start')
    monkeypatch.setattr(m,'running_apps',lambda:[dict(app,identity='reused')])
    calls=[];monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:calls.append(a))
    assert m.quit_app(app)=='No longer eligible' and not calls
    monkeypatch.setattr(m,'running_apps',lambda:[app]);monkeypatch.setattr(m,'processes',lambda:{})
    assert m.quit_app(app)=='Quit'
    assert calls[0][0][-1]=='33' and 'forceTerminate' not in m.QUIT
    monkeypatch.setattr(m,'processes',lambda:{33:dict(identity='start')})
    clock=iter([0,0,3]);monkeypatch.setattr(m.time,'monotonic',lambda:next(clock));monkeypatch.setattr(m.time,'sleep',lambda _:None)
    assert m.quit_app(app)=='Still open'


def test_reviewed_quits_remeasure_and_do_not_retry_expired_requests(memory,monkeypatch):
    m=memory;apps=[dict(pid=i,name=n,estimated_bytes=9*m.GIB) for i,n in [(1,'Editor'),(2,'Simulator')]]
    monkeypatch.setattr(m,'running_apps',lambda:apps)
    calls=[];monkeypatch.setattr(m,'quit_app',lambda a:calls.append(a['name']) or ('Still open' if a['pid']==1 else 'Quit'))
    monkeypatch.setattr(m,'helper',lambda _: {'available_memory':lambda:calls.append('measure') or 2*m.GIB})
    state=dict(memory={'reason':'headroom','shortfall_bytes':5*m.GIB},state='timed_out',deadline=1)
    ui=UI('apps',['1','2'],'quit','back')
    m.free_memory(ui,lambda:state,lambda:{'models':[]})
    assert calls==['Editor','measure','Simulator','measure']
    review=next(p for p in ui.pages if p[0]=='Quit selected apps?')
    assert review[2]['context'][:2]==['Editor','Simulator']
    assert 'marked' not in ui.pages[1][2]
    assert 'Still open' in ' '.join(ui.pages[-1][2]['context'])
    assert 'expired' in ' '.join(ui.pages[-1][2]['context'])
    assert state['state']=='timed_out'


def test_process_parser_and_unsupported_host(memory,monkeypatch):
    m=memory
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'12 1 501 24 Fri Nov 20 10:00:00 2026\nbad row\n')
    assert m.processes()[12]['bytes']==24*1024
    monkeypatch.setattr(m.sys,'platform','linux')
    with pytest.raises(ValueError,match='macOS'):m.running_apps()
    assert m.details({'created':time.time()-10,'deadline':time.time()+15,'memory':{'resource':'gpu','shortfall_bytes':1024**3}})[0]=='Waiting for GPU memory · 1.0 GiB short'


def test_free_memory_empty_cancel_and_external_provider(memory,monkeypatch):
    m=memory;calls=[]
    apps=[dict(pid=1,name='Editor',estimated_bytes=m.GIB)]
    monkeypatch.setattr(m,'running_apps',lambda:apps)
    monkeypatch.setattr(m,'helper',lambda _: {'provider_info':lambda p:{'name':'Provider'},'open_desktop':lambda p:calls.append(p),'available_memory':lambda:None})
    view=lambda:{'models':[{'provider':'ollama','owned':False}]}
    ui=UI('ollama','apps',None,'apps',[],'apps',['1'],'back','back')
    m.free_memory(ui,lambda:{},view)
    assert calls==['ollama']
    def unavailable():raise ValueError('unavailable')
    monkeypatch.setattr(m,'running_apps',unavailable)
    ui=UI('back');m.free_memory(ui,lambda:{},view)
    assert 'unavailable' in ui.pages[0][2]['context']


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12)])
def test_waiting_controls_shared_layout(memory,t_mod,monkeypatch,width,height):
    from test_tui import make_ui
    import select
    monkeypatch.setattr(select, 'select', lambda *a: ([True], [], []))
    module=load_script(ROOT/'libexec/t_session_models.py','session_memory_layout')
    monkeypatch.setattr(module,'helper',lambda _:vars(memory))
    ui=make_ui(t_mod,['j','j','j','enter'],width,height)
    view=dict(verification='Verified on this Mac at 32K',request={'memory':{'reason':'headroom','shortfall_bytes':5*memory.GIB},'state':'waiting_for_memory'},can_switch=True)
    module.waiting_menu(ui,lambda:view,lambda _:None)
    assert ui.frames
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line,width) for line in frame)<=height
        assert any('▸' in line for line in frame)
        assert all(t_mod._vis_len(line)<=width for line in frame)


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12)])
def test_waiting_controls_isolated_tmux(tmp_path,width,height):
    script=tmp_path/'screen.py'
    script.write_text(f'''import runpy
from pathlib import Path
m=runpy.run_path({str(ROOT/'libexec/t_session_models.py')!r})
off=runpy.run_path({str(ROOT/'libexec/t_offline.py')!r})
memory=runpy.run_path({str(ROOT/'libexec/t_memory_recovery.py')!r})
memory['free_memory'].__globals__['running_apps']=lambda:[]
memory['free_memory'].__globals__['helper']=lambda n:{{'status':lambda:{{'models':[]}}}}
m['waiting_menu'].__globals__['helper']=lambda n:memory
view={{'verification':'Verified on this Mac at 32K','can_switch':True,'context_tokens':32768,'cwd':'/sample','generation':1,'request':{{'memory':{{'reason':'headroom','shortfall_bytes':5*1024**3}},'state':'waiting_for_memory'}}}}
with off['recovery_ui']() as ui:
 m['waiting_menu'](ui,lambda:view,lambda s:None)
Path({str(tmp_path/'done')!r}).write_text('canceled')
''')
    with tempfile.TemporaryDirectory(prefix='t-memory-ui-') as directory:
        socket=str(Path(directory)/'tmux.sock')
        env={k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_') and k not in ('TMUX','TMUX_PANE')}
        env.update(HOME=str(tmp_path),XDG_STATE_HOME=str(tmp_path/'state'),XDG_CONFIG_HOME=str(tmp_path/'config'),TERM='xterm-256color')
        def tmux(*args):return subprocess.run(['tmux','-S',socket,*args],env=env,text=True,capture_output=True,timeout=5)
        def capture(needle):
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                screen=tmux('capture-pane','-p','-t','sample').stdout
                if needle in screen:return screen
                time.sleep(.03)
            pytest.fail('Missing '+needle+' in '+screen)
        try:
            result=tmux('-f','/dev/null','new-session','-d','-s','sample','-x',str(width),'-y',str(height),shlex.join([sys.executable,str(script)]))
            assert result.returncode==0,result.stderr
            screen=capture('Free memory')
            (tmp_path/f'waiting-{width}x{height}.txt').write_text(screen)
            assert '▸' in screen
            assert 'Verified' in screen and '5.0 GiB short' in screen
            tmux('send-keys','-t','sample','Enter');screen=capture('Remeasure')
            assert 'Back' in screen
            tmux('send-keys','-t','sample','Escape');capture('Prefix F6')
            tmux('send-keys','-t','sample','j','Enter');screen=capture('OpenAI')
            assert 'Local' in screen and 'Claude' in screen
            tmux('send-keys','-t','sample','Escape');capture('Prefix F6')
            tmux('send-keys','-t','sample','Escape')
            deadline=time.monotonic()+3
            while not (tmp_path/'done').exists() and time.monotonic()<deadline:time.sleep(.03)
            assert (tmp_path/'done').read_text()=='canceled'
        finally:tmux('kill-server')
