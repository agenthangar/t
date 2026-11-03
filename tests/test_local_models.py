"""Catalog tests never download weights or write the developer's settings."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_script


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    return load_script(Path(__file__).parents[1] / 'libexec/t_local_models.py', 'local_models_test')


def data(m):
    return {'schema': 1, 'fetched_at': m.time.time(), 'coverage': 'fixture',
            'models': [{'model': 'qwen3:8b', 'bytes': 5*m.GIB},
                       {'model': 'qwen3:30b', 'bytes': 20*m.GIB},
                       {'model': 'new:4b', 'bytes': 3*m.GIB}],
            'benchmarks': [{'release': '2026-01-01', 'source': 'https://example.test/new',
                            'scores': {'qwen3-30b-a3b-thinking': 90}},
                           {'release': '2025-01-01', 'source': 'https://example.test/old',
                            'scores': {'qwen3-8b-thinking': 60, 'qwen3-30b-a3b-thinking': 80}}]}


def machine(m, ram=32, disk=100):
    return dict(ram_bytes=ram*m.GIB, vram_bytes=ram*m.GIB, free_disk_bytes=disk*m.GIB,
                accelerator='Apple test', context_tokens=32768)


def test_rank_within_release_and_hardware(models):
    m=models
    result=m.report(data(m), machine(m,16))
    assert result['recommended']=='qwen3:8b'
    assert result['benchmark']['release']=='2025-01-01'
    assert [r['model'] for r in result['models']]==['qwen3:8b','new:4b','qwen3:30b']
    assert result['models'][1]['score'] is None
    assert result['models'][-1]['score']==80  # Never use its newer 90 in this comparison.
    assert m.report(data(m),machine(m,64))['recommended']=='qwen3:30b'
    assert m.report(data(m),machine(m,16), '2026-01-01')['recommended'] is None
    assert m.report(data(m),machine(m,16,1))['recommended'] is None
    assert m.report(data(m),dict(machine(m),vram_bytes=None))['recommended'] is None
    with pytest.raises(ValueError,match='available'): m.report(data(m),machine(m),'bad')
    assert m.score_for('qwen3:8b',{'qwen3-14b-thinking':100})==(None,None)


def test_parsers_and_schema_changes(models):
    m=models
    assert m.families('<li><a href="/library/qwen3"><span>tools</span></a></li>')==['qwen3']
    with pytest.raises(ValueError):m.families('<li>embedding</li>')
    html='<a href="/library/qwen3:8b">5GB - 6GB</a><a href="/library/qwen3:latest">5GB</a>'
    assert m.variants('qwen3',html)[0]['bytes']==6*10**9
    assert m.variants('qwen3','<a href="/library/qwen3:1b">800MB</a>')[0]['bytes']==800*10**6
    assert m.evaluations('model,a,b\nx,20,40\ny,nan,20\nz,missing,30\n',{'Coding':['a','b']})=={'x':30}
    for categories in ({}, {'Coding':['missing']}):
        with pytest.raises(ValueError):m.evaluations('model,a\nx,20\n',categories)


def test_cached_and_bundled_readonly(models, monkeypatch):
    m=models
    assert m.load()['models']  # Bundled snapshot, no cache writes.
    assert not m.cache_path().exists()
    m.cache_path().parent.mkdir(parents=True)
    m.cache_path().write_text('{broken')
    assert m.load()['models']
    d=data(m);d['fetched_at']=0
    m.cache_path().write_text(json.dumps(d))
    assert m.report(machine=machine(m))['stale']
    monkeypatch.setattr(m,'ROOT',m.cache_path().parent)
    m.cache_path().write_text('{}')
    with pytest.raises(ValueError,match='refresh'):m.load()


def fake_fetch(url):
    if url.endswith('/library'):
        return '<li><a href="/library/qwen3"><span>tools</span></a></li>'
    if url.endswith('/tags'):
        return '<a href="/library/qwen3:8b">5GB</a>'
    if 'git/trees' in url:
        return json.dumps({'tree':[{'path':'public/table_2025_01_01.csv'},{'path':'README.md'}]})
    if 'categories' in url:return '{"Coding":["coding"]}'
    return 'model,coding\nqwen3-8b-thinking,60\n'


def test_refresh_atomic_and_failure(models,monkeypatch):
    m=models
    monkeypatch.setattr(m,'fetch',fake_fetch)
    d=m.refresh()
    assert d['benchmarks'][0]['scores']=={'qwen3-8b-thinking':60}
    original=m.cache_path().read_bytes()
    def fail(url):
        if url.endswith('/tags'):raise OSError('offline')
        return fake_fetch(url)
    monkeypatch.setattr(m,'fetch',fail)
    with pytest.raises(ValueError,match='previous catalog kept'):m.refresh()
    assert m.cache_path().read_bytes()==original
    monkeypatch.setattr(m,'fetch',lambda url:'{"tree":[]}' if 'git/trees' in url else fake_fetch(url))
    with pytest.raises(ValueError,match='no tables'):m.refresh()
    monkeypatch.setattr(m,'fetch',fake_fetch)
    ticks=iter([0]+[100]*20)
    monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    with pytest.raises(ValueError,match='time budget'):m.refresh()


def test_fetch_size_bound(models,monkeypatch):
    m=models
    monkeypatch.setattr(m.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(b'hello'))
    assert m.fetch('https://example.test')=='hello'
    monkeypatch.setattr(m,'LIMIT',2)
    with pytest.raises(ValueError,match='exceeds'):m.fetch('https://example.test')


def test_hardware_and_probe(models,monkeypatch):
    m=models
    monkeypatch.setattr(m.sys,'platform','darwin')
    monkeypatch.setattr(m,'probe',lambda cmd:str(16*m.GIB) if 'hw.memsize' in cmd else 'Apple M4')
    assert m.hardware()['vram_bytes']==16*m.GIB
    monkeypatch.setattr(m,'probe',lambda cmd:'')
    assert m.hardware()['vram_bytes'] is None
    monkeypatch.setattr(m.sys,'platform','linux')
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**k:'MemTotal: 1000 kB' if str(p)=='/proc/meminfo' else original(p,*a,**k))
    monkeypatch.setattr(m.shutil,'which',lambda _: '/fake/nvidia-smi')
    monkeypatch.setattr(m,'probe',lambda cmd:'1000\n2000')
    h=m.hardware();assert h['ram_bytes']==1000*1024 and h['vram_bytes']==2000*1024**2
    monkeypatch.setattr(m,'probe',lambda cmd:'bad')
    assert m.hardware()['vram_bytes'] is None
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    assert m.hardware()['ram_bytes'] is None


def test_probe_failures(models,monkeypatch):
    m=models
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='ok\n'))
    assert m.probe(['fake'])=='ok'
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    assert m.probe(['fake'])==''
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    assert m.probe(['fake'])==''


def test_installed_blobs(models):
    m=models
    root=Path.home()/'.ollama/models'
    manifest=root/'manifests/registry.ollama.ai/library/test/1b'
    manifest.parent.mkdir(parents=True)
    blob=root/'blobs'/('sha256-'+'a'*64);blob.parent.mkdir()
    layer={'digest':'sha256:'+'a'*64,'size':3}
    manifest.write_text(json.dumps({'config':layer,'layers':[layer]}))
    assert m.missing_bytes('test:1b',100)==6
    blob.write_bytes(b'123')
    assert m.missing_bytes('test:1b',100)==0
    layer['digest']='../../other'
    manifest.write_text(json.dumps({'config':layer,'layers':[]}))
    assert m.missing_bytes('test:1b',100)==100


def test_commands(models,t_mod,monkeypatch,capsys):
    m=models
    monkeypatch.setattr(m,'load',lambda:data(m))
    monkeypatch.setattr(m,'hardware',lambda:machine(m,16))
    monkeypatch.setattr(m,'refresh',lambda:data(m))
    for action in ['list','refresh']:
        args=SimpleNamespace(models_action=action,json=True,benchmark=None)
        assert m.run(args)==0
        assert json.loads(capsys.readouterr().out)['recommended']=='qwen3:8b'
        args.json=False
        assert m.run(args)==0
        assert 'Best evaluated fit: qwen3:8b' in capsys.readouterr().out
        argv=['config','models',action,'--json']
        assert t_mod._command_readonly(argv)==(action=='list')
        parsed=t_mod.build_parser().parse_args(t_mod._canonical_argv(argv))
        assert parsed.models_action==action
        assert t_mod.main(['config','models',action,'--help'])==0
        assert 't help config models' in capsys.readouterr().out
    monkeypatch.setattr(t_mod.runpy,'run_path',lambda *a,**kw:{'command':lambda args:37})
    assert t_mod.cmd_config(None,parsed)==37
    monkeypatch.setattr(m,'load',lambda:(_ for _ in ()).throw(ValueError('offline')))
    assert m.run(SimpleNamespace(models_action='list'))==1
    monkeypatch.setattr(m,'load',lambda:(_ for _ in ()).throw(KeyboardInterrupt()))
    assert m.run(SimpleNamespace(models_action='list'))==130


def test_picker_refresh_custom_and_offline(models,monkeypatch):
    m=models
    d=data(m);d['fetched_at']=0
    monkeypatch.setattr(m,'load',lambda:d)
    monkeypatch.setattr(m,'hardware',lambda:machine(m,16))
    calls=[]
    monkeypatch.setattr(m,'refresh',lambda:calls.append('refresh') or data(m))
    class Dialog:
        notices=[]
        @contextmanager
        def suspended(self):yield
        def notice(self,value):self.notices.append(value)
        def choose(self,*args):return self.choices.pop(0)
    dialog=Dialog();dialog.choices=['refresh','qwen3:8b']
    assert m.choose(dialog)=='qwen3:8b' and len(calls)==2
    assert 'Eval: qwen3-8b-thinking' in dialog.notices[-1]
    monkeypatch.setattr(m,'refresh',lambda:(_ for _ in ()).throw(OSError('offline')))
    dialog.choices=['refresh','custom']
    assert m.choose(dialog)=='custom'
    assert 'offline' in dialog.notices
