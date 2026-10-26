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
    for value in ['bad', '[]', '{"schema":1}', 'x'*(v.CACHE_LIMIT+1)]:
        p.write_text(value); assert v.read(p)=={}
    p.write_bytes(b'\xff'); assert v.read(p)=={}

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

def test_first_error_never_becomes_previous_success(monkeypatch):
    now=[100000]
    monkeypatch.setattr(v.time,'time',lambda:now[0])
    monkeypatch.setattr(v,'collect',lambda:(['offline'],True))
    v.check(); now[0]+=v.RETRY+1; v.check()
    data=v.read(v.cache_path())
    assert data['last_success']==[] and data['lines']==['offline']

def test_help_documents_opt_in_and_disable(t_mod):
    help_text=t_mod._root_help()
    assert 'export T_VULNERABILITY_CHECK=1' in help_text
    assert 'unset T_VULNERABILITY_CHECK' in help_text

def test_cache_rejects_special_files_and_symlinks(tmp_path):
    import os
    fifo=tmp_path/'fifo'
    os.mkfifo(fifo)
    assert v.read(fifo)=={}
    link=tmp_path/'link'
    link.symlink_to(fifo)
    assert v.read(link)=={}
    assert v.read(tmp_path)=={}

def test_cache_read_stays_bounded_if_file_grows(tmp_path,monkeypatch):
    p=tmp_path/'growing'
    p.write_text('x'*(v.CACHE_LIMIT+1))
    monkeypatch.setattr(v.os,'fstat',lambda fd:SimpleNamespace(st_mode=0o100600,st_size=1))
    assert v.read(p)=={}


def sample(name='eslint',ecosystem='npm',version='8.0.0',binary='eslint',path='/repo/node_modules/eslint/bin.js'):
    return {'binary':binary,'path':path,'provenance':['repo api package-lock + installed metadata'],
            'name':name,'ecosystem':ecosystem,'version':version,'status':'mapped'}


def advisory(name='eslint',ecosystem='npm'):
    return {'id':'GHSA-aaaa-bbbb-cccc','affected':[{'package':{'name':name,'ecosystem':ecosystem},
            'ranges':[{'type':'SEMVER','events':[{'introduced':'0'},{'fixed':'8.1.0'}]}]}]}


def test_matching_delegates_version_semantics_to_osv_and_verifies_identity():
    for identity in [('npm','eslint','8.0.0'),('PyPI','some-tool','1.0rc1'),('crates.io','ripgrep','14.0.0')]:
        a=advisory(identity[1],identity[0])
        a['affected'][0]['ecosystem_specific']={'os':['Windows'],'arch':['x86']}
        result=v.evaluate(identity,[a])[0]
        assert 'OSV version match' in result and 'fixed' in result and 'Windows' in result
        assert 'https://osv.dev/vulnerability/GHSA-' in result
    assert 'No known findings' in v.evaluate(('npm','eslint','8.0.0'),[])[0]
    assert 'No known findings' in v.evaluate(('npm','eslint','8.0.0'),[{**advisory(),'withdrawn':'2026-01-01'}])[0]
    assert v.evaluate(('PyPI','some-tool','1.0'),[advisory('Some_Tool','PyPI')])
    for bad in [{},None,advisory('other'),{**advisory(),'affected':None}]:
        with pytest.raises(ValueError):v.evaluate(('npm','eslint','8.0.0'),[bad])
    a=advisory();a['affected'][0].pop('ranges');a['affected'][0]['versions']=['8.0.0']
    assert 'listed versions' in v.evaluate(('npm','eslint','8.0.0'),[a])[0]


def test_inventory_query_dedup_and_unsupported_never_sent(monkeypatch):
    rows=[sample(),sample(binary='eslint2',path='/other/bin'),sample('black','PyPI','23.0','black')]
    rows.append({**sample('private'), 'status':'unsupported','reason':'private source'})
    monkeypatch.setattr(v,'inventory',lambda:(rows,[]))
    calls=[]
    monkeypatch.setattr(v,'fetch',lambda identity:calls.append(identity) or [])
    lines,error=v.collect()
    assert not error and len(calls)==2
    assert ('npm','eslint','8.0.0') in calls
    assert any('unsupported/unknown' in line for line in lines)
    assert any('installed 23.0' in line for line in lines)


def test_offline_and_bad_response_cached_honestly(monkeypatch):
    monkeypatch.setattr(v,'inventory',lambda:([sample()],[]))
    for error in [OSError(),v.http.client.IncompleteRead(b'partial'),ValueError()]:
        monkeypatch.setattr(v,'fetch',lambda _,error=error:(_ for _ in ()).throw(error))
        data=v.check(force=True)
        assert data['error'] and data['expires']-data['checked']==v.RETRY
        assert any('unavailable' in line for line in data['lines'])
        assert not any('No known findings' in line for line in data['lines'])


def test_budgets_cancel_and_report_limit(monkeypatch):
    monkeypatch.setattr(v,'inventory',lambda:([sample()],[]))
    monkeypatch.setattr(v,'fetch',lambda _:pytest.fail('must not request'))
    assert v.collect(cancelled=lambda:True)[1]
    monkeypatch.setattr(v,'MAX_QUERIES',0)
    assert 'budget' in v.collect()[0][0]
    monkeypatch.setattr(v,'inventory',lambda:([],['unknown']*601))
    lines,error=v.collect();assert error and len(lines)==300
    monkeypatch.setattr(v,'inventory',lambda:(_ for _ in ()).throw(ValueError()))
    assert v.check(force=True)['error']


def test_request_contains_only_package_identity(monkeypatch):
    from io import BytesIO
    seen=[]
    def request(req,timeout):
        seen.append((req,timeout));return BytesIO(b'{"vulns":[]}')
    monkeypatch.setattr(v.urllib.request,'urlopen',request)
    assert v.fetch(('PyPI','black','23.1.0'))==[]
    assert json.loads(seen[0][0].data)=={'package':{'ecosystem':'PyPI','name':'black'},'version':'23.1.0'}
    assert seen[0][1]==8 and seen[0][0].full_url=='https://api.osv.dev/v1/query'
    assert 'Authorization' not in seen[0][0].headers


@pytest.mark.parametrize('payload',[b'[]',b'{"next_page_token":"next"}',b'{"vulns":{}}',b'x'*1048577])
def test_fetch_incomplete_or_invalid(monkeypatch,payload):
    from io import BytesIO
    monkeypatch.setattr(v.urllib.request,'urlopen',lambda *a,**k:BytesIO(payload))
    with pytest.raises(ValueError):v.fetch(('npm','eslint','8.0.0'))


def test_schema_change_invalidates_cloudflared_only_cache():
    path=v.cache_path();path.parent.mkdir(parents=True)
    v.write(path,{'schema':1,'checked':1,'expires':9999999999,'lines':['old cloudflared result']})
    assert v.read(path)=={}


def test_startup_announces_only_changed_results(monkeypatch,capsys):
    monkeypatch.setattr(v,'collect',lambda:(['tool: OSV version match finding'],False))
    v.check();v.startup()
    assert 'version match finding' in capsys.readouterr().err
    v.startup()
    assert 'No changed advisory results' in capsys.readouterr().err
    monkeypatch.setattr(v,'collect',lambda:(['tool: OSV version match changed'],False))
    v.check(force=True);v.startup()
    assert 'version match changed' in capsys.readouterr().err


def test_long_source_details_are_explicitly_truncated():
    a=advisory()
    a['affected'][0]['ranges']=[{'type':'ECOSYSTEM','events':[{'fixed':str(n)} for n in range(100)]}]
    a['affected'][0]['ecosystem_specific']={'platforms':['Windows','Linux']*50}
    line=v.evaluate(('npm','eslint','8.0.0'),[a])[0]
    assert line.count('[truncated; see source]')==2
    assert line.endswith('https://osv.dev/vulnerability/GHSA-aaaa-bbbb-cccc')


def test_long_provenance_keeps_actionable_link_and_reports_truncation(monkeypatch):
    item=sample();item['provenance']=['\u6f22'*180]
    monkeypatch.setattr(v,'inventory',lambda:([item],[]))
    monkeypatch.setattr(v,'fetch',lambda _: [advisory()])
    lines,_=v.collect()
    assert '[truncated; see source]' in lines[0]
    assert lines[0].endswith('https://osv.dev/vulnerability/GHSA-aaaa-bbbb-cccc')


def test_startup_does_not_reannounce_reordered_results(monkeypatch,capsys):
    monkeypatch.setattr(v,'collect',lambda:(['first','second'],False))
    v.check();v.startup();capsys.readouterr()
    monkeypatch.setattr(v,'collect',lambda:(['second','first'],False))
    v.check(force=True);v.startup()
    assert 'No changed advisory results' in capsys.readouterr().err
