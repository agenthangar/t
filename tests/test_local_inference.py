"""Local inference boundaries use fake runtimes; never install or load model weights."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from conftest import load_script


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in ('XDG_CONFIG_HOME','XDG_STATE_HOME','XDG_CACHE_HOME','CODEX_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    return load_script(Path(__file__).parents[1]/'libexec/t_local_inference.py', 'local_inference_test')


def hardware():
    return dict(ram_bytes=128*1024**3, free_disk_bytes=3*1024**4, accelerator='Apple test')


def test_policy_and_instructions(local):
    m=local
    assert m.policy()['model']==m.DEFAULT_MODEL
    for mode in m.MODES:
        assert m.instructions({'mode': mode})
    for value in ([], {'bad':1}, {'mode':'bad'}, {'provider':'cloud'}, {'model':'https://bad'},
                  {'model':'foo-cloud'}, {'model':'../foo'}, {'model':False}, {'context_tokens':True},
                  {'context_tokens':1}, {'timeout_seconds':1900}):
        with pytest.raises(ValueError):m.policy(value)
    assert 'no cloud fallback' in m.instructions({'mode':'local-only'})
    assert 'task delegation' in m.instructions({'mode':'automatic'})


def test_receipts_fit_and_identity(local):
    m=local;name=m.DEFAULT_MODEL
    assert len(m.artifacts())==4
    with pytest.raises(ValueError):m.artifact('fake')
    for name_bad in ('/tmp/x','../x','.hidden',''):
        with pytest.raises(ValueError):m.safe_file(name_bad)
    assert not m.receipt(name)
    directory=m.model_directory(name);directory.mkdir(parents=True)
    path=directory/'.t-artifact.json'
    for text in ('{bad','{}',json.dumps({'model':name,'revision':'a'*40,'files':[]})):
        path.write_text(text);assert not m.receipt(name)
    (directory/'weights.safetensors').write_bytes(b'123')
    data={'model':name,'revision':'a'*40,'files':[{'name':'weights.safetensors','size':3}], 'verified_context':32768}
    path.write_text(json.dumps(data));assert m.receipt(name)==data
    row=m.report(m.artifact(name),hardware());assert row['ready'] and row['fit'] and row['disk_fit']
    assert m.report(m.artifacts()[1],hardware())['fit'] is None
    assert m.report(m.artifact(name),dict(hardware(),ram_bytes=1024))['fit'] is False
    assert m.report(m.artifact(name),dict(hardware(),accelerator='unknown'))['fit'] is None
    data['measurement']={'median':10};path.write_text(json.dumps(data));assert m.report(m.artifact(name),hardware())['measurement']['median']==10
    (directory/'weights.safetensors').write_bytes(b'1');assert not m.receipt(name)


def test_effective_and_catalogue(local,monkeypatch):
    m=local
    monkeypatch.setattr(m,'helper',lambda name: {'load':lambda: ({},None),'tool_document':lambda d,t:d} if name=='t_workflow' else {'launch_policy':lambda *a: {'enabled':True,'inference':{'mode':'automatic'}}})
    assert m.effective('/repo')['mode']=='automatic'
    for chosen,expect in ((None,None),({'enabled':True},None),({'enabled':False},'disabled')):
        monkeypatch.setattr(m,'helper',lambda name: {'load':lambda: ({},None),'tool_document':lambda d,t:d} if name=='t_workflow' else {'launch_policy':lambda *a:chosen})
        result=m.effective('/repo');assert (result['mode'] if result else None)==expect
    legacy={'hardware':hardware,'report':lambda:{'models':[{'model':'qwen:8b','bytes':4,'fit':True,'score':50}], 'benchmark':{'source':'fixture','release':'2026-01-01'}},'missing_bytes':lambda *a:0}
    monkeypatch.setattr(m,'helper',lambda name:legacy)
    assert len(m.catalogue()['models'])==5
    assert m.catalogue('livebench-coding')['models'][-1]['benchmark']['score']==50
    with pytest.raises(ValueError):m.catalogue('fake')


def test_runtime_lease(local,monkeypatch):
    m=local;calls=[]
    monkeypatch.setattr(m,'receipt',lambda n:{'revision':'a'*40})
    monkeypatch.setattr(m,'request',lambda *a,**k:{})
    def lms(*a,**k):
        calls.append(a)
        if a[0]=='ls':
            return json.dumps([{'indexedModelIdentifier':m.DEFAULT_MODEL,'modelKey':'fixture','deviceIdentifier':None}])
        if a[0]=='ps':
            alias=next(c[c.index('--identifier')+1] for c in calls if c[0]=='load')
            return json.dumps([{'identifier':alias,'indexedModelIdentifier':m.DEFAULT_MODEL,'contextLength':32768}])
        return ''
    monkeypatch.setattr(m,'run_lms',lms)
    with m.runtime(m.DEFAULT_MODEL) as (base,alias,info):
        assert base=='http://127.0.0.1:1234' and alias.startswith('t-local-')
    assert [c[0] for c in calls]==['ls','load','ps','unload']
    calls.clear()
    with m.provider_runtime(m.policy({'mode':'local-only'})):
        assert '--ttl' not in next(c for c in calls if c[0]=='load')
    calls.clear()
    monkeypatch.setattr(m,'request',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    with pytest.raises(RuntimeError):
        with m.runtime(m.DEFAULT_MODEL):raise RuntimeError('fixture')
    assert [c[:2] for c in calls if c[0]=='server']==[('server','start'),('server','stop')]
    with pytest.raises(ValueError,match='unverified'):
        with m.runtime(m.artifacts()[1]['model']):pass
    monkeypatch.setattr(m,'receipt',lambda n:{})
    with pytest.raises(ValueError,match='downloaded'):
        with m.runtime(m.DEFAULT_MODEL):pass


def test_install_verify_and_measure(local,monkeypatch):
    m=local;name=m.DEFAULT_MODEL;payload=b'123'
    monkeypatch.setattr(m,'lms',lambda:'/fake/lms')
    monkeypatch.setattr(m,'helper',lambda n:{'hardware':hardware})
    manifest={'sha':'b'*40,'siblings':[{'rfilename':'weights.safetensors','size':3,'lfs':{'sha256':m.hashlib.sha256(payload).hexdigest()}},{'rfilename':'.hidden'},{'rfilename':'skip.bin'}]}
    monkeypatch.setattr(m,'request',lambda url,*a,**k:manifest if 'huggingface' in url else {'output':[{}],'usage':{'completion_tokens':30}})
    def download(cmd,**kw):Path(cmd[cmd.index('-o')+1]).write_bytes(payload)
    monkeypatch.setattr(m.subprocess,'run',download)
    @contextmanager
    def runtime(*a):yield 'http://fixture','alias',m.receipt(name)
    monkeypatch.setattr(m,'runtime',runtime)
    assert m.install(name,True)['downloaded']
    assert not m.receipt(name).get('verified_context')
    assert m.install(name)['ready']
    monkeypatch.setattr(m,'run_lms',lambda *a:'fixture-1')
    monkeypatch.setattr(m.platform,'platform',lambda:'fixture-platform')
    ticks=iter(range(8));monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    assert m.measure(name)['median']==30
    monkeypatch.setattr(m,'request',lambda *a,**k:{})
    with pytest.raises(ValueError,match='no output'):m.install(name)
    with pytest.raises(ValueError,match='token usage'):
        monkeypatch.setattr(m.time,'monotonic',lambda:1);m.measure(name)


def test_commands(local,t_mod,monkeypatch,capsys):
    m=local;row=dict(m.artifact(m.DEFAULT_MODEL),provider='lmstudio',benchmark=None,fit=True,installed=False,reason='estimated fit')
    monkeypatch.setattr(m,'catalogue',lambda *a,**k:{'models':[row], 'hardware':hardware(), 'context_tokens':32768})
    monkeypatch.setattr(m,'helper',lambda n:{'reservation':nullcontext,'refresh':lambda:None})
    monkeypatch.setattr(m,'install',lambda *a:{'downloaded':True})
    monkeypatch.setattr(m,'measure',lambda *a:{'median':10})
    for action in ('list','refresh','show','install','measure'):
        argv=['config','models',action]+([] if action in ('list','refresh') else [m.DEFAULT_MODEL])+(['--yes'] if action=='install' else [])
        args=t_mod.build_parser().parse_args(argv)
        assert m.command(args)==0
        args.json=True;assert m.command(args)==0
        assert t_mod.main(argv+['--help'])==0
    assert 'Memory' in capsys.readouterr().out or row['model']
    with pytest.raises(ValueError,match='unknown artifact'):
        m.command(SimpleNamespace(models_action='show',model_name='bad'))
    with pytest.raises(ValueError,match='requires'):
        m.command(SimpleNamespace(models_action='list',suite='gpqa',benchmark='2026-01-01'))


def test_local_lead_fails_closed(local,monkeypatch,tmp_path):
    m=local;calls=[]
    monkeypatch.setattr(m.subprocess,'call',lambda argv,**kw:calls.append((argv,kw)) or 0)
    monkeypatch.setattr(m,'effective',lambda cwd:None)
    assert m.lead(['codex','hello'])==0 and calls[-1][0]==['codex','hello']
    monkeypatch.setattr(m,'effective',lambda cwd:m.policy({'mode':'local-only'}))
    with pytest.raises(ValueError,match='new session'):m.lead(['codex','resume','sid'])
    with pytest.raises(ValueError,match='cloud review'):m.lead(['codex','--approve-for-me'])
    worker=load_script(Path(__file__).parents[1]/'libexec/t_local_worker.py','inference_worker')
    workflow=m.helper('t_workflow')
    toml=m.helper('t_toml')
    monkeypatch.setattr(m,'helper',lambda n: toml if n=='t_toml' else {'hardware':hardware} if n=='t_local_models' else workflow if n=='t_workflow' else {'reservation':nullcontext,'state_root':lambda:tmp_path,'environment':worker.environment})
    @contextmanager
    def runtime(*a):yield 'http://127.0.0.1:1234','local-alias',{'revision':'a'*40}
    monkeypatch.setattr(m,'runtime',runtime)
    assert m.lead(['codex','--model','cloud','-c','model_provider="openai"','--enable','fast_mode','--profile=x','-c','sandbox_mode="read-only"','hi'])==0
    argv,kwargs=calls[-1]
    assert 'cloud' not in argv and 'model_provider="openai"' not in argv
    assert argv[argv.index('--model')+1]=='local-alias'
    assert 'OPENAI_API_KEY' not in kwargs['env']
    assert 'mcp_servers={}' in argv and 'features.multi_agent=false' in argv
    monkeypatch.setattr(m,'effective',lambda cwd: (_ for _ in ()).throw(ValueError('bad policy')))
    count=len(calls)
    with pytest.raises(ValueError):m.lead(['codex'])
    assert len(calls)==count


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12)])
def test_disabled_picker_and_model_details(local,t_mod,monkeypatch,width,height):
    from test_tui import make_ui
    ui=make_ui(t_mod,['enter','j','enter'],width,height)
    assert ui.pick('Actions',[('bad','Install'),('back','Back')],disabled={'bad':'Does not fit'})=='back'
    assert any('Does not fit' in ''.join(frame) for frame in ui.frames)
    m=local;row=m.report(m.artifact(m.DEFAULT_MODEL),hardware())
    row['benchmark']=None
    monkeypatch.setattr(m,'catalogue',lambda *a,**k:{'models':[row]})
    class UI:
        def __init__(self):self.choices=iter([m.DEFAULT_MODEL,'back','__suite','gpqa','__back'])
        def pick(self,*a,**kw):return next(self.choices)
        def page(self,*a,**kw):return 'enter'
    assert m.select_model(UI()) is None
    assert 'Publisher benchmarks' in '\n'.join(m.detail_lines(row))
    row.update(ready=True,revision='a'*40,measurement={'median':2,'min':1,'max':3,'metric':'fixture','context_tokens':32768,'stale':True})
    assert 'Stale measurement' in '\n'.join(m.detail_lines(row))
    class Menu:
        def __init__(self,choices):self.choices=iter(choices)
        def pick(self,*a,**kw):return next(self.choices)
    assert m.workflow_menu(Menu(['mode','local-only','context_tokens','8192','timeout_seconds','60','back']))['mode']=='local-only'
    monkeypatch.setattr(m,'select_model',lambda *a:{'model':m.DEFAULT_MODEL,'provider':'lmstudio'})
    assert m.workflow_menu(Menu(['model','back']))['model']==m.DEFAULT_MODEL


def test_workflow_roundtrip_suppresses_cloud_stages(local,monkeypatch):
    w=local.helper('t_workflow')
    document=w['defaults']()
    policy=document['workflows']['standard']
    policy['inference']=local.policy({'mode':'local-only'})
    restored=w['validate'](json.loads(w['encode'](document)))
    assert w['main_session_settings'](restored['workflows']['standard'])=={}
    assert w['model_stages'](policy)==[]
    text=w['render'](restored)
    assert 'LOCAL ONLY' in text and 'Plan with' not in text
    assert any('local-only' in x for x in w['summary_lines'](policy))
    policy['inference']['mode']='automatic'
    assert 'AUTOMATIC HYBRID' in w['render'](document)


def test_ollama_measurement_and_provider_identity(local,monkeypatch,tmp_path):
    m=local
    @contextmanager
    def server(*a):yield 'http://127.0.0.1:12345',{'HOME':str(tmp_path)}
    w={'ollama_server':server,'executable':lambda n:'/fake/'+n,'check_model':lambda *a:{'capabilities':['tools']},'state_root':lambda:tmp_path}
    monkeypatch.setattr(m,'helper',lambda n:w if n=='t_local_worker' else {'hardware':hardware})
    calls=[];monkeypatch.setattr(m.subprocess,'run',lambda argv,**k:calls.append(argv))
    monkeypatch.setattr(m,'request',lambda *a,**k:{'eval_count':20,'eval_duration':10**9})
    assert m.ollama_action('qwen:8b','install')['ready']
    assert calls[0][-2:]==['pull','qwen:8b']
    assert m.ollama_action('qwen:8b','measure')['median']==20
    monkeypatch.setattr(m,'request',lambda *a,**k:{})
    with pytest.raises(ValueError,match='decode'):m.ollama_action('qwen:8b','measure')
    chosen=m.policy({'provider':'ollama','model':'qwen:8b'})
    monkeypatch.setattr(m,'request',lambda *a,**k:{'models':[{'name':'qwen:8b','digest':'sha256:fixture'}]})
    with m.provider_runtime(chosen) as (_,name,info):assert name=='qwen:8b' and info['revision']=='sha256:fixture'
    monkeypatch.setattr(m,'request',lambda *a,**k:{'models':[]})
    with pytest.raises(ValueError,match='digest'):
        with m.provider_runtime(chosen):pass


def test_download_validation_and_runtime_tools(local,monkeypatch,tmp_path):
    m=local;name=m.DEFAULT_MODEL
    monkeypatch.setattr(m,'helper',lambda n:{'hardware':hardware})
    monkeypatch.setattr(m,'report',lambda *a:{'disk_fit':False,'fit':False,'reason':'bad'})
    with pytest.raises(ValueError,match='disk'):m.install(name)
    monkeypatch.setattr(m,'report',lambda *a:{'disk_fit':True,'fit':False,'reason':'bad'})
    with pytest.raises(ValueError,match='bad'):m.install(name)
    monkeypatch.setattr(m,'report',lambda *a:{'disk_fit':True,'fit':True,'reason':'fit'})
    for manifest,message in (({'sha':'bad'},'revision'),({'sha':'a'*40,'siblings':[]},'weights')):
        monkeypatch.setattr(m,'request',lambda *a,**k:manifest)
        with pytest.raises(ValueError,match=message):m.install(name,True)
    manifest={'sha':'a'*40,'siblings':[{'rfilename':'a.safetensors','size':3,'lfs':{'sha256':'wrong'}}]}
    monkeypatch.setattr(m,'request',lambda *a,**k:manifest)
    monkeypatch.setattr(m.subprocess,'run',lambda argv,**kw:Path(argv[argv.index('-o')+1]).write_bytes(b'1'))
    with pytest.raises(ValueError,match='incomplete'):m.install(name,True)
    monkeypatch.setattr(m.subprocess,'run',lambda argv,**kw:Path(argv[argv.index('-o')+1]).write_bytes(b'123'))
    with pytest.raises(ValueError,match='checksum'):m.install(name,True)
    monkeypatch.setattr(m.shutil,'which',lambda n:None)
    with pytest.raises(ValueError,match='runtime missing'):m.lms()
    binary=tmp_path/'.lmstudio/bin/lms';binary.parent.mkdir(parents=True);binary.write_text('')
    assert m.lms()==str(binary)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='fixture'))
    assert m.run_lms('--version')=='fixture'


def test_automatic_approval_is_narrow_and_transport_bound(local,monkeypatch,tmp_path):
    m=local
    assert m.approval_args({'mode':'manual'})==[]
    args=m.approval_args({'mode':'automatic'})
    assert len(args)==6 and all('mcp_servers.t-local-worker.tools.' in v for v in args[1::2])
    assert not m.trusted_connection()
    w=m.helper('t_local_worker');w['save_settings']({'enabled':True,'model':m.DEFAULT_MODEL,'provider':'lmstudio'})
    assert not m.trusted_connection()
    config=Path(m.os.environ['CODEX_HOME'])/'config.toml';config.parent.mkdir(parents=True,exist_ok=True)
    config.write_text('[mcp_servers.t-local-worker]\ncommand="/unrelated"\nargs=["mcp", "--local-worker"]\n')
    assert not m.trusted_connection()
    config.write_text('[mcp_servers.t-local-worker]\ncommand='+json.dumps(str(m.ROOT/'bin/t'))+'\nargs=["mcp", "--local-worker"]\n')
    assert m.trusted_connection()
    workflow=m.helper('t_workflow');p=dict(workflow['DEFAULT'],inference=m.policy({'mode':'automatic'}))
    assert workflow['inference_args'](p,'codex')==args
    assert workflow['inference_args'](p,'claude')==[]
    w['save_settings']({'enabled':False,'model':m.DEFAULT_MODEL})
    assert workflow['inference_args'](p,'codex')==[]


def test_install_review_and_download_only(local,t_mod,monkeypatch):
    m=local;row=dict(m.artifact(m.DEFAULT_MODEL),provider='lmstudio',fit=True,installed=False,reason='fit')
    monkeypatch.setattr(m,'catalogue',lambda *a,**k:{'models':[row]})
    args=SimpleNamespace(models_action='install',model_name=m.DEFAULT_MODEL,yes=False)
    monkeypatch.setattr(m.sys.stdin,'isatty',lambda:False)
    with pytest.raises(ValueError,match='--yes'):m.command(args)
    monkeypatch.setattr(m.sys.stdin,'isatty',lambda:True)
    class Dialog:
        accepted=False
        def __init__(self,api):pass
        def review(self,lines):return self.accepted
        def close(self):pass
    monkeypatch.setattr(m,'helper',lambda n:{'Dialog':Dialog,'reservation':nullcontext})
    monkeypatch.setattr(m.runpy,'run_path',lambda *a:{})
    calls=[];monkeypatch.setattr(m,'install',lambda *a:calls.append(a) or {'ready':False})
    assert m.command(args)==130 and not calls
    Dialog.accepted=True;args.download_only=True;args.context=8192
    assert m.command(args)==0 and calls==[(m.DEFAULT_MODEL,True,8192)]
    args.yes=True;args.provider='ollama';row['provider']='ollama'
    monkeypatch.setattr(m,'ollama_action',lambda *a:{'ready':True})
    assert m.command(args)==0


def test_request_bypasses_proxy_and_preserves_payload(local,monkeypatch):
    m=local;seen=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self):return b'{"ok":true}'
    class Opener:
        def open(self,request,timeout):
            seen.append((request,timeout));return Response()
    monkeypatch.setattr(m.urllib.request,'build_opener',lambda proxy:Opener())
    assert m.request('http://127.0.0.1:1234/test',{'prompt':'fixture'},timeout=12)=={'ok':True}
    assert json.loads(seen[0][0].data)=={'prompt':'fixture'} and seen[0][1]==12


def test_runtime_rejects_ambiguous_or_wrong_artifact(local,monkeypatch):
    m=local;calls=[]
    monkeypatch.setattr(m,'receipt',lambda n:{'revision':'a'*40})
    monkeypatch.setattr(m,'request',lambda *a,**k:{})
    monkeypatch.setattr(m,'run_lms',lambda *a,**k:'[]')
    with pytest.raises(ValueError,match='uniquely'):
        with m.runtime(m.DEFAULT_MODEL):pass
    def lms(*args,**kw):
        calls.append(args)
        if args[0]=='ls':return json.dumps([{'indexedModelIdentifier':m.DEFAULT_MODEL,'modelKey':'fixture'}])
        return '[]'
    monkeypatch.setattr(m,'run_lms',lms)
    with pytest.raises(ValueError,match='identity'):
        with m.runtime(m.DEFAULT_MODEL):pass
    assert calls[-1][0]=='unload'
    monkeypatch.setattr(m,'run_lms',lambda *a,**k: (_ for _ in ()).throw(ValueError('load failed')))
    with pytest.raises(ValueError):
        with m.runtime(m.DEFAULT_MODEL):pass


def test_picker_install_measure_and_unavailable(local,monkeypatch):
    m=local;row=dict(m.artifact(m.DEFAULT_MODEL),provider='lmstudio',fit=True,installed=True,ready=True,benchmark=None,reason='fit',disk_fit=True)
    monkeypatch.setattr(m,'catalogue',lambda *a,**k:{'models':[row]})
    monkeypatch.setattr(m,'helper',lambda n:{'reservation':nullcontext})
    calls=[]
    monkeypatch.setattr(m,'install',lambda *a:calls.append(a) or {})
    monkeypatch.setattr(m,'measure',lambda *a:calls.append(a) or {})
    class UI:
        def __init__(self,choices):self.choices=iter(choices)
        def pick(self,*a,**k):return next(self.choices)
        def page(self,*a,**k):return 'enter'
        def done(self,*a):pass
        def restore(self):pass
        def raw(self):pass
    for action in ('install','download','measure'):
        choices=[m.DEFAULT_MODEL,action]+(['yes'] if action!='measure' else [])+['__back']
        assert m.select_model(UI(choices)) is None
    assert len(calls)==3
    assert m.select_model(UI([m.DEFAULT_MODEL,'use']))=={'model':m.DEFAULT_MODEL,'provider':'lmstudio'}
    assert m.select_model(UI([m.DEFAULT_MODEL,'install','no','__back'])) is None
    monkeypatch.setattr(m,'measure',lambda *a:(_ for _ in ()).throw(ValueError('unavailable')))
    assert m.select_model(UI([m.DEFAULT_MODEL,'measure','__back'])) is None
    row.update(fit=None,ready=False,disk_fit=False)
    assert m.select_model(UI([m.DEFAULT_MODEL,'back','__back'])) is None


def test_ollama_catalogue_surfaces_measurement_without_starting_runtime(local, monkeypatch):
    m=local;name='qwen:8b'
    assert m.ollama_revision(name) is None and m.ollama_revision('../unsafe:tag') is None
    manifest=Path.home()/'.ollama/models/manifests/registry.ollama.ai/library/qwen/8b'
    manifest.parent.mkdir(parents=True);manifest.write_text('fixture manifest')
    legacy={'hardware':hardware,'report':lambda:{'models':[{'model':name,'bytes':4,'fit':True,'score':None,'reason':'fit'}], 'benchmark':{}},'missing_bytes':lambda *a:0}
    monkeypatch.setattr(m,'helper',lambda n:legacy)
    measured={'model':name,'revision':m.ollama_revision(name),'median':20,'min':19,'max':21,'metric':'decode tokens / second','context_tokens':32768,'hardware':hardware()}
    path=m.measurement_path(name);path.parent.mkdir(parents=True);path.write_text(json.dumps(measured))
    row=m.catalogue()['models'][-1]
    assert not row['measurement']['stale'] and 'Measured speed: 20.0' in '\n'.join(m.detail_lines(row))
    manifest.write_text('new revision')
    assert m.catalogue()['models'][-1]['measurement']['stale']
    for invalid in ('{invalid','{}',json.dumps(dict(measured,model='other:8b'))):
        path.write_text(invalid)
        assert 'measurement' not in m.catalogue()['models'][-1]


def test_outage_policy_and_routing(local,monkeypatch):
    m=local
    assert m.policy()['on_cloud_failure']=='pause'
    for value in ({'on_cloud_failure':'guess'},{'mode':'disabled','on_cloud_failure':'local'}):
        with pytest.raises(ValueError):m.policy(value)
    assert 'connection' in m.instructions({'mode':'automatic','on_cloud_failure':'local'})
    calls=[]
    monkeypatch.setattr(m,'helper',lambda n:{'supervise':lambda *a:calls.append(a) or 7})
    assert m.lead(['codex'],cwd='/repo',chosen={'mode':'automatic','on_cloud_failure':'local'})==7
    assert calls[0][2]=='/repo'
    class UI:
        def __init__(self,choices):self.choices=iter(choices)
        def pick(self,*a,**kw):return next(self.choices)
    assert m.workflow_menu(UI(['on_cloud_failure','local','back']))['on_cloud_failure']=='local'
    assert m.workflow_menu(UI(['on_cloud_failure','local','mode','disabled','back']))['on_cloud_failure']=='pause'


def test_exact_workspace_trust_only(local,tmp_path):
    m=local;home=Path(m.os.environ['CODEX_HOME']);home.mkdir()
    path=home/'config.toml';workspace=tmp_path/'repo';workspace.mkdir()
    assert not m.trusted_workspace(workspace)
    path.write_text('not TOML');assert not m.trusted_workspace(workspace)
    path.write_text('[projects]\n'+json.dumps(str(workspace))+' = {trust_level="trusted"}\n')
    assert m.trusted_workspace(workspace)
    assert not m.trusted_workspace(workspace/'child')
    assert not m.trusted_workspace(tmp_path)
    path.write_text('[projects]\n'+json.dumps(str(workspace))+' = {trust_level="untrusted"}\n')
    assert not m.trusted_workspace(workspace)


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12)])
def test_outage_picker_narrow(local,t_mod,width,height):
    from test_tui import make_ui
    ui=make_ui(t_mod,['j','j','enter','j','j','enter','q'],width,height)
    assert local.workflow_menu(ui)['on_cloud_failure']=='local'
    assert any('Pause and retry' in ''.join(frame) for frame in ui.frames)
