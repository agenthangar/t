import importlib.util
from pathlib import Path
from threading import Event
import pytest

SPEC=importlib.util.spec_from_file_location('service',Path(__file__).resolve().parents[1]/'libexec/t_advisory_service.py')
s=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(s)

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path/'cache'))
    monkeypatch.setenv('HOME',str(tmp_path/'home'))


def test_status_disabled_is_readonly():
    assert 'disabled; worker stopped' in s.status()
    assert not s.directory().exists()


def test_enable_disable_explicit_only(monkeypatch):
    calls=[]
    monkeypatch.setattr(s.subprocess,'Popen',lambda *a,**k:calls.append((a,k)))
    s.enable(12)
    assert s.enabled() and s.load('service.json')['interval']==43200
    assert len(calls)==1 and calls[0][1]['start_new_session']
    assert '--version' not in calls[0][0][0]
    monkeypatch.setattr(s,'running',lambda:True)
    s.enable(24);assert len(calls)==2
    assert 'disabled' in s.disable() and not s.enabled()
    for value in [0,721,True]:
        with pytest.raises(ValueError):s.enable(value)


def test_daemon_periodic_stop_and_overlap(monkeypatch):
    s.save('service.json',{'enabled':True,'interval':3600})
    stop=Event();calls=[]
    def check(**kwargs):
        calls.append(kwargs)
        assert kwargs['authorized'] and not kwargs['cancelled']()
        assert s.running()
        cancelled=Event();cancelled.set()
        s.daemon(cancelled)  # overlap sees the lock and exits
        s.disable();assert kwargs['cancelled']()
        stop.set()
        return {'lines':['x: OSV version match'],'checked':100,'error':False}
    monkeypatch.setitem(s.HELPERS,'check',check)
    monkeypatch.setitem(s.HELPERS,'read',lambda _: {})
    s.daemon(stop)
    assert len(calls)==1 and not s.running()
    assert len(s.load('notices.json')['events'])==1


def test_recent_cache_waits_and_honors_disable(monkeypatch):
    s.save('service.json',{'enabled':True,'interval':86400})
    monkeypatch.setitem(s.HELPERS,'read',lambda _: {'checked':s.time.time(),'error':True})
    monkeypatch.setitem(s.HELPERS,'check',lambda **k:pytest.fail('recent cache should wait'))
    class Stop:
        def is_set(self):return False
        def wait(self,_):s.disable()
    s.daemon(Stop())
    assert not s.running()


def test_changes_dedup_offline_and_removal():
    data={'checked':100,'lines':['tool OSV version match A'],'error':False}
    s.record_changes(data);s.record_changes(data)
    assert len(s.load('notices.json')['events'])==1
    s.record_changes({**data,'error':True,'lines':[]})
    assert len(s.load('notices.json')['events'])==1
    s.record_changes({**data,'lines':['tool OSV version match B']})
    s.record_changes({**data,'lines':[]})
    assert len(s.load('notices.json')['events'])==3
    assert 'not proof' in s.load('notices.json')['events'][-1]['message']


@pytest.mark.parametrize('action,readonly',[('status',True),('enable',False),('disable',False)])
def test_cli_dispatch(t_mod,monkeypatch,capsys,action,readonly):
    calls=[]
    helper={'status':lambda:calls.append('status') or 'cached',
            'enable':lambda hours:calls.append(('enable',hours)) or 'enabled',
            'disable':lambda:calls.append('disable') or 'disabled'}
    monkeypatch.setattr(t_mod.runpy,'run_path',lambda _:helper)
    monkeypatch.setattr(t_mod,'Config',lambda:None)
    assert t_mod._command_readonly(['system','advisories',action]) is readonly
    assert t_mod.main(['system','advisories',action])==0
    assert calls==[('enable',24) if action=='enable' else action]


def test_bad_state_is_disabled_and_status_honest(monkeypatch):
    s.save('service.json',{'enabled':True,'interval':-1})
    assert not s.enabled()
    monkeypatch.setitem(s.HELPERS,'read',lambda _:{'checked':1,'expires':2,'lines':['unsupported tool']})
    assert 'stale' in s.status() and 'unsupported tool' in s.status()
    monkeypatch.setenv('XDG_STATE_HOME','relative')
    assert '.local/state/t/advisories' in str(s.directory())


def test_fifo_lock_does_not_block_status():
    import os
    s.directory().mkdir(parents=True)
    os.mkfifo(s.directory()/'worker.lock')
    assert not s.running()


def test_overlap_candidate_waits_for_draining_owner(monkeypatch):
    s.save('service.json',{'enabled':True,'interval':86400})
    calls=[]
    def flock(*args):
        calls.append(1)
        if len(calls)==1:raise BlockingIOError()
    monkeypatch.setattr(s.fcntl,'flock',flock)
    monkeypatch.setitem(s.HELPERS,'read',lambda _: {'checked':s.time.time()})
    class Stop:
        def is_set(self):return False
        def wait(self,delay):
            if delay==1:s.disable()
    s.daemon(Stop())
    assert len(calls)==2


def test_valid_enabled_status_and_notice(monkeypatch):
    s.save('service.json',{'enabled':True,'interval':86400})
    s.save('notices.json',{'events':[{'at':100,'message':'changed'}]})
    assert '24 hours' in s.status() and 'Notice 100' in s.status()
    s.save('notices.json',{'events':'malformed'})
    s.record_changes({'error':False,'checked':100,'lines':[]})
    assert s.load('notices.json')['events']==[]
