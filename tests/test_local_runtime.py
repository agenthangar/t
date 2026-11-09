"""Provider protocol fixtures do not contact installed runtimes or load weights."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import urllib.error
import pytest
from conftest import load_script

@pytest.fixture
def runtime(monkeypatch):
    m = load_script(Path(__file__).parents[1]/'libexec/t_local_runtime.py','runtime_test')
    monkeypatch.setattr(m,'helper',lambda name:{'hardware':lambda:{'ram_bytes':128*m.GIB,'accelerator':'Apple test'}})
    return m

@pytest.fixture
def provider(runtime,tmp_path,monkeypatch):
    m=runtime
    settings={'memory_percent':80,'idle_seconds':300,'providers':{p:{'mode':'desktop','url':url} for p,url in [('lmstudio','http://127.0.0.1:1234'),('ollama','http://127.0.0.1:11434')]}}
    p=m.Providers(tmp_path,settings)
    monkeypatch.setattr(m,'available_memory',lambda:100*m.GIB)
    catalog=[{'indexedModelIdentifier':'artifact','modelKey':'key'}]
    helper={'receipt':lambda name:{'revision':'revision'},'report':lambda *a:{'memory_bytes':10*m.GIB},'artifact':lambda n:{},'run_lms':lambda *a:json.dumps(catalog)}
    monkeypatch.setattr(m,'helper',lambda n:helper)
    loaded=[];calls=[]
    def request(base,path,payload=None,**kw):
        calls.append((base,path,payload))
        if path=='/v1/models':return {}
        if path=='/api/v1/models':return {'models':[{'key':'key','loaded_instances':list(loaded)}]}
        if path=='/api/v1/models/load':
            loaded.append({'id':'alias','config':{'context_length':payload['context_length']}})
            return {'instance_id':'alias'}
        if path=='/api/v1/models/unload':loaded.clear();return {}
        if path=='/api/version':return {'version':'fixture'}
        if path=='/api/tags':return {'models':[{'name':'artifact','digest':'revision','size':10*m.GIB}]}
        if path=='/api/show':return {'capabilities':['tools']}
        if path=='/api/ps':return {'models':list(loaded)}
        if path=='/api/generate':
            loaded.clear()
            if payload['keep_alive']:
                loaded.append({'name':'artifact','digest':'revision','context_length':payload['options']['num_ctx']})
            return {}
        raise AssertionError(path)
    monkeypatch.setattr(m,'request',request)
    return p,loaded,calls,catalog,request


def choice(provider='lmstudio',context=32768):
    return {'provider':provider,'model':'artifact','context_tokens':context}

@pytest.mark.parametrize('url',['https://127.0.0.1:1','http://localhost:1','http://example.com:1','http://u:p@127.0.0.1:1','http://127.0.0.1','http://127.0.0.1:1/path','http://127.0.0.1:1?q=x','http://127.0.0.1:1#x','http://127.0.0.1:99999'])
def test_invalid_endpoint(runtime,url):
    with pytest.raises(ValueError):runtime.local_url(url)

def test_endpoint_and_memory_probes(runtime,monkeypatch):
    m=runtime
    assert m.local_url('http://[::1]:1234/')=='http://[::1]:1234'
    monkeypatch.setattr(m.sys,'platform','linux')
    monkeypatch.setattr(m.Path,'read_text',lambda *a:'MemAvailable: 123 kB\n')
    assert m.available_memory()==123*1024
    monkeypatch.setattr(m.Path,'read_text',lambda *a:'bad');assert m.available_memory() is None
    monkeypatch.setattr(m.Path,'read_text',lambda *a:(_ for _ in ()).throw(OSError()));assert m.available_memory() is None
    monkeypatch.setattr(m.sys,'platform','darwin')
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'page size of 16384 bytes\nPages free: 1.\nPages inactive: 2.\nPages speculative: 3.')
    assert m.available_memory()==6*16384
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'bad');assert m.available_memory() is None
    monkeypatch.setattr(m.sys,'platform','other');assert m.available_memory() is None
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'1024\n2048\n');assert m.gpu_available()==2*m.GIB
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'bad');assert m.gpu_available() is None
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:(_ for _ in ()).throw(OSError()));assert m.gpu_available() is None

def test_exact_reuse_unload_and_expiry(runtime,provider,monkeypatch):
    p,loaded,calls,_,_=provider
    item=p.get(choice());assert item['owned'] and item['revision']=='revision'
    assert p.get(choice()) is item
    assert sum(path.endswith('/load') for _,path,_ in calls)==1
    item['last_used']=0;p.expire(['alias']);assert loaded
    p.expire();assert not loaded and not p.instances
    item=p.get(choice());loaded.clear()
    with pytest.raises(ValueError,match='disappeared'):p.get(choice())
    assert not p.instances
    item=p.get(choice());p.close();assert not loaded

def test_borrowed_instances_are_never_unloaded(runtime,provider):
    p,loaded,calls,_,_=provider
    loaded.append({'id':'external','config':{'context_length':32768}})
    item=p.get(choice());assert not item['owned'] and item['alias']=='external'
    with pytest.raises(ValueError,match='provider app'):p.unload(item)
    p.close();assert loaded
    item['last_used']=0;p.expire();assert not p.instances and loaded
    assert not any(path.endswith('/unload') for _,path,_ in calls)

def test_catalog_identity_and_capability_rejections(runtime,provider,monkeypatch):
    p,_,_,catalog,request=provider
    catalog.append(dict(catalog[0]))
    with pytest.raises(ValueError,match='uniquely'):p.describe(choice())
    catalog.pop();catalog[0]['modelKey']='other'
    with pytest.raises(ValueError,match='exact artifact'):p.describe(choice())
    monkeypatch.setattr(runtime,'request',lambda base,path,*a,**k:{'models':[]} if path=='/api/tags' else request(base,path,*a,**k))
    with pytest.raises(ValueError,match='Exact Ollama'):p.describe(choice('ollama'))
    monkeypatch.setattr(runtime,'request',lambda base,path,*a,**k:{'remote_model':'cloud','capabilities':['tools']} if path=='/api/show' else request(base,path,*a,**k))
    with pytest.raises(ValueError,match='local model'):p.describe(choice('ollama'))

def test_ollama_ownership_and_context(runtime,provider,monkeypatch):
    p,loaded,calls,_,_=provider
    p.ollama_private=True
    item=p.get(choice('ollama'));assert item['owned'] and p.present(item)
    p.unload(item);assert not loaded
    p.ollama_private=False
    item=p.get(choice('ollama'));assert not item['owned']
    p.instances.clear();borrowed=p.get(choice('ollama'));assert not borrowed['owned']
    p.instances.clear()
    monkeypatch.setattr(p, 'room', lambda description: None)
    with pytest.raises(runtime.WaitingForMemory,match='outside t'):p.get(choice('ollama',8192))

def test_budget_unknown_memory_lru_and_gpu(runtime,provider,monkeypatch):
    p,loaded,calls,_,_=provider;m=runtime
    item=p.get(choice());desc=p.describe(choice())
    monkeypatch.setattr(p,'observe',lambda:[])
    p.machine['ram_bytes']=0
    with pytest.raises(m.WaitingForMemory,match='physical memory'):p.room(desc)
    p.machine['ram_bytes']=128*m.GIB
    p.external=[{'memory_bytes':None}]
    with pytest.raises(m.WaitingForMemory,match='unknown'):p.room(desc)
    p.external=[]
    monkeypatch.setattr(m,'available_memory',lambda:1)
    with pytest.raises(m.WaitingForMemory):p.get(choice(context=8192),['alias'])
    assert p.instances
    with pytest.raises(m.WaitingForMemory):p.get(choice(context=8192))
    assert not p.instances and not loaded
    monkeypatch.setattr(m,'available_memory',lambda:100*m.GIB)
    p.machine.update(accelerator='NVIDIA',vram_bytes=16*m.GIB)
    monkeypatch.setattr(m,'gpu_available',lambda:2*m.GIB)
    with pytest.raises(m.WaitingForMemory,match='GPU'):p.room(desc)
    monkeypatch.setattr(m,'gpu_available',lambda:16*m.GIB);p.room(desc)

def test_external_observation_and_failed_unload(runtime,provider,monkeypatch):
    p,loaded,_,_,request=provider
    loaded.append({'id':'other','config':{},'name':'other','size':123})
    external=p.observe();assert any(x['memory_bytes'] is None for x in external)
    loaded.clear();item=p.get(choice())
    monkeypatch.setattr(runtime,'request',lambda base,path,*a,**k:{} if path.endswith('/unload') else request(base,path,*a,**k))
    with pytest.raises(ValueError,match='confirm'):p.unload(item)
    assert p.instances

def test_lm_endpoint_modes(runtime,provider,monkeypatch):
    p,_,_,_,request=provider
    monkeypatch.setattr(runtime,'request',lambda *a,**k:(_ for _ in ()).throw(ConnectionRefusedError()))
    with pytest.raises(ValueError,match='stopped'):p.endpoint('lmstudio',True)
    p.settings['providers']['lmstudio']['mode']='headless'
    calls=[];monkeypatch.setattr(runtime,'helper',lambda n:{'run_lms':lambda *a:calls.append(a) or json.dumps({'isDaemon':True})})
    assert p.endpoint('lmstudio',True).endswith(':1234') and calls
    p.settings['providers']['lmstudio']['url']='http://127.0.0.1:9999'
    with pytest.raises(ValueError,match='configured'):p.endpoint('lmstudio',True)

def test_private_ollama_environment(runtime,provider,monkeypatch,tmp_path):
    p,_,_,_,_=provider
    p.settings['providers']['ollama']['mode']='headless'
    assert p.endpoint('ollama') is None
    calls=[]
    monkeypatch.setattr(runtime,'helper',lambda n:{'executable':lambda n:'/fake/ollama','environment':lambda home:{'HOME':str(home)},'stop':lambda proc:calls.append('stop')})
    monkeypatch.setattr(runtime.subprocess,'Popen',lambda argv,**kw:calls.append(kw) or SimpleNamespace(pid=100,poll=lambda:None))
    base=p.endpoint('ollama',True)
    assert p.endpoint('ollama',True)==base
    assert calls[0]['env']['OLLAMA_NUM_PARALLEL']=='1'
    assert calls[0]['env']['OLLAMA_NO_CLOUD']=='1'
    assert calls[0]['env']['OLLAMA_MODELS']==str(Path.home()/'.ollama/models')
    p.close();assert calls[-1]=='stop'


@pytest.mark.parametrize('value',[None,False,{},'http://127.0.0.1:1234\n'])
def test_endpoint_rejects_nonstring_and_control_characters(runtime,value):
    with pytest.raises(ValueError):runtime.local_url(value)


def test_unknown_provider_memory_blocks_admission(runtime,provider,monkeypatch):
    p,_,_,_,_=provider
    def unavailable(*a,**kw):raise urllib.error.URLError(TimeoutError())
    monkeypatch.setattr(runtime,'request',unavailable)
    assert all(x['memory_bytes'] is None for x in p.observe())
    with pytest.raises(runtime.WaitingForMemory,match='unknown'):p.room({'estimate':1})
    monkeypatch.setattr(runtime,'request',lambda *a,**kw:(_ for _ in ()).throw(urllib.error.URLError(ConnectionRefusedError())))
    assert p.observe()==[]


def test_lm_headless_refuses_gui_fallback_and_auth_error(runtime,provider,monkeypatch):
    p,_,_,_,_=provider
    p.settings['providers']['lmstudio']['mode']='headless'
    monkeypatch.setattr(runtime,'request',lambda *a,**kw:(_ for _ in ()).throw(ConnectionRefusedError()))
    monkeypatch.setattr(runtime,'helper',lambda name:{'run_lms':lambda *a:'{"isDaemon":false}'})
    with pytest.raises(ValueError,match='llmster'):p.endpoint('lmstudio',True)
    monkeypatch.setattr(runtime,'request',lambda *a,**kw:(_ for _ in ()).throw(urllib.error.HTTPError('url',401,'auth',{},None)))
    with pytest.raises(ValueError,match='authentication'):p.endpoint('lmstudio',True)
