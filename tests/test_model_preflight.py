"""Model-test consent and admission use fake owners, never live sessions."""
import copy
from pathlib import Path
from types import SimpleNamespace
import pytest
from conftest import load_script


@pytest.fixture
def preflight():
    return load_script(Path(__file__).parents[1] / 'libexec/t_model_preflight.py', 'model_preflight_test')


class UI:
    def __init__(self, *choices):
        self.choices = iter(choices)
        self.pages = []
    def pick(self, title, rows, **kw):
        self.pages.append((title, rows, copy.deepcopy(kw.get('context', []))))
        if kw.get('refresh') and kw['refresh']():
            return '__done__'
        return next(self.choices)
    def page(self, *args, **kw):
        self.pages.append(args)


@pytest.fixture
def boundary(preflight, monkeypatch):
    m = preflight
    owners = [dict(id='a', pid=10, identity='start10', model='first-model', kind='worker', tmux='/test/socket', pane='%1'),
              dict(id='b', pid=20, identity='start20', model='second-model', kind='session', tmux='/test/socket', pane='%2')]
    state = dict(generation='g', owners=owners, legacy=[], reserved=False, pid=99)
    calls, sends = [], []
    def rpc(action, **kw):
        calls.append((action, kw))
        return copy.deepcopy(state) if action == 'test-preflight' else {'token': 'test-token', 'ready': True}
    manager = dict(ensure=lambda **kw: {'test_preflight': True}, rpc=rpc, process_identity=lambda p:'start'+str(p))
    targets = [dict(socket='/test/socket', pane='%'+str(i), sid='sid'+str(i), pid=p,
                    session='dev-t-'+str(p), agent='codex', pane_pid=str(p)) for i,p in ((1,10),(2,20))]
    recovery = dict(run=lambda *a:SimpleNamespace(returncode=0, stdout='/test/socket'),
                    panes=lambda socket: targets,
                    descendant=lambda p, parent: str(p)==str(parent),
                    owner=lambda socket,pane: pane, current=lambda t:True,
                    tmux=lambda *args: sends.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(m, 'helper', lambda name: manager if name=='t_local_manager' else recovery)
    monkeypatch.setattr(m, 'legacy_owners', lambda *a: state['legacy'])
    return SimpleNamespace(m=m, state=state, calls=calls, sends=sends, manager=manager, recovery=recovery, targets=targets)


def test_batch_is_listed_and_one_selection_interrupts_every_owner(boundary):
    b=boundary; ui=UI('interrupt')
    lease=b.m.prepare(ui,'test-model')
    assert lease.token=='test-token'
    lease.close()
    assert [p[0] for p in ui.pages]==['Local runtime is in use','Waiting for runtime release']
    assert all(name in str(ui.pages[0]) for name in ('t-10','t-20','first-model','second-model'))
    assert len(b.sends)==2
    assert all(call[-1]=='Escape' for call in b.sends)
    assert [a for a,_ in b.calls].count('test-interrupt')==1


@pytest.mark.parametrize('choice,expected', [('wait',None),('back',False),(None,False)])
def test_wait_and_back_never_interrupt(boundary,choice,expected):
    b=boundary
    assert b.m.prepare(UI(choice),'model') is expected
    assert not b.sends and not any(a=='test-reserve' for a,_ in b.calls)


def test_free_runtime_reserves_without_prompt_or_interrupt(boundary):
    b=boundary;b.state['owners']=[];ui=UI()
    lease=b.m.prepare(ui,'model');lease.close()
    assert not b.sends
    assert not any(p[0]=='Local runtime is in use' for p in ui.pages)


def test_changed_set_requires_new_display_before_any_interrupt(boundary):
    b=boundary
    class Changing(UI):
        def pick(self,*a,**kw):
            selected=super().pick(*a,**kw)
            if selected=='interrupt': b.state['owners'].pop()
            return selected
    ui=Changing('interrupt','back')
    assert b.m.prepare(ui,'model') is False
    assert len(ui.pages)==2 and not b.sends


def test_targets_deduplicate_and_refuse_unverified_or_calling_owner(boundary):
    b=boundary; owners=b.state['owners'];owners.append(dict(owners[0],id='another'))
    assert len(b.m.session_targets(owners,b.recovery,b.manager))==2
    b.manager['process_identity']=lambda p:None
    assert all(r['target'] is None for r in b.m.session_targets(owners,b.recovery,b.manager))


def test_one_unverified_owner_disables_entire_batch(boundary):
    b=boundary;b.manager['process_identity']=lambda pid:'start10' if pid==10 else None
    class Check(UI):
        def pick(self,*a,**kw):
            assert 'interrupt' in kw['disabled']
            return 'interrupt'  # Backend refuses even if a caller ignores disabled UI.
    assert b.m.prepare(Check(),'model') is False
    assert not b.sends and not any(a=='test-reserve' for a,_ in b.calls)


def test_native_session_supervisor_resolves_its_child_cli(boundary):
    b=boundary
    owner=dict(b.state['owners'][1], pid=19, identity='start19')
    b.targets[1]['pane_pid']='18'
    # The native launcher holds the lease and is the CLI's parent, while
    # workers normally descend from the CLI. Both must belong to this pane.
    b.recovery['descendant']=lambda pid,parent: (str(pid),str(parent)) in {('19','18'),('20','19')}
    assert b.m.session_targets([owner],b.recovery,b.manager)[0]['target']==b.targets[1]
    owner['kind']='worker'
    assert b.m.session_targets([owner],b.recovery,b.manager)[0]['target'] is None


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12)])
def test_all_preflight_sessions_are_navigable_on_narrow_terminal(boundary,t_mod,width,height):
    from test_tui import make_ui
    ui=make_ui(t_mod,['up','up','enter','enter','q'],width,height)
    assert boundary.m.prepare(ui,'sample/model') is False
    assert not boundary.sends
    assert all(sum(t_mod._term_rows(line,width) for line in frame)<=height for frame in ui.frames)
    assert any('t-10' in line for frame in ui.frames for line in frame)


@pytest.mark.parametrize('failure', ['reserved','old','send','wait','interrupt','cancel','changed_target','lost_after_reserve'])
def test_failed_or_cancelled_preflight_never_starts_test(boundary,monkeypatch,failure):
    b=boundary;ui=UI('interrupt','back');rpc=b.manager['rpc']
    if failure=='reserved':b.state['reserved']=True
    if failure=='old':b.manager['ensure']=lambda **kw:{}
    if failure=='send':b.recovery['tmux']=lambda *a:SimpleNamespace(returncode=1)
    if failure=='wait':
        monkeypatch.setattr(b.m,'wait_for_release',lambda *a:(_ for _ in ()).throw(ValueError('not released')))
    if failure=='cancel':monkeypatch.setattr(b.m,'wait_for_release',lambda *a:False)
    if failure=='interrupt':
        b.manager['rpc']=lambda action,**kw: (_ for _ in ()).throw(ValueError('changed')) if action=='test-interrupt' else rpc(action,**kw)
    if failure=='changed_target':b.recovery['current']=lambda t:False
    if failure=='lost_after_reserve':
        def mutate(action,**kw):
            result=rpc(action,**kw)
            if action=='test-reserve':b.state['owners'].pop()
            return result
        b.manager['rpc']=mutate
    assert b.m.prepare(ui,'model') is False
    if any(a=='test-reserve' for a,_ in b.calls):assert any(a=='test-release' for a,_ in b.calls)


def test_wait_release_timeout_cancel_and_error(preflight,monkeypatch):
    m=preflight;lease=SimpleNamespace(error=None,call=lambda action:{'ready':False})
    assert not m.wait_for_release(UI('cancel'),lease,[])
    with pytest.raises(ValueError,match='not released'):m.wait_for_release(UI(),lease,[],timeout=-1)
    lease.error='lost reservation'
    with pytest.raises(ValueError,match='lost reservation'):m.wait_for_release(UI(),lease,[])
    lease.error=None;lease.call=lambda a:{'error':'provider still busy'}
    with pytest.raises(ValueError,match='provider still busy'):m.wait_for_release(UI(),lease,[])


def test_reservation_heartbeat_failure_and_close(preflight):
    m=preflight
    def fail(*a,**kw):raise ValueError('expired')
    # Exercise heartbeat deterministically without an actual timer.
    lease=m.Reservation.__new__(m.Reservation);lease.manager={'rpc':fail};lease.token='t';lease.error=None
    lease.done=SimpleNamespace(wait=lambda _:False,set=lambda:None)
    lease.thread=SimpleNamespace(join=lambda **kw:None)
    lease.heartbeat();assert lease.error=='expired';lease.close()


def test_legacy_probe_is_readonly_and_reports_lock_holder(preflight,monkeypatch,tmp_path):
    m=preflight;monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path));manager={'process_identity':lambda p:'start'}
    assert m.legacy_owners(manager,{})==[]
    path=tmp_path/'t/local-worker/worker.lock';path.parent.mkdir(parents=True);path.touch()
    assert m.legacy_owners(manager,{})==[]
    assert m.legacy_owners(manager,{'legacy_owned':True})==[]
    with path.open('r') as lock:
        m.fcntl.flock(lock,m.fcntl.LOCK_EX)
        monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='10\n99\n'))
        assert m.legacy_owners(manager,{'pid':99})[0]['pid']==10
        monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:(_ for _ in ()).throw(OSError()))
        assert m.legacy_owners(manager,{})[0]['id']=='legacy:unknown'
