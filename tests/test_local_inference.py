"""Local inference boundaries use fake runtimes; never install or load model weights."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from conftest import load_script, ManagedClientFixture


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.delenv('TMUX', raising=False)
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
    for label in ('Local requested', 'Local accepted', 'Local result', 'Lead checked', 'Local unavailable'):
        assert label in m.instructions({'mode':'automatic'})


def test_runtime_lease_pins_provider_artifact_and_context(local, monkeypatch, tmp_path):
    m = local
    chosen = m.policy()
    assert m.leased_runtime(chosen) is None
    path = tmp_path / 'lease.json'; monkeypatch.setenv('T_LOCAL_RUNTIME_RECEIPT', str(path))
    receipt = {'model': chosen['model'], 'provider': chosen['provider'], 'context_tokens': chosen['context_tokens'],
               'revision': 'pinned', 'alias': 'selected-alias', 'base': 'http://127.0.0.1:1234/capability/local'}
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(m, 'request', lambda url: receipt)
    assert m.leased_runtime(chosen) == receipt
    with m.provider_runtime(chosen) as (base, alias, identity):
        assert base == receipt['base'] and alias == 'selected-alias' and identity['revision'] == 'pinned'
    for change in ({'model': 'different'}, {'context_tokens': 4096}, {'base': 'https://external.invalid:1234'}):
        path.write_text(json.dumps({**receipt, **change}))
        with pytest.raises(ValueError): m.leased_runtime(chosen)
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(m, 'request', lambda url: {**receipt, 'revision': 'different'})
    with pytest.raises(ValueError, match='identity'): m.leased_runtime(chosen)


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
    monkeypatch.setattr(m,'helper',lambda name: {'load':lambda: ({},True),'tool_document':lambda d,t:d} if name=='t_workflow' else {'launch_policy':lambda *a: {'enabled':True,'inference':{'mode':'automatic'}}})
    assert m.effective('/repo')['mode']=='automatic'
    for chosen,expect in ((None,None),({'enabled':True},'manual'),({'enabled':False},None)):
        monkeypatch.setattr(m,'helper',lambda name: {'load':lambda: ({},True),'tool_document':lambda d,t:d} if name=='t_workflow' else {'launch_policy':lambda *a:chosen})
        result=m.effective('/repo');assert (result['mode'] if result else None)==expect
        if result: assert result['cloud_retry_seconds'] == 300
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
    original_helper = m.helper
    monkeypatch.setattr(m, 'helper', lambda n: {'Client': ManagedClientFixture} if n == 't_local_manager' else original_helper(n))
    with m.provider_runtime(m.policy({'mode':'local-only'})) as (base, model, info):
        assert base.endswith('/client-token') and model == m.DEFAULT_MODEL
        assert not calls  # Managed runtime does not load directly.
    calls.clear()
    monkeypatch.setattr(m,'request',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    with pytest.raises(RuntimeError):
        with m.runtime(m.DEFAULT_MODEL):raise RuntimeError('fixture')
    assert [c[:2] for c in calls if c[0]=='server']==[('server','start'),('server','stop')]
    with pytest.raises(ValueError,match='uniquely'):
        with m.runtime(m.artifacts()[1]['model']):pass
    monkeypatch.setattr(m,'receipt',lambda n:{})
    with pytest.raises(ValueError,match='downloaded'):
        with m.runtime(m.DEFAULT_MODEL):pass


def test_install_verify_and_measure(local,monkeypatch):
    m=local;name=m.DEFAULT_MODEL;payload=b'123'
    monkeypatch.setattr(m,'lms',lambda:'/fake/lms')
    monkeypatch.setattr(m,'helper',lambda n:{'hardware':hardware})
    manifest={'sha':'b'*40,'siblings':[{'rfilename':'weights.safetensors','size':3,'lfs':{'sha256':m.hashlib.sha256(payload).hexdigest()}},{'rfilename':'.hidden'},{'rfilename':'skip.bin'}]}
    monkeypatch.setattr(m,'request',lambda url,*a,**k:manifest if 'huggingface' in url else {'output':[{'type':'message','content':[{'type':'output_text','text':'OK'}]}],'usage':{'completion_tokens':30}})
    def download(cmd,**kw):Path(cmd[cmd.index('-o')+1]).write_bytes(payload)
    monkeypatch.setattr(m.subprocess,'run',download)
    @contextmanager
    def runtime(*a,**kw):yield 'http://fixture','alias',m.receipt(name)
    monkeypatch.setattr(m,'managed_runtime',runtime)
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
    original_helper=m.helper
    monkeypatch.setattr(m,'helper',lambda n:original_helper(n) if n=='t_model_display' else {'reservation':nullcontext,'refresh':lambda:None})
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
    status=m.helper('t_local_status')
    monkeypatch.setattr(m,'helper',lambda n: {'local_launch':lambda *a:None} if n=='t_usage' else status if n=='t_local_status' else toml if n=='t_toml' else {'hardware':hardware} if n=='t_local_models' else workflow if n=='t_workflow' else {'Client':ManagedClientFixture} if n=='t_local_manager' else {'reservation':nullcontext,'state_root':lambda:tmp_path,'environment':worker.environment})
    @contextmanager
    def runtime(*a,**kw):yield 'http://127.0.0.1:1234','local-alias',{'revision':'a'*40}
    monkeypatch.setattr(m,'runtime',runtime)
    def popen(argv, **kw):
        calls.append((argv,kw))
        return SimpleNamespace(poll=lambda:0, returncode=0)
    monkeypatch.setattr(m.subprocess, 'Popen', popen)
    assert m.lead(['codex','--model','cloud','-c','model_provider="openai"','--enable','fast_mode','--profile=x','-c','sandbox_mode="read-only"','hi'])==0
    argv,kwargs=calls[-1]
    assert 'cloud' not in argv and 'model_provider="openai"' not in argv
    assert argv[argv.index('--model')+1]==m.DEFAULT_MODEL
    assert 'OPENAI_API_KEY' not in kwargs['env']
    assert 'mcp_servers={}' in argv and 'features.multi_agent=false' in argv
    def runner(argv, env):
        assert 'OPENAI_API_KEY' not in env and '--oss' in argv
        assert env['CODEX_OSS_BASE_URL'] == 'http://127.0.0.1:1234/client-token/v1'
        assert env['OLLAMA_HOST'] == 'http://127.0.0.1:1234/client-token'
        return {'action': 'cloud'}
    assert m.lead(['codex', '--', 'task'], runner=runner) == {'action': 'cloud'}
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
        def __init__(self):self.choices=iter([m.DEFAULT_MODEL,'back',('gpqa',m.DEFAULT_MODEL),'__back'])
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
    w={'reservation':nullcontext,'ollama_server':server,'executable':lambda n:'/fake/'+n,'check_model':lambda *a:{'capabilities':['tools']},'state_root':lambda:tmp_path}
    monkeypatch.setattr(m,'helper',lambda n:w if n=='t_local_worker' else {'hardware':hardware})
    calls=[];monkeypatch.setattr(m.subprocess,'run',lambda argv,**k:calls.append(argv))
    @contextmanager
    def managed(*a, **kw): yield 'http://127.0.0.1:12345', 'qwen:8b', {}
    monkeypatch.setattr(m, 'managed_runtime', managed)
    monkeypatch.setattr(m,'request',lambda *a,**k:{'response':'OK','eval_count':20,'eval_duration':10**9})
    assert m.ollama_action('qwen:8b','install')['ready']
    assert calls[0][-2:]==['pull','qwen:8b']
    assert m.ollama_action('qwen:8b','measure')['median']==20
    monkeypatch.setattr(m,'request',lambda *a,**k:{})
    with pytest.raises(ValueError,match='decode'):m.ollama_action('qwen:8b','measure')
    chosen=m.policy({'provider':'ollama','model':'qwen:8b'})
    with m.provider_runtime(chosen) as (base,name,info):
        assert name=='qwen:8b' and base=='http://127.0.0.1:12345'


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
    assert len(args)==10 and all('mcp_servers.t-local-worker.tools.' in v for v in args[1::2])
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
    def run_action(ui, operation, model, **kwargs):
        try: operation(None)
        except ValueError: pass
    original_helper=m.helper
    monkeypatch.setattr(m,'helper',lambda n:original_helper(n) if n=='t_model_display' else {'status':lambda:{'models':[]}} if n=='t_local_manager' else {'run_model_action':run_action})
    calls=[]
    monkeypatch.setattr(m,'install',lambda *a,**k:calls.append(a) or {})
    monkeypatch.setattr(m,'measure',lambda *a,**k:calls.append(a) or {})
    class UI:
        def __init__(self,choices):self.choices=iter(choices)
        def pick(self,*a,**k):return next(self.choices)
        def page(self,*a,**k):return 'enter'
        def done(self,*a):pass
        def restore(self):pass
        def raw(self):pass
    for action in ('install','download','measure'):
        choices=[('tools',m.DEFAULT_MODEL),action]+(['yes'] if action!='measure' else [])+['back','__back']
        assert m.select_model(UI(choices)) is None
    assert len(calls)==3
    assert m.select_model(UI([m.DEFAULT_MODEL]))=={'model':m.DEFAULT_MODEL,'provider':'lmstudio'}
    assert m.select_model(UI([('tools',m.DEFAULT_MODEL),'install','no','back','__back'])) is None
    monkeypatch.setattr(m,'measure',lambda *a,**kw:(_ for _ in ()).throw(ValueError('unavailable')))
    assert m.select_model(UI([('tools',m.DEFAULT_MODEL),'measure','back','__back'])) is None
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
    monkeypatch.setattr(m,'helper',lambda n:{'supervise':lambda *a:calls.append(a) or 7, 'launch_options':lambda *a:None})
    assert m.lead(['codex'],cwd='/repo',chosen={'mode':'automatic','on_cloud_failure':'local'})==7
    assert calls[0][2]=='/repo'
    assert m.lead(['codex'],cwd='/repo',chosen={'mode':'automatic','on_cloud_failure':'ask'})==7
    assert m.policy({'on_cloud_failure':'ask'})['on_cloud_failure']=='ask'
    monkeypatch.setattr(m, 'catalogue', lambda **kw: {'models': []})
    class UI:
        def __init__(self,choices):self.choices=iter(choices)
        def pick(self,*a,**kw):return next(self.choices)
    assert m.workflow_menu(UI(['on_cloud_failure','on_cloud_failure','local','back','back']))['on_cloud_failure']=='local'
    assert m.workflow_menu(UI(['on_cloud_failure','on_cloud_failure','local','back','mode','disabled','back']))['on_cloud_failure']=='pause'
    assert m.workflow_menu(UI(['on_cloud_failure','on_cloud_failure','ask','back','back']))['on_cloud_failure']=='ask'


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
    ui=make_ui(t_mod,['j','j','enter','j','enter','j','j','j','enter','q','q'],width,height)
    assert local.workflow_menu(ui)['on_cloud_failure']=='local'
    assert any('Pause' in ''.join(frame) for frame in ui.frames)


def test_special_runtime_verification_evidence(local, monkeypatch):
    m = local
    row = m.artifacts()[1]
    name = row['model']
    directory = m.model_directory(name)
    directory.mkdir(parents=True)
    (directory / 'weights.safetensors').write_bytes(b'123')
    receipt = directory / '.t-artifact.json'
    receipt.write_text(json.dumps({'model': name, 'revision': 'a'*40,
                                 'files': [{'name': 'weights.safetensors', 'size': 3}]}))
    machine = hardware()
    monkeypatch.setattr(m, 'helper', lambda n: {'hardware': lambda: machine})
    monkeypatch.setattr(m, 'lms', lambda: '/fake/lms')
    calls = []
    @contextmanager
    def runtime(*args,**kwargs):
        calls.append(args)
        yield 'http://fixture', 'alias', m.receipt(name)
    monkeypatch.setattr(m, 'managed_runtime', runtime)
    monkeypatch.setattr(m, 'request', lambda *a, **k: {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]})
    before = m.report(row, machine)
    assert before['fit'] is None and before['verification_allowed'] and not before['ready']
    assert m.install(name)['ready']
    assert calls == [(name, 'lmstudio', 32768, 'verification')]
    assert m.report(row, machine)['ready']
    assert not m.report(row, machine, 65536)['ready']
    assert not m.report(row, dict(machine, accelerator='Apple other'))['ready']
    assert m.report(row, dict(machine, ram_bytes=64*1024**3))['fit'] is False
    # Download-only never expands prior context evidence or starts inference.
    assert not m.install(name, True, 65536)['ready']
    assert len(calls) == 1
    monkeypatch.setattr(m, 'request', lambda *a, **k: {'output': [{'type': 'reasoning'}]})
    with pytest.raises(ValueError, match='no output text'):
        m.install(name)
    assert m.report(row, machine)['ready']
    assert m.receipt(name)['verified_context'] == 32768
    assert m.receipt(name).get('verification_error')
    assert (directory / 'weights.safetensors').read_bytes() == b'123'
    # Retrying or cancelling does not discard successful proof for unchanged files/context.
    @contextmanager
    def interrupted(*args,**kwargs):
        raise KeyboardInterrupt
        yield
    monkeypatch.setattr(m, 'managed_runtime', interrupted)
    with pytest.raises(KeyboardInterrupt):
        m.install(name)
    assert m.receipt(name)['verified_context'] == 32768


def test_special_runtime_exact_lease_and_failure_cleanup(local, monkeypatch):
    m = local; name = m.artifacts()[1]['model']; calls = []
    monkeypatch.setattr(m, 'receipt', lambda n: {'revision': 'a'*40})
    monkeypatch.setattr(m, 'request', lambda *a, **k: {})
    def lms(*args, **kwargs):
        calls.append(args)
        if args[0] == 'ls':
            return json.dumps([{'indexedModelIdentifier': name, 'modelKey': 'flash'}])
        if args[0] == 'ps':
            load = next(c for c in calls if c[0] == 'load')
            return json.dumps([{'identifier': load[load.index('--identifier')+1],
                               'indexedModelIdentifier': name, 'contextLength': 32768}])
        return ''
    monkeypatch.setattr(m, 'run_lms', lms)
    with m.runtime(name):
        pass
    assert [c[0] for c in calls] == ['ls', 'load', 'ps', 'unload']
    assert not any('guardrail' in arg for c in calls for arg in c)


@pytest.mark.parametrize('width,height', [(80,24), (44,20), (32,12)])
def test_model_overview_navigation_and_provenance(local, t_mod, monkeypatch, width, height):
    from test_tui import make_ui
    m = local; row = m.report(m.artifacts()[1], hardware())
    # No default detail dump. Context and the focused action stay in the viewport.
    ui = make_ui(t_mod, ['q'], width, height)
    assert m.model_menu(ui, row, 32768) is None
    frame = '\n'.join(ui.frames[0])
    assert 'Install and test' in frame
    assert 'https://' not in frame and 'Publisher' not in frame
    for captured in ui.frames:
        assert sum(t_mod._term_rows(line, width) for line in captured) <= height
    # Read-only drilldowns preserve focus and never call install.
    class UI:
        def __init__(self, choices):
            self.choices = iter(choices); self.pages = []; self.menus = []
        def pick(self, label, rows, **kwargs):
            self.menus.append((label, rows, kwargs)); return next(self.choices)
        def page(self, title, body, **kwargs):
            self.pages.append((title, body)); return 'q'
        def cols(self): return width
    row.update(installed=True, verification_error='Unsupported architecture', measurement={
        'median':2, 'min':1, 'max':3, 'metric':'tokens/s', 'context_tokens':32768, 'stale':True})
    ui = UI(['benchmarks', 'sources', 'artifact', 'back'])
    assert m.model_menu(ui, row, 32768) is None
    assert len(ui.pages) == 2
    assert 'Unsupported architecture' in '\n'.join(ui.pages[-1][1])
    assert 'Stale measurement' in '\n'.join(ui.pages[-1][1])
    assert all(t_mod._display_width(line) <= width for _, body in ui.pages for line in body)
    row['benchmarks'] = {}
    ui = UI(['benchmarks', 'sources', 'back'])
    m.model_menu(ui, row, 32768)
    assert 'No published' in '\n'.join(ui.pages[0][1])
    assert 'No published' in '\n'.join(ui.menus[1][2]['context'])
    ui.page = lambda *a, **k: 'abort'
    with pytest.raises(KeyboardInterrupt):
        m.model_page(ui, 'Title', ['body'])


def test_model_command_error_is_actionable(t_mod, monkeypatch, capsys):
    def failure(args):
        raise ValueError('Model loading was stopped due to insufficient system resources')
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {'command': failure})
    assert t_mod.cmd_config(None, SimpleNamespace(models_action='install')) == 1
    error = capsys.readouterr().err
    assert error.startswith('t config models: Model loading')
    assert 'Traceback' not in error


def test_download_cancel_and_artifact_lock(local,monkeypatch):
    import threading
    m=local;cancel=threading.Event();cancel.set()
    with pytest.raises(ValueError,match='cancelled'):m.download_command(['fake'],cancel)
    with pytest.raises(ValueError,match='cancelled'):
        with m.managed_runtime(m.DEFAULT_MODEL,'lmstudio',cancel=cancel):pass
    cancel.clear();calls=[]
    class Process:
        returncode=None
        def poll(self):return self.returncode
        def terminate(self):calls.append('terminate');self.returncode=-15
        def wait(self,**kw):return self.returncode
    proc=Process()
    monkeypatch.setattr(m.subprocess,'Popen',lambda *a,**kw:proc)
    monkeypatch.setattr(cancel,'wait',lambda t:True)
    with pytest.raises(ValueError,match='cancelled'):m.download_command(['fake'],cancel)
    assert calls==['terminate']
    proc.returncode=1
    with pytest.raises(ValueError,match='failed'):m.download_command(['fake'],cancel)
    proc.returncode=0;m.download_command(['fake'],cancel)
    directory=m.model_directory(m.DEFAULT_MODEL);directory.mkdir(parents=True)
    with (directory/'.t-install.lock').open('a') as lock:
        m.fcntl.flock(lock,m.fcntl.LOCK_EX|m.fcntl.LOCK_NB)
        with pytest.raises(ValueError,match='in progress'):m.install(m.DEFAULT_MODEL)


def test_model_actions_use_exact_instance_and_report_launch_failures(local,monkeypatch):
    m=local;row=m.report(m.artifact(m.DEFAULT_MODEL),hardware());row.update(ready=True,installed=True)
    calls=[];failure=[False]
    def rpc(action,**kw):
        if failure[0]:raise ValueError('still active')
        calls.append((action,kw))
    def open_app(provider):
        if failure[0]:raise ValueError('missing app')
        calls.append(('app',provider))
    monkeypatch.setattr(m,'helper',lambda n:{'status':lambda:{'models':[{'model':row['model'],'alias':'exact-instance','owned':True}]},'rpc':rpc} if n=='t_local_manager' else {'open_desktop':open_app} if n=='t_local_apps' else {'provider_menu':lambda ui,provider:calls.append(('guide',provider))})
    class UI:
        def __init__(self,choices):self.choices=iter(choices);self.pages=[]
        def pick(self,*a,**kw):return next(self.choices)
        def page(self,title,body,**kw):self.pages.append(title);return 'q'
    ui=UI(['guide','app','unload','back']);m.model_menu(ui,row,32768)
    assert ('release',{'alias':'exact-instance'}) in calls and 'Desktop app opened' in ui.pages
    failure[0]=True;ui=UI(['app','unload','back']);m.model_menu(ui,row,32768)
    assert ui.pages==['Could not open desktop app','Unload unavailable']


def test_ollama_readiness_requires_matching_proof(local,monkeypatch):
    m=local;name='qwen:8b';revision='sha256:fixture'
    monkeypatch.setattr(m,'ollama_revision',lambda name:revision)
    legacy={'hardware':hardware,'report':lambda:{'models':[{'model':name,'bytes':4,'fit':True,'score':None,'reason':'fit'}], 'benchmark':{}},'missing_bytes':lambda *a:0}
    monkeypatch.setattr(m,'helper',lambda name:legacy)
    path=m.measurement_path(name).with_suffix('.ready.json');path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'revision':revision,'context_tokens':32768,'hardware':hardware()}))
    assert m.catalogue()['models'][-1]['ready']
    assert not m.catalogue(context=65536)['models'][-1]['ready']
    revision='changed';assert not m.catalogue()['models'][-1]['ready']


def test_managed_runtime_lost_lease_never_claims_success(local,monkeypatch):
    class Failed(ManagedClientFixture):
        def __init__(self,*a,**kw):super().__init__(*a,**kw);self.error='manager connection lost'
    monkeypatch.setattr(local,'helper',lambda name:{'Client':Failed})
    with pytest.raises(ValueError,match='connection lost'):
        with local.managed_runtime(local.DEFAULT_MODEL,'lmstudio'):pass


def test_download_cancellation_kills_unresponsive_process(local,monkeypatch):
    import threading
    calls=[];cancel=threading.Event();monkeypatch.setattr(cancel,'wait',lambda t:True)
    class Process:
        returncode=None
        def poll(self):return self.returncode
        def terminate(self):calls.append('terminate')
        def kill(self):calls.append('kill');self.returncode=-9
        def wait(self,**kw):
            if kw:raise local.subprocess.TimeoutExpired('curl',5)
            calls.append('wait')
    monkeypatch.setattr(local.subprocess,'Popen',lambda *a,**kw:Process())
    with pytest.raises(ValueError,match='cancelled'):local.download_command(['curl'],cancel)
    assert calls==['terminate','kill','wait']


@pytest.mark.parametrize('unresponsive', [False, True])
def test_native_lead_stops_when_manager_lease_is_lost(local, monkeypatch, tmp_path, unresponsive):
    m = local
    calls = []
    worker = m.helper('t_local_worker')
    modules = {name: m.helper(name) for name in ('t_local_status', 't_workflow', 't_toml')}
    class LostLease(ManagedClientFixture):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.error = 'Shared manager connection lost'
    modules.update(t_local_models={'hardware': hardware}, t_local_manager={'Client': LostLease},
                   t_local_worker={'state_root': lambda: tmp_path, 'environment': worker['environment']})
    modules['t_usage'] = {'local_launch': lambda *a: None}
    monkeypatch.setattr(m, 'helper', lambda name: modules[name])
    class Process:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self):
            calls.append('terminate')
            if not unresponsive: self.returncode = -15
        def wait(self, **kwargs):
            if kwargs:
                assert kwargs['timeout'] == 5
                raise m.subprocess.TimeoutExpired('codex', 5)
            calls.append('reaped')
        def kill(self):
            calls.append('kill')
            self.returncode = -9
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *args, **kwargs: Process())
    chosen = m.policy({'mode': 'local-only'})
    with pytest.raises(ValueError, match='manager connection lost'):
        m.lead(['codex', '--', 'task'], chosen=chosen)
    assert 'terminate' in calls
    assert ('kill' in calls) == unresponsive
    assert ('reaped' in calls) == unresponsive
    # A recovery callback cannot report a successful handoff after losing its lease.
    with pytest.raises(ValueError, match='manager connection lost'):
        m.lead(['codex', '--', 'task'], chosen=chosen, runner=lambda *args, **kwargs: 0)


def test_cloud_retry_limits(local):
    assert local.policy()['cloud_retry_seconds'] == 300
    for value in (True, None, '300', 14, 301, 30.5):
        with pytest.raises(ValueError, match='cloud_retry_seconds'):
            local.policy({'cloud_retry_seconds': value})
    for seconds in (15, 30, 60, 120, 300):
        assert local.policy({'cloud_retry_seconds': seconds})['cloud_retry_seconds'] == seconds
    assert local.retry_label(60) == 'Up to 1 minute'
    assert local.retry_label(300) == 'Up to 5 minutes'
    assert local.retry_label(15) == 'Up to 15 seconds'


def test_cloud_recovery_staging_and_readiness(local, monkeypatch):
    views = []
    class UI:
        def __init__(self, choices): self.choices = iter(choices)
        def pick(self, title, rows, **kwargs):
            views.append((title, rows, kwargs))
            choice = next(self.choices)
            if choice == 'interrupt': raise KeyboardInterrupt
            return choice
    row = {'model': local.DEFAULT_MODEL, 'provider': 'lmstudio', 'ready': False, 'installed': True}
    monkeypatch.setattr(local, 'catalogue', lambda **kw: {'models': [row]})
    calls = []
    monkeypatch.setattr(local, 'model_menu', lambda *args: calls.append(args) or {'provider': 'lmstudio', 'model': 'chosen-model'})
    monkeypatch.setattr(local, 'select_model', lambda *args: {'provider': 'lmstudio', 'model': local.DEFAULT_MODEL})
    current = local.policy({'on_cloud_failure':'local', 'cloud_retry_seconds':30})
    local.cloud_failure_menu(UI(['readiness','model','cloud_retry_seconds','60','back']), current)
    assert current['cloud_retry_seconds'] == 60 and calls[0][1] is row
    assert any(v[2]['values'].get('readiness') == 'Downloaded · not verified' for v in views)
    local.cloud_failure_menu(UI(['cloud_retry_seconds',None,'on_cloud_failure',None,'back']), current)
    assert current['cloud_retry_seconds'] == 60 and current['on_cloud_failure'] == 'local'
    local.cloud_failure_menu(UI(['cloud_retry_seconds','','reset','back']), current)
    assert current == local.DEFAULT
    assert not Path(local.os.environ['XDG_CONFIG_HOME']).exists()
    with pytest.raises(KeyboardInterrupt) as exc:
        local.workflow_menu(UI(['on_cloud_failure','cloud_retry_seconds','120','interrupt']))
    assert exc.value.workflow_inference['cloud_retry_seconds'] == 120
    monkeypatch.setattr(local, 'catalogue', lambda **kw: {'models': []})
    local.cloud_failure_menu(UI(['readiness','back']), local.policy({'on_cloud_failure':'local'}))
    assert any(v[2]['values'].get('readiness', '').startswith('Model unavailable') for v in views)
    monkeypatch.setattr(local, 'catalogue', lambda **kw: (_ for _ in ()).throw(OSError('offline')))
    local.cloud_failure_menu(UI(['readiness','model','back']), local.policy({'on_cloud_failure':'local'}))
    assert views[-1][2]['values']['readiness'] == 'Status unavailable · Try again'


def test_pause_launch_compatibility(local, monkeypatch, capsys):
    def reject(*args): raise ValueError('unsupported launch')
    monkeypatch.setattr(local, 'helper', lambda name: {'launch_options': reject})
    calls=[]
    monkeypatch.setattr(local.subprocess, 'call', lambda args: calls.append(args) or 0)
    assert local.lead(['codex','resume','saved'], chosen={'on_cloud_failure':'pause'}) == 0
    assert calls == [['codex','resume','saved']]
    assert 'using native tool retries' in capsys.readouterr().err
    with pytest.raises(ValueError, match='unsupported launch'):
        local.lead(['codex','resume','saved'], chosen={'on_cloud_failure':'local'})
    monkeypatch.setattr(local, 'helper', lambda name: {'launch_options':lambda *args:None, 'supervise':lambda *args:9})
    assert local.lead(['codex'], chosen={'on_cloud_failure':'pause'}) == 9


def test_default_retry_launch_prefix_and_disabled_workflow(local, tmp_path):
    w = local.helper('t_workflow')
    doc = w['defaults']()
    path = w['config_path']()
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {k:v for k,v in local.os.environ.items() if not k.startswith('COV_CORE_')}
    result = local.subprocess.run([local.sys.executable, str(local.ROOT/'libexec/t_local_inference.py'),
                                   '--launch-prefix', str(tmp_path)], env=env, text=True, capture_output=True, check=True)
    assert not result.stdout
    for enabled in (True, False):
        doc['workflows'][doc['default']]['enabled'] = enabled
        path.write_text(w['encode'](doc))
        result = local.subprocess.run([local.sys.executable, str(local.ROOT/'libexec/t_local_inference.py'),
                                       '--launch-prefix', str(tmp_path)], env=env, text=True, capture_output=True, check=True)
        assert ('--lead' in result.stdout) is enabled


def test_time_limits_defaults_validation_and_scoped_reset(local):
    m=local
    assert [m.DEFAULT[k] for k in m.TIME_LIMITS] == [900,1800,300,300]
    for key in m.TIME_LIMITS:
        for value in (True,0,1801,'300'):
            with pytest.raises(ValueError):m.policy({key:value})
    with pytest.raises(ValueError,match='Maximum'):m.policy({'timeout_seconds':900,'max_timeout_seconds':300})
    class UI:
        def __init__(self,choices):self.choices=iter(choices);self.views=[]
        def pick(self,title,rows,**kwargs):self.views.append((title,rows,kwargs));return next(self.choices)
    chosen=m.policy({'mode':'automatic','context_tokens':8192,'timeout_seconds':300})
    ui=UI(['reset_time','back'])
    result=m.workflow_menu(ui,chosen)
    assert result['mode']=='automatic' and result['context_tokens']==8192 and result['timeout_seconds']==900
    assert dict(ui.views[0][1])['timeout_seconds']=='Default task limit'
    assert ui.views[0][2]['sections']['timeout_seconds']=='Time limits'
    ui=UI(['max_timeout_seconds','300','inactivity_timeout_seconds','60','queue_timeout_seconds','','back'])
    result=m.workflow_menu(ui)
    assert result['max_timeout_seconds']==1800 and result['inactivity_timeout_seconds']==60
    assert result['queue_timeout_seconds']==300 and '300' in ui.views[1][2]['disabled']
    ui=UI(['timeout_seconds','','back'])
    result=m.workflow_menu(ui,{'timeout_seconds':60,'max_timeout_seconds':300})
    assert result['timeout_seconds']==60 and '' in ui.views[1][2]['disabled']
    assert m.duration(10)=='10 sec'
