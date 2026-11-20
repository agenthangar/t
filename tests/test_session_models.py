"""Native continuation controls never launch real agents in routine tests."""
from contextlib import contextmanager
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_script
from test_local_runtime_ui import UI

ROOT=Path(__file__).parents[1]


@pytest.fixture
def models(monkeypatch,tmp_path):
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    monkeypatch.delenv('TMUX',raising=False)
    monkeypatch.delenv('TMUX_PANE',raising=False)
    return load_script(ROOT/'libexec/t_session_models.py','session_models_test')


def checkpoint(tmp_path):
    offline=load_script(ROOT/'libexec/t_offline.py','model_checkpoint_test')
    cp=offline.Checkpoint(tmp_path,str(tmp_path),allow_images=True)
    cp.thread='source-thread';cp.generation=1;cp.user([{'type':'text','text':'Continue the task'}])
    return cp


def test_screenshot_snapshot_is_private_and_detects_mutation(tmp_path):
    cp=checkpoint(tmp_path)
    image=tmp_path/'screen.png';image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    cp.user([{'type':'localImage','path':str(image)}]);cp.save()
    path=Path(cp.attachment_paths()[0])
    assert path!=image and path.read_bytes()==image.read_bytes()
    assert path.stat().st_mode & 0o777==0o600
    image.unlink();assert cp.prompt('OpenAI')
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):cp.prompt()
    assert (tmp_path/'checkpoint.json').exists()


def test_unsupported_attachment_and_symlink_fail_closed(tmp_path):
    cp=checkpoint(tmp_path);cp.user([{'type':'image','url':'https://example.invalid/private.png'}])
    with pytest.raises(ValueError,match='Attachment transfer unavailable'):cp.prompt()
    cp=checkpoint(tmp_path)
    target=tmp_path/'actual';target.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    link=tmp_path/'link';link.symlink_to(target)
    cp.user([{'type':'localImage','path':str(link)}])
    with pytest.raises(ValueError,match='Attachment transfer unavailable'):cp.prompt()


def test_picker_reviews_destination_and_disables_unverified_images(models):
    rows=[dict(id='m',model='m',label='M',ready=True,images=True,provider='openai',detail='available')]
    ui=UI('openai','m','continue')
    view=dict(context_tokens=32768,cwd='/sample',generation=3,attachments=1)
    result=models.choose(ui,view,lambda *a:rows)
    assert result['destination']=='openai' and result['generation']==3
    assert 'Worktree: /sample' in ui.pages[-1][2]['context']
    assert 'Screenshots: 1 · supported' in ui.pages[-1][2]['context']
    rows[0]['images']=False
    ui=UI('openai','m','back')
    assert models.choose(ui,view,lambda *a:rows) is None
    assert ui.pages[1][2]['disabled']=={'m':'Screenshot input support is not verified.'}


def test_controller_rechecks_owner_generation_and_readiness(models,tmp_path,monkeypatch):
    cp=checkpoint(tmp_path)
    info=['%9','1','$2','title',str(tmp_path),'window']
    monkeypatch.setattr(models,'helper',lambda name: {'pane_info':lambda *a:info,'ancestry':lambda *a:True})
    c=models.Controller(cp,{'model':'local','context_tokens':32768})
    rows=[dict(id='m',model='m',label='M',ready=True,images=False,provider='openai',detail='available')]
    monkeypatch.setattr(models,'model_choices',lambda *a:rows)
    with pytest.raises(ValueError,match='changed'):c.accept(dict(destination='openai',id='m',generation=0))
    cp.attachments=[dict(path='not-read',sha256='x',bytes=1)]
    with pytest.raises(ValueError,match='screenshot changed'):c.accept(dict(destination='openai',id='m',generation=1))
    cp.attachments=[]
    rows[0]['ready']=False
    with pytest.raises(ValueError,match='readiness'):c.accept(dict(destination='openai',id='m',generation=1))
    rows[0]['ready']=True
    assert c.accept(dict(destination='openai',id='m',generation=1))['accepted']
    cp.generation=2
    assert c.monitor(cp,None,None) is None and 'changed' in cp.notice
    info[1]='2'
    # pane_info fixtures return a fresh value in production.
    c.info=['%9','1','$2','title',str(tmp_path),'window']
    with pytest.raises(ValueError,match='ownership'):c.status()


def test_user_f6_binding_is_preserved(models,monkeypatch):
    calls=[]
    def tmux(socket,*args):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='user binding' if args[0]=='list-keys' else '')
    monkeypatch.setattr(models,'helper',lambda _: {'tmux':tmux})
    assert not models.install('/socket','%1','python helper')
    assert not any(c[0] in ('bind-key','set-option') for c in calls)


def test_f6_binding_on_isolated_tmux_server(models,tmp_path):
    with tempfile.TemporaryDirectory(prefix='t-model-key-') as root:
        socket=str(Path(root)/'tmux.sock')
        env={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE') and not k.startswith('COV_CORE_')}
        env.update(HOME=str(tmp_path),XDG_CONFIG_HOME=str(tmp_path/'config'))
        def tmux(*args):return subprocess.run(['tmux','-S',socket,*args],text=True,capture_output=True,env=env,timeout=5)
        try:
            assert tmux('-f','/dev/null','new-session','-d','-s','fixture','sleep 30').returncode==0
            pane=tmux('display-message','-p','-t','fixture','#{pane_id}').stdout.strip()
            assert models.install(socket,pane,'printf controlled')
            assert 'display-popup' in tmux('list-keys','-T','prefix','F6').stdout
            assert models.install(socket,pane,'printf replaced',replace=True)
            tmux('bind-key','-N','user binding','-T','prefix','F6','display-message','custom')
            assert not models.install(socket,pane,'printf blocked',replace=True)
            assert 'custom' in tmux('list-keys','-T','prefix','F6').stdout
        finally:tmux('kill-server')


def test_startup_input_preserves_image_but_drops_cloud_defaults(models):
    argv=['codex','--model','cloud','-c','model_provider="openai"','-i','/screen.png','--','Fix this']
    assert models.startup_argv(argv,['codex','--sandbox','read-only'])==['codex','--sandbox','read-only','--image','/screen.png','--','Fix this']
    assert models.startup_argv(['codex','hello'],['codex'])==['codex','--','hello']
    with pytest.raises(ValueError,match='Missing'):models.startup_argv(['codex','--image'],['codex'])


def test_local_to_openai_preserves_attachment_and_worktree(models,tmp_path,monkeypatch):
    offline=load_script(ROOT/'libexec/t_offline.py','session_loop_offline')
    screenshot=tmp_path/'source.png';screenshot.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    launched=[]
    def supervise(argv,chosen,cwd,**kw):
        launched.append((argv,cwd,kw))
        cp=kw['checkpoint']
        if len(launched)==1:
            cp.thread='source';cp.user([{'type':'text','text':'Fix bug'},{'type':'localImage','path':str(screenshot)}]);cp.save()
            return {'action':dict(destination='openai',provider='openai',model='m'), 'exit_code':0}
        assert '--image' in argv and Path(argv[argv.index('--image')+1]).read_bytes()==screenshot.read_bytes()
        assert cwd==str(tmp_path)
        return {'action':None,'exit_code':0}
    @contextmanager
    def serve(self):yield self
    monkeypatch.setattr(models.Controller,'__init__',lambda *a,**k:None)
    monkeypatch.setattr(models.Controller,'serve',serve)
    local={'lead':lambda argv,cwd,chosen,runner:runner(argv,{'T_LOCAL_CLIENT_ID':'fixture'}),
           'helper':lambda _: {'state_root':lambda:tmp_path}}
    off=dict(vars(offline),supervise=supervise)
    monkeypatch.setattr(models,'helper',lambda n:off if n=='t_offline' else local)
    assert models.run(['codex','--','Fix bug'],dict(model='local',provider='ollama',context_tokens=32768),str(tmp_path))==0
    assert len(launched)==2
    assert 'Fix bug' in launched[1][0][-1]
    assert list(tmp_path.glob('continuation-*/0/checkpoint.json'))


def test_switch_and_cleanup_leave_queued_request_untouched(models,monkeypatch):
    memory=load_script(ROOT/'libexec/t_memory_recovery.py','session_memory_test')
    view=dict(generation=1,context_tokens=32768,cwd='/sample',attachments=0,verification='Verified on this Mac at 32K',can_switch=True,
              request={'state':'waiting_for_memory','memory':{'reason':'headroom','shortfall_bytes':5*1024**3}})
    calls=[]
    monkeypatch.setattr(models,'helper',lambda _:dict(vars(memory),free_memory=lambda *a:calls.append('cleanup')))
    models.waiting_menu(UI('memory','back'),lambda:view,lambda _:calls.append('submit'))
    assert calls==['cleanup'] and view['request']['state']=='waiting_for_memory'


def test_retry_requires_expired_request(models,tmp_path,monkeypatch):
    cp=checkpoint(tmp_path)
    monkeypatch.setattr(models,'helper',lambda _: {'pane_info':lambda *a:None})
    c=models.Controller(cp,{'context_tokens':32768})
    monkeypatch.setattr(c,'verify',lambda:None)
    monkeypatch.setattr(c,'request',lambda:{'state':'waiting_for_memory'})
    with pytest.raises(ValueError,match='not expired'):c.accept(dict(destination='retry',generation=1))
    monkeypatch.setattr(c,'request',lambda:{'state':'timed_out'})
    assert c.accept(dict(destination='retry',generation=1))['accepted']


def test_private_controller_socket_and_exact_binding_cleanup(models,tmp_path,monkeypatch):
    cp=checkpoint(tmp_path);saved=[''];commands=[]
    def tmux(socket,*args):
        commands.append(args)
        if args[0]=='show-options':return SimpleNamespace(returncode=0,stdout=saved[0])
        if args[0]=='set-option':saved[0]='' if '-pu' in args else args[-1]
        return SimpleNamespace(returncode=1 if args[0]=='list-keys' else 0,stdout='')
    local={'pane_info':lambda *a:['%1','123','$1','title',str(tmp_path),'window'],
           'ancestry':lambda *a:True,'tmux':tmux}
    real=models.helper
    monkeypatch.setattr(models,'helper',lambda n:local if n=='t_local_status' else real(n))
    controller=models.Controller(cp,{'context_tokens':32768,'model':'fixture'})
    with controller.serve():
        import shlex
        endpoint=shlex.split(saved[0])[-1]
        status=models.call(endpoint)
        assert status['can_switch'] and status['cwd']==str(tmp_path)
        assert Path(endpoint).stat().st_mode & 0o777==0o600
        with pytest.raises(ValueError,match='Unknown action'):models.call(endpoint,'bad')
        with pytest.raises(ValueError,match='Invalid destination'):
            models.call(endpoint,'switch',selection={'destination':'bad','generation':1})
        cp.invalid='missing input'
        assert not models.call(endpoint)['can_switch']
    assert not saved[0] and not Path(endpoint).exists()
    assert any(x[0]=='bind-key' for x in commands)


def test_model_catalogues_preserve_readiness_and_image_metadata(models,monkeypatch):
    local=[dict(model='local',provider='mlx-vlm',ready=True,fit=True,input_modalities=['image']),
           dict(model='missing',provider='ollama',ready=False,fit=False)]
    app={'_config_live_models':lambda agent:[{'model':'m','inputModalities':['text','image']}],
         '_config_catalog':lambda *a:[dict(id='m',label='M')]}
    monkeypatch.setattr(models,'helper',lambda n:{'catalogue':lambda **kw:{'models':local}} if n=='t_local_inference' else {'ui_helpers':lambda:app})
    assert models.model_choices('local',32768)[0]['images']
    assert not models.model_choices('local',32768)[1]['ready']
    assert models.model_choices('openai',32768)[0]['images']
    assert not models.model_choices('claude',32768)[0]['images']


def test_exact_pane_request_selects_memory_blocker(models,monkeypatch):
    view={'clients':[dict(id='c',tmux='/socket',pane='%1',owner_verified=True),dict(id='other',tmux='/socket',pane='%2',owner_verified=True)],
          'requests':[dict(id='wait',client='c',state='waiting_for_memory',memory={'reason':'short'}),dict(id='aux',client='c',state='queued'),dict(id='foreign',client='other',state='queued')]}
    monkeypatch.setattr(models,'helper',lambda n:{'pane_info':lambda *a:['%1']} if n=='t_local_status' else {'status':lambda:view})
    assert models.pane_requests('/socket','%1')['id']=='wait'
    cp=SimpleNamespace()
    c=models.Controller(cp,{},'c')
    assert c.request()['id']=='wait'
    view['requests'][0]['state']='timed_out'
    assert c.request()['id']=='wait'
    view['requests'][1]['created']=1
    assert c.request()['id']=='aux'
    view['requests'][1]['state']='completed'
    assert not c.request()
    monkeypatch.setattr(models,'helper',lambda n:{'pane_info':lambda *a:None})
    with pytest.raises(ValueError,match='no longer'):models.pane_requests('/socket','%1')


def test_controller_pending_rejects_duplicates_and_returns_valid_action(models,tmp_path,monkeypatch):
    cp=checkpoint(tmp_path)
    monkeypatch.setattr(models,'helper',lambda _: {'pane_info':lambda *a:None})
    c=models.Controller(cp,{'context_tokens':32768})
    monkeypatch.setattr(c,'verify',lambda:None)
    monkeypatch.setattr(c,'request',lambda:{'state':'timed_out'})
    assert c.monitor(cp,None,None) is None
    action={'destination':'retry','generation':1}
    c.accept(action)
    with pytest.raises(ValueError,match='already pending'):c.accept(action)
    assert c.monitor(cp,None,None)==action


@pytest.mark.parametrize('destination',['local','retry','claude','failure','stop'])
def test_continuation_outcomes_and_failure_retention(models,tmp_path,monkeypatch,destination):
    offline=load_script(ROOT/'libexec/t_offline.py','session_outcomes_offline')
    calls=[]
    def supervise(argv,chosen,cwd,**kw):
        calls.append(argv)
        if destination=='failure':raise ValueError('failed to launch')
        if destination=='stop':raise offline.RuntimeStopError('writer still active')
        cp=kw['checkpoint'];cp.thread='source';cp.user([{'type':'text','text':'task'}]);cp.save()
        return {'action':dict(destination=destination,provider='ollama',model='next') if len(calls)==1 else None,'exit_code':0}
    @contextmanager
    def serve(self):yield self
    monkeypatch.setattr(models.Controller,'__init__',lambda *a,**k:None)
    monkeypatch.setattr(models.Controller,'serve',serve)
    local={'lead':lambda argv,cwd,chosen,runner:runner(argv,{}),'helper':lambda _: {'state_root':lambda:tmp_path}}
    monkeypatch.setattr(models,'helper',lambda n:dict(vars(offline),supervise=supervise) if n=='t_offline' else local)
    monkeypatch.setattr(models.shutil,'which',lambda _: '/bin/claude')
    external=[];monkeypatch.setattr(models.subprocess,'call',lambda argv,**kw:external.append((argv,kw)) or 0)
    if destination=='stop':
        with pytest.raises(offline.RuntimeStopError):models.run(['codex','--','task'],{'model':'m','provider':'ollama','context_tokens':32768},str(tmp_path))
        assert not external and len(calls)==1
    else:
        assert models.run(['codex','--','task'],{'model':'m','provider':'ollama','context_tokens':32768},str(tmp_path))==(1 if destination=='failure' else 0)
        if destination=='claude':
            assert external[0][0][:3]==['/bin/claude','--model','next']
            assert external[0][1]['cwd']==str(tmp_path)


def test_picker_unavailable_refresh_details_and_explicit_retry(models,monkeypatch):
    memory=load_script(ROOT/'libexec/t_memory_recovery.py','controls_edges')
    monkeypatch.setattr(models,'helper',lambda _:vars(memory))
    view=dict(generation=1,cwd='/sample',context_tokens=32768,can_switch=True,request={'state':'timed_out'})
    calls=[]
    models.waiting_menu(UI('details','retry'),lambda:view,lambda s:calls.append(s))
    assert calls==[{'destination':'retry','generation':1}]
    ui=UI('openai','back')
    def failed(*a):raise ValueError('catalogue offline')
    assert models.choose(ui,view,failed) is None
    assert any(p[0]=='Models unavailable' for p in ui.pages)
    # A failed submit leaves the source available and the menu usable.
    def reject(selection):raise ValueError('changed')
    ui=UI('retry','back');models.waiting_menu(ui,lambda:view,reject)
    assert any(p[0]=='Continuation unavailable' for p in ui.pages)
