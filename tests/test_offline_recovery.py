"""Prompted outage recovery: isolated runtimes, no real network or model inference."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest
from conftest import load_script
from test_offline import started, event, error, fake_codex

ROOT = Path(__file__).parents[1]

@pytest.fixture
def offline():
    return load_script(ROOT/'libexec/t_offline.py', 'offline_recovery_test')


class UI:
    def __init__(self, choices):
        self.choices, self.calls, self.notices = iter(choices), [], []
    def pick(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return next(self.choices)
    def done(self, *args):self.notices.append(args)


def policy(**kw):
    return dict(mode='automatic', provider='lmstudio', model='a', context_tokens=32768,
                timeout_seconds=300, on_cloud_failure='ask', **kw)


def models():
    return [{'model': 'a', 'provider': 'lmstudio', 'ready': True, 'fit': True},
            {'model': 'b', 'provider': 'ollama', 'ready': True, 'fit': True},
            {'model': 'c', 'provider': 'lmstudio', 'ready': False, 'fit': None, 'reason': 'Not downloaded'}]


def test_ready_model_is_immediate_action(offline):
    local={'catalogue':lambda **kw:{'models':models()}}
    ui=UI(['1'])
    selected=offline.choose_local(ui,local,policy())
    assert selected['model']=='b' and selected['provider']=='ollama'
    assert len(ui.calls)==1  # No second model details/confirmation screen.
    args,kwargs=ui.calls[0]
    assert kwargs['disabled']=={'2':'Not downloaded'}
    assert 'Memory fit ?' in kwargs['values']['2']
    assert 'Default' in kwargs['values']['0']
    for choice in ('pause',None):assert offline.choose_local(UI([choice]),local,policy()) is None
    ui=UI(['pause'])
    assert offline.choose_local(ui,{'catalogue':lambda **kw:{'models':[]}},policy(),failed=True) is None
    assert ui.calls[0][0][1]==[('pause','Keep task paused')]
    ui=UI(['pause'])
    assert offline.choose_local(ui,{'catalogue':lambda **kw:(_ for _ in ()).throw(ValueError('missing'))},policy()) is None
    assert 'missing' in ui.calls[0][1]['context'][0]


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12),(120,12)])
def test_recovery_picker_uses_shared_viewport(offline,t_mod,width,height):
    from test_tui import make_ui
    ui=make_ui(t_mod,['j','j','enter','esc'],width,height)
    assert offline.choose_local(ui,{'catalogue':lambda **kw:{'models':models()}},policy()) is None
    assert any('Not downloaded' in ''.join(frame) for frame in ui.frames)
    assert all(len(frame)<=height for frame in ui.frames)


def test_cloud_choices_and_catalogue_failures(offline,monkeypatch):
    for choices in ([None],['stay'],['previous','stay'],['previous','switch']):
        result=offline.choose_cloud(UI(choices),'cloud-a')
        assert result==({'model':'cloud-a'} if choices[-1]=='switch' else None)
    monkeypatch.setattr(offline,'ui_helpers',lambda:{'_config_catalog':lambda *a:[{'id':'cloud-b','label':'Cloud B'}],
                                                  '_config_live_models':lambda *a:[]})
    assert offline.choose_cloud(UI(['choose','cloud-b','switch']),'cloud-a')=={'model':'cloud-b'}
    assert offline.choose_cloud(UI(['choose',None,'stay']),'cloud-a') is None
    assert offline.choose_cloud(UI(['previous',None,'stay']),None) is None
    monkeypatch.setattr(offline,'ui_helpers',lambda:{'_config_catalog':lambda *a:[], '_config_live_models':lambda *a:[]})
    ui=UI(['choose','stay']);assert offline.choose_cloud(ui,'cloud-a') is None
    assert ui.notices


def test_checkpoint_round_trip_keeps_original_requests_and_local_progress(offline,tmp_path):
    source=started(offline,tmp_path)
    handoff=source.prompt()
    cp=offline.Checkpoint(tmp_path,tmp_path,seed=source,handoff=handoff)
    cp.client({'id':1,'method':'thread/start','params':{}})
    cp.server({'id':1,'result':{'thread':{'id':'main'},'model':'local-alias'}})
    cp.client({'method':'turn/start','params':{'threadId':'main','input':[{'type':'text','text':handoff,'text_elements':[]}]}})
    assert cp.users==source.users and cp.active
    event(cp,'turn/started',turn={'id':'turn'})
    event(cp,'item/completed',item={'type':'fileChange','changes':[{'path':'updated.py'}],'status':'completed'})
    event(cp,'turn/completed',turn={'id':'stale','status':'completed'});assert cp.active
    event(cp,'turn/completed',turn={'id':'turn','status':'completed'});assert not cp.active
    assert 'updated.py' in cp.prompt('cloud') and 'new cloud conversation' in cp.prompt('cloud')
    cp.declined=True;event(cp,'error',error=error());assert not cp.due()
    cp.client({'method':'turn/start','params':{'threadId':'main','input':[{'type':'text','text':'New request'}]}})
    assert not cp.declined and cp.users[-1]=='New request'
    cp.client({'method':'turn/interrupt','params':{'threadId':'main'}})
    event(cp,'turn/completed',turn={'id':'turn','status':'interrupted'});assert not cp.active


def test_endpoint_reachability_is_credential_free(offline,monkeypatch):
    calls=[]
    @contextmanager
    def request(req,timeout):
        calls.append((req,timeout));yield SimpleNamespace(status=200)
    monkeypatch.setattr(offline.urllib.request,'urlopen',request)
    assert offline.cloud_reachable()
    assert calls[0][0].method=='HEAD' and not calls[0][0].has_header('Authorization')
    assert calls[0][1]==3
    for code in (401,403,429,503):
        monkeypatch.setattr(offline.urllib.request,'urlopen',lambda *a,**k:(_ for _ in ()).throw(offline.urllib.error.HTTPError('url',code,'',{},None)))
        assert offline.cloud_reachable()==(code==401)
    monkeypatch.setattr(offline.urllib.request,'urlopen',lambda *a,**k:(_ for _ in ()).throw(OSError('offline')))
    assert not offline.cloud_reachable()


def test_probe_requires_consecutive_successes_and_stops(offline,monkeypatch):
    outcomes=iter([True,False,True,True,True]);calls=[]
    monkeypatch.setattr(offline,'cloud_reachable',lambda:calls.append(1) or next(outcomes))
    probe=offline.RecoveryProbe()
    monkeypatch.setattr(probe.stop,'wait',lambda n:False)
    with probe:probe.thread.join(1)
    assert probe.ready.is_set() and len(calls)==5 and probe.stop.is_set()
    probe=offline.RecoveryProbe()
    monkeypatch.setattr(offline,'cloud_reachable',lambda:False)
    monkeypatch.setattr(probe.stop,'wait',lambda n:True)
    probe.run();assert not probe.ready.is_set()


def test_cloud_launch_preserves_permissions_and_replaces_payload(offline):
    argv=['codex','--no-daemon','-C','/repo','-m','original','-a','on-request','--add-dir','/extra','-c','model_reasoning_effort="high"','--','old task']
    same=offline.cloud_argv(argv,'new checkpoint')
    assert same[:-1]==argv[:-1] and same[-1]=='new checkpoint'
    previous=offline.cloud_argv(argv,'new checkpoint','native-model-selection')
    assert 'original' not in previous and 'native-model-selection' in previous
    assert 'model_reasoning_effort="high"' in previous
    changed=offline.cloud_argv(argv,'new checkpoint','other',True)
    assert 'original' not in changed and 'model_reasoning_effort="high"' not in changed
    assert '-a' in changed and '/extra' in changed and 'other' in changed
    assert offline.cloud_argv(['codex','old task'],'new')==['codex','--','new']
    opts=offline.local_server_options(['codex','--oss','--local-provider','lmstudio','--model','alias','-c','mcp_servers={}','-C','/repo','--add-dir','/extra','--dangerously-bypass-approvals-and-sandbox','--','task'])
    assert 'model_provider="lmstudio"' in opts and 'model="alias"' in opts and 'mcp_servers={}' in opts
    assert 'approval_policy="never"' in opts and '/repo' not in opts


def test_terminal_restoration_and_cancellation(offline,monkeypatch):
    calls=[]
    monkeypatch.setattr(offline.os,'isatty',lambda fd:True)
    monkeypatch.setattr(offline.os,'waitpid',lambda *a:calls.append('stopped'))
    monkeypatch.setattr(offline.termios,'tcgetattr',lambda fd:['native'])
    monkeypatch.setattr(offline.termios,'tcsetattr',lambda fd,when,attrs:calls.append(attrs))
    monkeypatch.setattr(offline,'terminal_snapshot',lambda:(['native content'],0,0,True))
    ui=SimpleNamespace(raw=lambda:calls.append('raw'),restore=lambda:calls.append('restore'))
    monkeypatch.setattr(offline,'ui_helpers',lambda:{'_RailUI':lambda:ui})
    frontend=SimpleNamespace(pid=10,poll=lambda:None,send_signal=lambda sig:calls.append(sig))
    with pytest.raises(KeyboardInterrupt):
        with offline.recovery_ui(frontend,['cooked']):raise KeyboardInterrupt
    assert calls==[offline.signal.SIGSTOP,'stopped',['cooked'],'raw','restore',['native'],offline.signal.SIGCONT,offline.signal.SIGWINCH]
    calls.clear()
    with offline.recovery_ui():pass
    assert calls==[['native'],'raw','restore',['native']]
    calls.clear()
    monkeypatch.setattr(offline,'terminal_snapshot',lambda:(_ for _ in ()).throw(ValueError('Not the owning pane')))
    with pytest.raises(ValueError,match='owning'):
        with offline.recovery_ui(frontend):pass
    assert calls==[offline.signal.SIGSTOP,'stopped',['native'],offline.signal.SIGCONT,offline.signal.SIGWINCH]
    monkeypatch.setattr(offline.os,'isatty',lambda fd:False)
    with pytest.raises(ValueError,match='terminal'):
        with offline.recovery_ui():pass


def test_monitor_bridge_stops_owned_runtime(offline,tmp_path,monkeypatch,fake_codex):
    monkeypatch.setenv('FIXTURE_ROOT',str(tmp_path));monkeypatch.setenv('FIXTURE_MODE','outage')
    monkeypatch.setenv('SOCKET_HELPER',str(ROOT/'libexec/t_codex_socket.py'))
    monkeypatch.setattr(offline,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path/'state'}})
    real_popen=offline.subprocess.Popen;processes=[]
    def popen(*a,**kw):
        kw['env']={k:v for k,v in (kw.get('env') or offline.os.environ).items() if not k.startswith('COV_CORE_')}
        proc=real_popen(*a,**kw);processes.append(proc);return proc
    monkeypatch.setattr(offline.subprocess,'Popen',popen)
    result=offline.supervise([str(fake_codex),'--','task'],dict(policy(),on_cloud_failure='pause'),tmp_path,
                             monitor=lambda cp,*a:{'model':'local'} if cp.due() else None)
    assert result['action']=={'model':'local'} and all(p.poll() is not None for p in processes)


def test_recovery_round_trip_and_failure_retry(offline,tmp_path,monkeypatch):
    m=offline;stages=[];choices=iter([dict(policy(),model='b'),dict(policy(),model='a')]);failures=[True]
    monkeypatch.setattr(m.os,'isatty',lambda fd:True)
    monkeypatch.setattr(m,'recovery_ui',lambda *a:nullcontext(UI([])))
    monkeypatch.setattr(m,'choose_local',lambda *a,**kw:next(choices))
    monkeypatch.setattr(m,'choose_cloud',lambda *a:{'model':'cloud-b'})
    class Probe:
        def __init__(self):self.ready=threading.Event();self.ready.set()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(m,'RecoveryProbe',Probe)
    def stage(argv,chosen,cwd,**kw):
        cp=kw['checkpoint'];stages.append((argv,kw))
        cp.client({'id':1,'method':'thread/start','params':{}})
        cp.server({'id':1,'result':{'thread':{'id':'main'},'model':'cloud-a'}})
        cp.client({'method':'turn/start','params':{'threadId':'main','input':[{'type':'text','text':cp.handoff or 'Original task'}]}})
        event(cp,'turn/started',turn={'id':'turn'})
        if len(stages)==1:
            event(cp,'turn/completed',turn={'id':'turn','status':'failed','error':error()})
            action=kw['monitor'](cp,None,None)
        elif len(stages)==2:
            assert kw['env']=={'CODEX_OSS_BASE_URL':'http://local/v1'}
            assert kw['monitor'](cp,None,None) is None  # Active turn: no prompt.
            event(cp,'item/completed',item={'type':'agentMessage','text':'Local progress'})
            event(cp,'turn/completed',turn={'id':'turn','status':'completed'})
            action=kw['monitor'](cp,None,None)
        else:
            assert cp.users==['Original task'] and 'Local progress' in argv[-1]
            assert 'cloud-b' in argv
            action=None
        return {'action':action,'exit_code':0,'checkpoint':cp}
    def lead(argv,**kw):
        assert kw['chosen']['mode']=='local-only' and kw['chosen']['on_cloud_failure']=='pause'
        if failures.pop() if failures else False:raise ValueError('local runtime busy')
        return kw['runner'](['codex','--oss','--local-provider','lmstudio','--model','alias','--',argv[-1]],env={'CODEX_OSS_BASE_URL':'http://local/v1'})
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path},'lead':lead})
    monkeypatch.setattr(m,'supervise',stage)
    assert m.recover(['codex','-a','on-request','--','Original task'],policy(),tmp_path)==0
    assert len(stages)==3
    assert list(tmp_path.glob('outage-*/1/status.json'))


def test_ask_noninteractive_preserves_native_cloud(offline,tmp_path,monkeypatch):
    monkeypatch.setattr(offline,'inference',lambda:{})
    monkeypatch.setattr(offline.os,'isatty',lambda fd:False)
    monkeypatch.setattr(offline.subprocess,'call',lambda argv,**kw:7)
    assert offline.recover(['codex','--','task'],policy(),tmp_path)==7


def test_shutdown_failure_never_retries_local(offline,tmp_path,monkeypatch):
    m=offline;choices=[]
    monkeypatch.setattr(m.os,'isatty',lambda fd:True)
    monkeypatch.setattr(m,'recovery_ui',lambda *a:nullcontext(UI([])))
    monkeypatch.setattr(m,'choose_local',lambda *a,**kw:choices.append(1) or policy())
    class Probe:
        def __enter__(self):return SimpleNamespace(ready=threading.Event())
        def __exit__(self,*a):pass
    monkeypatch.setattr(m,'RecoveryProbe',Probe)
    def stage(argv,chosen,cwd,**kw):
        cp=kw['checkpoint'];cp.thread='main';cp.users=['Task'];cp.failed=True
        return {'action':kw['monitor'](cp,None,None),'exit_code':None,'checkpoint':cp}
    def lead(*a,**kw):raise m.RuntimeStopError('writer could not stop')
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path},'lead':lead})
    monkeypatch.setattr(m,'supervise',stage)
    with pytest.raises(m.RuntimeStopError):m.recover(['codex'],policy(),tmp_path)
    assert len(choices)==1


def test_checkpoint_tracks_native_model_changes(offline,tmp_path):
    cp=started(offline,tmp_path)
    cp.client({'method':'turn/start','params':{'threadId':'main','model':'chosen-in-native-ui','input':[]}})
    assert cp.model=='chosen-in-native-ui'


def test_invalid_checkpoint_disables_local_selection(offline):
    ui=UI(['pause'])
    assert offline.choose_local(ui,{'catalogue':lambda **kw:{'models':models()}},policy(),
                                failed=True,blocked='Non-text request needs manual continuation') is None
    assert len(ui.calls[0][1]['disabled'])==3


@pytest.mark.parametrize('cancelled',[False,True])
def test_declining_outage_keeps_cloud_and_suppresses_repeat(offline,tmp_path,monkeypatch,cancelled):
    m=offline;offers=[]
    monkeypatch.setattr(m.os,'isatty',lambda fd:True)
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path}})
    monkeypatch.setattr(m,'recovery_ui',lambda *a:nullcontext(UI([])))
    def choose(*a):
        offers.append(1)
        if cancelled:raise KeyboardInterrupt
        return None
    monkeypatch.setattr(m,'choose_local',choose)
    def stage(*args,**kw):
        cp=kw['checkpoint'];cp.thread='main';cp.users=['Task'];cp.failed=True
        assert kw['monitor'](cp,None,None) is None
        assert cp.declined and kw['monitor'](cp,None,None) is None
        assert len(offers)==1
        return {'action':None,'exit_code':0,'checkpoint':cp}
    monkeypatch.setattr(m,'supervise',stage)
    assert m.recover(['codex'],policy(),tmp_path)==0


def test_stay_local_does_not_repeat_recovery_offer(offline,tmp_path,monkeypatch):
    m=offline;stages=[];offers=[]
    monkeypatch.setattr(m.os,'isatty',lambda fd:True)
    monkeypatch.setattr(m,'recovery_ui',lambda *a:nullcontext(UI([])))
    monkeypatch.setattr(m,'choose_local',lambda *a:policy())
    monkeypatch.setattr(m,'choose_cloud',lambda *a:offers.append(1))
    class Probe:
        def __enter__(self):
            ready=threading.Event();ready.set();return SimpleNamespace(ready=ready)
        def __exit__(self,*a):pass
    monkeypatch.setattr(m,'RecoveryProbe',Probe)
    def stage(*args,**kw):
        cp=kw['checkpoint'];cp.thread='main';cp.users=['Task'];cp.failed=True;stages.append(cp)
        action=kw['monitor'](cp,None,None)
        if len(stages)==2:
            assert action is None
            assert kw['monitor'](cp,None,None) is None
            assert len(offers)==1
        return {'action':action,'exit_code':0,'checkpoint':cp}
    def lead(argv,**kw):return kw['runner'](['codex','--local-provider','lmstudio'],env={})
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path},'lead':lead})
    monkeypatch.setattr(m,'supervise',stage)
    assert m.recover(['codex'],policy(),tmp_path)==0
    assert len(stages)==2


def test_picker_restores_native_alternate_screen(tmp_path):
    """A real headless terminal proves dismissal preserves the native screen buffer."""
    import os
    import shlex
    import shutil
    import subprocess
    import sys
    import time
    import uuid
    if not shutil.which('tmux'):
        pytest.skip('tmux required for terminal restoration')
    env={k:v for k,v in os.environ.items() if k in ('PATH','LANG')}
    env.update(HOME=str(tmp_path),TERM='xterm-256color',XDG_CONFIG_HOME=str(tmp_path),XDG_STATE_HOME=str(tmp_path))
    command=['tmux','-L','t-outage-test-'+uuid.uuid4().hex,'-f',os.devnull]
    def tmux(*args):return subprocess.run([*command,*args],env=env,capture_output=True,text=True,timeout=5)
    script=tmp_path/'preview.py'
    script.write_text('import runpy,subprocess,sys,time,termios\nfrom pathlib import Path\n'+
        'm=runpy.run_path('+repr(str(ROOT/'libexec/t_offline.py'))+')\n'+
        'root=Path('+repr(str(tmp_path))+')\nattrs=termios.tcgetattr(0)\n'+
        'p=subprocess.Popen([sys.executable,"-c",'+repr('import sys,time\nfrom pathlib import Path\n'
        'sys.stdout.write("\\033[?1049h\\033[2J\\033[HNATIVE SESSION CONTENT\\n");sys.stdout.flush()\n'
        f'Path({str(tmp_path/"ready")!r}).touch()\ntime.sleep(30)')+'])\n'+
        'try:\n'
        ' while not (root/"ready").exists():time.sleep(.01)\n'
        ' with m["recovery_ui"](p,attrs) as ui:\n'
        '  ui.pick("Recovery choice",[("local","Continue locally")])\n'
        ' (root/"done").touch()\n time.sleep(30)\n'
        'finally:\n p.terminate();p.wait()\n')
    def wait_for(check):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if check():return
            time.sleep(.03)
        pytest.fail('Isolated terminal timed out: '+tmux('capture-pane','-pt','preview').stdout)
    try:
        result=tmux('new-session','-d','-s','preview','-x','80','-y','24',shlex.join([sys.executable,str(script)]))
        assert result.returncode==0,result.stderr
        wait_for(lambda:'Recovery choice' in tmux('capture-pane','-pt','preview').stdout)
        assert tmux('send-keys','-t','preview','Enter').returncode==0
        wait_for(lambda:(tmp_path/'done').exists())
        output=tmux('capture-pane','-pt','preview').stdout
        assert 'NATIVE SESSION CONTENT' in output and 'Recovery choice' not in output
    finally:
        tmux('kill-server')


def test_snapshot_checks_owning_pane_and_retains_style(offline,monkeypatch):
    m=offline
    monkeypatch.delenv('TMUX',raising=False);monkeypatch.delenv('TMUX_PANE',raising=False)
    with pytest.raises(ValueError,match='tmux'):m.terminal_snapshot()
    monkeypatch.setenv('TMUX','isolated-test');monkeypatch.setenv('TMUX_PANE','%1')
    calls=[]
    def run(argv,**kw):
        calls.append(argv);assert kw['timeout']==2 and kw['check']
        return SimpleNamespace(stdout='/dev/own\t3\t4\t0\n' if 'display-message' in argv else '\033[32mNative text\033[0m\n\n')
    monkeypatch.setattr(m.subprocess,'run',run)
    monkeypatch.setattr(m.os,'ttyname',lambda fd:'/dev/other')
    with pytest.raises(ValueError,match='own'):m.terminal_snapshot()
    assert len(calls)==1
    monkeypatch.setattr(m.os,'ttyname',lambda fd:'/dev/own')
    assert m.terminal_snapshot()==(['\033[32mNative text\033[0m',''],3,4,False)
    assert m.inference()['DEFAULT']['on_cloud_failure']=='pause'
    assert '_RailUI' in m.ui_helpers()


def test_monitor_warning_retains_cloud_ui(offline,tmp_path,monkeypatch,fake_codex):
    m=offline
    monkeypatch.setenv('FIXTURE_ROOT',str(tmp_path));monkeypatch.setenv('FIXTURE_MODE','outage')
    monkeypatch.setenv('SOCKET_HELPER',str(ROOT/'libexec/t_codex_socket.py'))
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path/'state'}})
    def monitor(cp,*args):
        if cp.due():cp.notice='Checkpoint cannot be transferred';cp.declined=True
    result=m.supervise([str(fake_codex),'--','task'],dict(policy(),on_cloud_failure='pause'),tmp_path,monitor=monitor)
    assert result['action'] is None and result['exit_code']==0
    assert 'cannot be transferred' in json.loads((tmp_path/'warning.json').read_text())['params']['message']


def test_invalid_cloud_checkpoint_never_offers_a_model(offline,tmp_path,monkeypatch):
    m=offline
    monkeypatch.setattr(m.os,'isatty',lambda fd:True)
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path}})
    def stage(*args,**kw):
        cp=kw['checkpoint'];cp.thread='main';cp.users=['Task'];cp.failed=True;cp.invalid='Non-text request'
        assert kw['monitor'](cp,None,None) is None
        assert cp.declined and 'Non-text request' in cp.notice
        return {'action':None,'exit_code':0,'checkpoint':cp}
    monkeypatch.setattr(m,'supervise',stage)
    assert m.recover(['codex'],policy(),tmp_path)==0


@pytest.mark.parametrize('invalid',[False,True])
def test_failed_local_checkpoint_is_retained_on_pause(offline,tmp_path,monkeypatch,invalid):
    m=offline;choices=[];stages=[]
    monkeypatch.setattr(m.os,'isatty',lambda fd:True)
    monkeypatch.setattr(m,'recovery_ui',lambda *a:nullcontext(UI([])))
    def choose(*a,**kw):
        choices.append(kw)
        return policy() if len(choices)==1 else None
    monkeypatch.setattr(m,'choose_local',choose)
    class Probe:
        def __enter__(self):return SimpleNamespace(ready=threading.Event())
        def __exit__(self,*a):pass
    monkeypatch.setattr(m,'RecoveryProbe',Probe)
    def stage(*args,**kw):
        cp=kw['checkpoint'];cp.thread='main';cp.users=['Task'];cp.failed=True;stages.append(cp)
        if len(stages)==1:action=kw['monitor'](cp,None,None)
        else:
            cp.invalid='Non-text input cannot be transferred' if invalid else ''
            cp.evidence.append({'kind':'agentMessage','text':'Local changes are retained'})
            action=None
        return {'action':action,'exit_code':1,'checkpoint':cp}
    def lead(argv,**kw):return kw['runner'](['codex','--local-provider','lmstudio'],env={})
    monkeypatch.setattr(m,'inference',lambda:{'helper':lambda n:{'state_root':lambda:tmp_path},'lead':lead})
    monkeypatch.setattr(m,'supervise',stage)
    assert m.recover(['codex'],policy(),tmp_path)==1
    assert len(stages)==2 and choices[-1]['failed']
    assert bool(choices[-1]['blocked'])==invalid
    assert list(tmp_path.glob('outage-*/1/status.json'))
