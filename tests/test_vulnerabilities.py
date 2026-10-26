import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

SPEC = importlib.util.spec_from_file_location('vulnerabilities', Path(__file__).resolve().parents[1] / 'libexec/t_vulnerabilities.py')
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    monkeypatch.setenv('T_VULNERABILITY_CHECK', '1')


def test_disabled(monkeypatch, capsys):
    monkeypatch.delenv('T_VULNERABILITY_CHECK')
    monkeypatch.setattr(v, 'cache_path', lambda: pytest.fail('disabled cache access'))
    v.startup()
    v.check()
    assert not capsys.readouterr().err


@pytest.mark.parametrize('installed,expr,expected', [('2026.10.1','< 2026.10.2',True), ('2026.10.2','< 2026.10.2',False), ('2026.10.10','>=2026.10.2, <2027.0.0',True), ('2023.3.0','<=2023.3.0',True), ('2023.3.1','=2023.3.0',False), ('2023.3.1','>2023.3.0',True)])
def test_matching(installed,expr,expected):
    assert v.matches(installed,expr) is expected


@pytest.mark.parametrize('bad', ['v2026.10.2', '2026.10.2-rc1', '', None])
def test_unknown_version(bad):
    with pytest.raises(ValueError): v.version(bad)


def advisory(ecosystem='Linux', affected='<2026.10.2', fixed='2026.10.2'):
    return {'ghsa_id':'GHSA-aaaa-bbbb-cccc', 'vulnerabilities':[{'package':{'ecosystem':ecosystem,'name':'cloudflared'}, 'vulnerable_version_range':affected,'patched_versions':fixed}]}


def test_platforms_and_boundaries():
    assert 'vendor recommends' in v.evaluate('2026.10.1','Darwin',[])[0]
    assert 'no known findings' in v.evaluate('2026.10.2','Linux',[])[0]
    assert 'fixed 2026.10.2' in v.evaluate('2026.10.1','Linux',[advisory()])[-1]
    assert 'no known findings' in v.evaluate('2026.10.2','Linux',[advisory('windows 32-bit')])[0]
    assert 'no known findings' in v.evaluate('2026.10.2','Darwin',[advisory()])[0]
    assert 'coverage unknown' in v.evaluate('2026.10.2','Linux',[advisory('')])[0]
    for a in [advisory(affected='*'), advisory(fixed='bad')]:
        assert 'coverage unknown' in v.evaluate('2026.10.2','Linux',[a])[0]
    assert 'coverage unknown' in v.evaluate('2026.10.2','Linux',[{'ghsa_id':'GHSA-aaaa-bbbb-cccc'}])[0]
    a=advisory(); a['withdrawn_at']='date'
    assert 'no known findings' in v.evaluate('2026.10.1','Windows',[a])[0]
    with pytest.raises(ValueError): v.evaluate('2026.10.2','Linux',[{}])
    a=advisory(); a['vulnerabilities']=[None]
    with pytest.raises(ValueError): v.evaluate('2026.10.2','Linux',[a])


def test_collect(monkeypatch):
    monkeypatch.setattr(v.platform,'system',lambda:'Windows')
    assert 'Unsupported' in v.collect()[0][0]
    monkeypatch.setattr(v.platform,'system',lambda:'Linux')
    monkeypatch.setattr(v.shutil,'which',lambda _:None)
    assert 'not found' in v.collect()[0][0]
    monkeypatch.setattr(v.shutil,'which',lambda _:'/fake/cloudflared')
    monkeypatch.setattr(v.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='unexpected',returncode=0))
    assert v.collect()[1]
    monkeypatch.setattr(v.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='cloudflared version 2026.10.1 (built today)\n',returncode=0))
    monkeypatch.setattr(v,'fetch',lambda:[])
    assert not v.collect()[1]
    monkeypatch.setattr(v,'fetch',lambda:(_ for _ in ()).throw(OSError()))
    lines,error=v.collect()
    assert error and 'vendor recommends' in lines[0] and 'unavailable' in lines[-1]


def test_cache_repeated_startup_and_offline(monkeypatch,capsys):
    now=[100000]
    monkeypatch.setattr(v.time,'time',lambda:now[0])
    monkeypatch.setattr(v,'collect',lambda:(['finding'],False))
    v.check(); v.check()
    calls=[]
    monkeypatch.setattr(v.subprocess,'Popen',lambda *a,**k:calls.append((a,k)))
    v.startup(); v.startup()
    assert not calls and 'cached' in capsys.readouterr().err
    now[0]+=v.DAY+1
    v.startup()
    assert len(calls)==1 and calls[0][1]['start_new_session']
    assert 'stale' in capsys.readouterr().err
    monkeypatch.setattr(v,'collect',lambda:(_ for _ in ()).throw(OSError()))
    v.check()
    data=v.read(v.cache_path())
    assert 'finding' in data['lines'] and data['expires']==now[0]+v.RETRY


def test_pending_errors_lock(monkeypatch,capsys):
    monkeypatch.setattr(v.subprocess,'Popen',lambda *a,**k:(_ for _ in ()).throw(OSError()))
    v.startup()
    assert 'unavailable' in capsys.readouterr().err
    monkeypatch.setattr(v.fcntl,'flock',lambda *a:(_ for _ in ()).throw(BlockingIOError()))
    v.check()
    assert not v.cache_path().exists()


def test_cache_validation(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME','relative')
    monkeypatch.setenv('HOME',str(tmp_path))
    assert v.cache_path()==tmp_path/'.cache/t/vulnerabilities.json'
    p=tmp_path/'bad'
    for value in ['bad', '[]', '{"schema":1}', 'x'*131073]:
        p.write_text(value); assert v.read(p)=={}
    p.write_bytes(b'\xff'); assert v.read(p)=={}


@pytest.mark.parametrize('payload,headers', [(b'{}',{}),(b'[]',{'Link':'next'}),(b'x'*1048577,{}),(b'[1]',{})])
def test_fetch_invalid(monkeypatch,payload,headers):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,n): return payload
    r=Response(); r.headers=headers
    monkeypatch.setattr(v.urllib.request,'urlopen',lambda *a,**k:r)
    with pytest.raises(ValueError): v.fetch()


def test_fetch_success(monkeypatch):
    from io import BytesIO
    response=BytesIO(b'[]'); response.headers={}
    monkeypatch.setattr(v.urllib.request,'urlopen',lambda *a,**k:response)
    assert v.fetch()==[]


def test_bad_package_and_offline_current(monkeypatch):
    a=advisory(); a['vulnerabilities'][0]['package']='bad'
    with pytest.raises(ValueError): v.evaluate('2026.10.2','Linux',[a])
    monkeypatch.setattr(v.platform,'system',lambda:'Linux')
    monkeypatch.setattr(v.shutil,'which',lambda _:'/fake/cloudflared')
    monkeypatch.setattr(v.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='cloudflared version 2026.10.2',returncode=0))
    monkeypatch.setattr(v,'fetch',lambda:(_ for _ in ()).throw(OSError()))
    lines,error=v.collect()
    assert error and not any('no known findings' in line for line in lines)


@pytest.mark.parametrize('terminal,enabled,expected', [(False,True,0),(True,False,0),(True,True,1)])
def test_bare_startup_boundary(t_mod,monkeypatch,terminal,enabled,expected):
    monkeypatch.setattr(t_mod.sys.stdin,'isatty',lambda:terminal)
    monkeypatch.setattr(t_mod.sys.stdout,'isatty',lambda:terminal)
    monkeypatch.setenv('T_VULNERABILITY_CHECK','1' if enabled else '0')
    calls=[]
    monkeypatch.setattr(t_mod.runpy,'run_path',lambda path:{'startup':lambda:calls.append(path)})
    monkeypatch.setattr(t_mod,'Config',lambda:None)
    monkeypatch.setattr(t_mod,'_onboarding',lambda:{'needed':lambda *a:False})
    monkeypatch.setattr(t_mod,'_onboarding_start',lambda cfg:0)
    assert t_mod._bare_entry()==0
    assert len(calls)==expected


def test_concurrent_workers_and_repeated_failures(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    calls=[]
    def collect():
        calls.append(1); entered.set(); assert release.wait(3)
        return ['original finding'],False
    monkeypatch.setattr(v,'collect',collect)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(v.check)
        assert entered.wait(3)
        pool.submit(v.check).result(timeout=3)
        release.set(); first.result(timeout=3)
    assert len(calls)==1
    now=v.time.time()+v.DAY+1
    monkeypatch.setattr(v.time,'time',lambda:now)
    monkeypatch.setattr(v,'collect',lambda:(['offline'],True))
    v.check(); now+=v.RETRY+1; v.check()
    assert v.read(v.cache_path())['lines']==['offline','Previous results (stale):','original finding']


def test_null_advisory_id():
    with pytest.raises(ValueError):
        v.evaluate('2026.10.2','Linux',[{'ghsa_id':None}])
