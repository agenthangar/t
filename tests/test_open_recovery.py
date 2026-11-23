"""Recovery resets only disposable target state and preserves source/context."""
import fcntl
import json
import os
import pty
import select
import signal
import shutil
import subprocess
import sys
import termios
import time
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from conftest import load_script, REPO_ROOT
from test_session_lifecycle import lifecycle, _shell, _run, _unindexed_writers  # noqa: F401


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    for key, name in [('HOME', 'home'), ('XDG_STATE_HOME', 'state'),
                      ('XDG_CACHE_HOME', 'cache'), ('CODEX_HOME', 'codex')]:
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    return load_script(REPO_ROOT / 'libexec/t_open_recovery.py', 'open_recovery_test')


def record(sid='one', workspace='/work', updated=1, host='local'):
    return dict(repo='org/repo', agent='claude', sid=sid, updated=updated,
                associations=[dict(host=host, workspace=workspace, slot='3')], sources=[])


def test_context_selection_requires_workspace_or_exact_identity(recovery):
    a, b = record(), record('two', updated=2)
    assert recovery.select_context([a, b, record('other', '/elsewhere', 99)], '/work') == b
    assert recovery.select_context([a, b], '/work', 'one') == a
    assert recovery.select_context([record(host='remote')], '/work') is None
    with pytest.raises(ValueError, match='ambiguous'):
        recovery.select_context([a, record('two')], '/work')


def test_reset_backs_up_before_clearing_only_target_records(recovery, tmp_path, monkeypatch):
    history = recovery.HISTORY
    store = history['Store']()
    calls = []
    backup = tmp_path / 'backup.json'
    def cache(op, payload):
        assert backup.exists()
        assert json.loads(backup.read_text())['records']
        calls.append((op, payload))
        return {'forgotten': 1}
    monkeypatch.setitem(store.forget_workspace.__wrapped__.__globals__, '_CACHE_REQUEST', cache)
    one, other = record(), record('two', '/elsewhere')
    one['sources'] = [dict(host='local', path='/native/one.jsonl')]
    store.merge(one); store.merge(other)
    removed = store.forget_workspace('/work', backup=backup, evidence={'phase': 'prepared'})
    assert [r['sid'] for r in removed] == ['one']
    assert [r['sid'] for r in store.records()] == ['two']
    assert calls[0][0] == 'forget'
    assert len(calls) == 3
    assert backup.stat().st_mode & 0o777 == 0o600
    store.close()


def test_cache_failure_keeps_history_and_backup(recovery, tmp_path, monkeypatch):
    store = recovery.HISTORY['Store']()
    store.merge(record())
    monkeypatch.setitem(store.forget_workspace.__wrapped__.__globals__, '_CACHE_REQUEST', lambda *a: {})
    clock = iter([0, 4])
    monkeypatch.setattr(recovery.HISTORY['time'], 'monotonic', lambda: next(clock))
    backup = tmp_path / 'backup.json'
    with pytest.raises(ValueError, match='cache unavailable'):
        store.forget_workspace('/work', backup=backup, evidence={})
    assert len(store.records()) == 1 and backup.exists()
    store.close()


def test_existing_worktree_is_never_checked_out_or_freshened(recovery, tmp_path):
    r = object.__new__(recovery.Recovery)
    r.workspace = tmp_path / 'work'; r.workspace.mkdir()
    r.directory = str(r.workspace)
    calls = []
    def git(*args, **kw):
        calls.append(args)
        return str(r.workspace) if '--show-toplevel' in args else str(tmp_path / '.git')
    r.git = git
    assert r.source(None, lambda *a: pytest.fail('unexpected prompt')) is None
    assert all(args[0] == 'rev-parse' for args in calls)
    r.materialize(None)
    r.git = lambda *a, **kw: str(tmp_path / 'different-repository')
    with pytest.raises(ValueError, match='does not belong'):
        r.source(None, lambda *a: pytest.fail('unexpected prompt'))
    r.workspace.rmdir(); r.workspace.symlink_to(tmp_path)
    with pytest.raises(ValueError, match='symlink'):
        r.source(None, lambda *a: True)


def test_missing_source_requires_explicit_default_choice(recovery, tmp_path):
    r = object.__new__(recovery.Recovery)
    r.workspace = tmp_path / 'missing'; r.directory = str(r.workspace); r.branch = 'dev/repo-3'
    r.git = lambda *a, **kw: ('dev/repo-3' if a[0] == 'check-ref-format' else
                            'ref: refs/heads/main\tHEAD' if a[0] == 'ls-remote' else '')
    with pytest.raises(InterruptedError):
        r.source(None, lambda *a: False)
    assert r.source(None, lambda *a: True) == ('branch', 'dev/repo-3', '')
    r.git = lambda *a, **kw: ''
    with pytest.raises(ValueError, match='branch name is invalid'):
        r.source(None, lambda *a: pytest.fail('unexpected prompt'))
    r.git = lambda *a, **kw: 'dev/repo-3' if a[0] == 'check-ref-format' else ''
    with pytest.raises(ValueError, match='default branch could not be determined'):
        r.source(None, lambda *a: pytest.fail('unexpected prompt'))


@pytest.mark.parametrize('failure', ['', 'owner', 'agent', 'legacy'])
def test_force_rechecks_owner_before_reset(recovery, tmp_path, monkeypatch, failure):
    r = object.__new__(recovery.Recovery)
    r.repo='repo'; r.slot='3'; r.session='dev-repo-3'; r.directory='/work'; r.branch='dev/repo-3'
    r.api={}; r.cfg=SimpleNamespace(); r.backup=None
    calls=[]
    store=SimpleNamespace(records=lambda: [], close=lambda: None,
        forget_workspace=lambda *a, **kw: calls.append('reset') or [])
    history=SimpleNamespace(discover=lambda *a, **kw: None, store=store)
    monkeypatch.setitem(recovery.HISTORY, 'HistoryCommand', lambda *a: history)
    owners=iter([None, {'sid':'new'} if failure == 'owner' else None])
    r.owner=lambda: next(owners)
    r.source=lambda *a: None
    r.materialize=lambda source: calls.append('materialize')
    r.reservations=lambda: [{'path': '/work/.git/t-app-slot'}] if failure == 'legacy' else []
    r.clear_reservations=lambda values: None
    r.shell=lambda name, *a: ('unknown' if failure == 'agent' else 'claude') if name == '_dev_agent_for' else calls.append(name) or ''
    from contextlib import nullcontext
    monkeypatch.setitem(recovery.LIFECYCLE, 'launch_claims', lambda *a: nullcontext())
    monkeypatch.setitem(recovery.LIFECYCLE, 'native_preflight', lambda *a, **kw: calls.append('guard'))
    if failure:
        message = {'owner': 'changed', 'agent': 'unsupported', 'legacy': 'ownership is unknown'}[failure]
        with pytest.raises(ValueError, match=message):
            r.force(confirmed=True)
        assert not calls
        if failure == 'legacy':
            assert json.loads(r.backup.read_text())['phase'] == 'prepared'
    else:
        r.force(confirmed=True)
        assert calls.index('guard') < calls.index('reset') < calls.index('_dev_new_session_claimed')
        assert json.loads(r.backup.read_text())['phase'] == 'complete'


def force_open(base, env, command):
    # Recovery performs several guarded login-shell calls, unlike a single open.
    # Keep a bounded deadline that also tolerates concurrent full test runs.
    return _run(['zsh', '-lic', command], env=env, cwd=base, timeout=90)


def test_force_without_confirmation_does_not_touch_session(lifecycle):
    base, env = lifecycle
    result = force_open(base, env, 't session open repo 1 --force')
    assert result.returncode == 2, result.stdout + result.stderr
    assert '--yes' in result.stderr
    assert not (base / 'agents.log').exists()


def test_force_preserves_dirty_worktree_and_stops_only_selected_owner(lifecycle):
    base, env = lifecycle
    (base/'config/t/local.zsh').write_text((base/'config/t/local.zsh').read_text()+
        '\nDEV_AGENT_DEFAULT=claude\nDEV_REPOS[alias]=$DEV_REPOS[repo]\n')
    first = _shell(base, env, 't session open repo 1 --claude')
    assert first.returncode == 0, first.stdout + first.stderr
    worktree = base/'worktrees/repo/repo-1'
    (worktree/'draft.txt').write_text('keep this work\n')
    before = _run(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], env=env, cwd=base).stdout
    result = force_open(base, env, 't session open alias 1 --force --yes')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'fresh conversation' in result.stdout
    assert (worktree/'draft.txt').read_text() == 'keep this work\n'
    assert _run(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], env=env, cwd=base).stdout == before
    assert len((base/'agents.log').read_text().splitlines()) == 2
    backups = list((base/'state/t/lifecycle/open-recovery').glob('*.json'))
    assert len(backups) == 1 and json.loads(backups[0].read_text())['phase'] == 'complete'


@pytest.mark.parametrize('agent', ['claude', 'codex'])
@pytest.mark.parametrize('missing', [False, True])
def test_force_restores_saved_context(lifecycle, agent, missing):
    base, env = lifecycle
    worktree = base/'worktrees/repo/repo-1'
    worktree.parent.mkdir(parents=True)
    added = _run(['git', '-C', str(base/'repo'), 'worktree', 'add', '-b', 'dev/repo-1', str(worktree), 'origin/main'], env=env, cwd=base)
    assert added.returncode == 0, added.stderr
    sid='12345678-1234-1234-1234-123456789abc'
    if agent == 'claude':
        project=base/'home/.claude/projects'/recovery_encode(str(worktree)); project.mkdir(parents=True)
        transcript=project/(sid+'.jsonl')
        transcript.write_text(json.dumps(dict(type='user', cwd=str(worktree), sessionId=sid,
            message=dict(role='user', content='Saved context'))) + '\n')
    else:
        home=base/'codex'
        (home/'thread-writer-locks').mkdir()
        transcript=home/('rollout-2026-10-11T00-00-00-'+sid+'.jsonl')
        transcript.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid, cwd=str(worktree), source='cli', cli_version='0.160.0'))) + '\n' +
            json.dumps(dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='Saved context')])))+'\n')
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('create table threads (id text, rollout_path text, cwd text, name text, title text, updated_at integer, archived integer, source text, first_user_message text)')
            db.execute('insert into threads values (?,?,?,?,?,?,?,?,?)',(sid,str(transcript),str(worktree),None,'Saved work',1,0,'cli','Saved context'))
        if not missing:
            with _unindexed_writers(base,env,(sid,)):
                blocked=force_open(base,env,'t session open repo 1 --force --yes')
                assert blocked.returncode != 0 and 'writer lock' in blocked.stderr
                assert not (base/'agents.log').exists()
    if missing:
        result=_run(['git','-C',str(base/'repo'),'worktree','remove',str(worktree)],env=env,cwd=base)
        assert result.returncode == 0, result.stderr
    result=force_open(base, env, 't session open repo 1 --force --yes')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Resumed saved context' in result.stdout
    assert sid in (base/'agents.log').read_text()
    assert transcript.exists()


def recovery_encode(value):
    return ''.join(c if c.isalnum() else '-' for c in value)


def test_shell_and_git_boundaries_quote_arguments_and_keep_errors(recovery, monkeypatch):
    r=object.__new__(recovery.Recovery)
    r.api={'ZSH_PRELUDE':'source plugin; '}; r.cfg=SimpleNamespace(repos={'repo':'/repo'}); r.repo='repo'
    calls=[]
    outcome=SimpleNamespace(returncode=0, stdout='result\n', stderr='')
    monkeypatch.setenv('COV_CORE_SOURCE', 'must not escape')
    monkeypatch.setattr(recovery.subprocess, 'run', lambda argv, **kw: calls.append((argv,kw)) or outcome)
    assert r.shell('action', "a' b; $cmd") == 'result\n'
    assert calls[-1][0][:2] == ['zsh','-lic']
    assert calls[-1][1]['env']['T_MCP_DETACHED']=='1'
    assert 'COV_CORE_SOURCE' not in calls[-1][1]['env']
    assert r.git('status') == 'result'
    outcome.returncode=1; outcome.stderr='specific failure'
    for fn in (r.shell,r.git):
        with pytest.raises(ValueError,match='specific failure'): fn('action')
        assert fn('action',optional=True)==''
    outcome.stderr=''; outcome.stdout=''
    with pytest.raises(ValueError,match='session recovery'): r.shell('action')
    with pytest.raises(ValueError,match='source'): r.git('action')


def test_recovery_target_and_owner_validation(recovery, monkeypatch, tmp_path):
    cfg=SimpleNamespace(repos={'repo':'/repo'},worktree_root='/worktrees')
    with pytest.raises(ValueError,match='numeric'): recovery.Recovery({},cfg,'repo','all')
    responses={'_dev_worktree_path':str(tmp_path/'work'), '_dev_worktree_branch':'dev/repo-3'}
    monkeypatch.setattr(recovery.Recovery,'shell',lambda self,name,*a,**kw:responses.get(name,''))
    r=recovery.Recovery({},cfg,'repo','3')
    assert r.owner() is None
    responses['_t_open_recovery_owner']='bad'
    with pytest.raises(ValueError,match='verify'): r.owner()
    responses['_t_open_recovery_owner']='\0'.join(['$1','%1','1','','sid','claude','dead',''])
    assert r.owner()['agent']=='claude'
    responses['_dev_worktree_path']=''
    with pytest.raises(ValueError,match='isolated'): recovery.Recovery({},cfg,'repo','3')


def test_repair_restarts_only_dead_pane(recovery, monkeypatch):
    r=object.__new__(recovery.Recovery); r.repo='repo'; r.slot='3'; r.session='dev-repo-3'; r.directory='/work'
    events=[]; monkeypatch.setattr(recovery,'CACHE',lambda *a:events.append(a))
    calls=[]; r.shell=lambda *a,**kw:calls.append(a)
    r.owner=lambda:None
    r.repair(); assert calls[-1] == ('_t_dev','repo','3')
    r.owner=lambda:{'dead':'1','sid':''}
    with pytest.raises(ValueError,match='no saved'): r.repair()
    r.owner=lambda:{'dead':'1','sid':'sid','agent':'claude','dead_identity':'dead'}
    r.repair(); assert calls[-1][0]=='_t_restart_slot' and 'restart-dead' in calls[-1]


def test_source_precedence_and_live_branch_protection(recovery,tmp_path):
    r=object.__new__(recovery.Recovery); r.workspace=tmp_path/'missing'; r.directory=str(r.workspace); r.branch='dev/repo-3'
    calls=[]
    state={'local':'revision','remote':'','pr':'','listing':''}
    def git(*a,**kw):
        calls.append(a)
        if a[0]=='check-ref-format': return a[-1]
        if a[:2]==('worktree','list'): return state['listing']
        if a[0]=='rev-parse':
            return state['remote'] if 'refs/remotes/' in a[-1] else state['pr'] if 'refs/t/' in a[-1] else state['local']
        return ''
    r.git=git
    assert r.source(None,lambda *a:False)==('local',r.branch,r.branch)
    state['local']=''; state['remote']='remote-rev'
    assert r.source(None,lambda *a:False)==('branch',r.branch,'remote-rev')
    state['remote']=''; state['pr']='pr-rev'
    row=record(workspace=r.directory); row['prs']=[{'repo':'org/repo','number':7}]
    assert r.source(row,lambda *a:False)==('branch',r.branch,'pr-rev')
    assert any('refs/pull/7/head:' in a[-1] for a in calls)
    r.materialize(('branch',r.branch,'revision'))
    assert calls[-1]==('worktree','add','--force','-b',r.branch,r.directory,'revision')
    state['listing']=f'worktree {tmp_path}\nbranch refs/heads/{r.branch}'
    with pytest.raises(ValueError,match='already checked out'): r.materialize(('local',r.branch,r.branch))


def test_reservation_backup_and_identity_recheck(recovery,tmp_path):
    r=object.__new__(recovery.Recovery); r.workspace=tmp_path/'work'; r.workspace.mkdir(); r.directory=str(r.workspace)
    gitdir=tmp_path/'git'; gitdir.mkdir(); r.git=lambda *a,**kw:str(gitdir)
    marker=tmp_path/'.work.history-reservation'; marker.write_text('token')
    legacy=gitdir/'t-app-slot'; legacy.write_text('old')
    saved=r.reservations()
    assert {x['data'] for x in saved}=={b'token'.hex(),b'old'.hex()}
    marker.write_text('different')
    with pytest.raises(ValueError,match='changed'): r.clear_reservations(saved)
    r.clear_reservations(r.reservations())
    assert not marker.exists() and not legacy.exists()
    assert r.reservations() == []
    marker.symlink_to(gitdir)
    with pytest.raises(ValueError,match='unsafe'): r.reservations()


@pytest.mark.parametrize('outcome', ['resume','cancel','writer-held'])
def test_force_saved_owner_release_precedes_reset_and_resume(recovery,tmp_path,monkeypatch,outcome):
    from contextlib import nullcontext
    r=object.__new__(recovery.Recovery)
    r.repo='repo'; r.slot='3'; r.session='dev-repo-3'; r.directory='/work'; r.api={}; r.cfg=SimpleNamespace(); r.backup=None
    owner=dict(identity='$1',pane='%1',dead='0',pid='123',sid='one',agent='claude',dead_identity='live')
    r.owner=lambda:owner
    calls=[]
    row=record()
    store=SimpleNamespace(records=lambda:[row],close=lambda:None,
        forget_workspace=lambda *a,**kw:calls.append('reset') or [row])
    history=SimpleNamespace(discover=lambda *a,**kw:None,store=store,
        source=lambda *a,**kw:tmp_path/'transcript',
        install_transcript=lambda *a:calls.append('install') or tmp_path/'transcript')
    monkeypatch.setitem(recovery.HISTORY,'HistoryCommand',lambda *a:history)
    monkeypatch.setitem(recovery.LIFECYCLE,'launch_claims',lambda *a:nullcontext())
    monkeypatch.setitem(recovery.LIFECYCLE,'native_preflight',lambda *a,**kw:calls.append('guard-'+a[0]))
    def release(*a):
        calls.append('released')
        if outcome=='writer-held': raise ValueError('writer still held')
    monkeypatch.setitem(recovery.LIFECYCLE,'wait_released',release)
    monkeypatch.setattr(recovery,'ask',lambda *a:outcome!='cancel')
    r.source=lambda *a:None; r.reservations=lambda:[]; r.clear_reservations=lambda *a:None; r.materialize=lambda *a:None
    r.shell=lambda name,*a:calls.append(name) or 'pane text'
    if outcome=='cancel':
        with pytest.raises(InterruptedError): r.force()
        assert not calls and r.backup is None
    elif outcome=='writer-held':
        with pytest.raises(ValueError,match='writer'): r.force()
        assert 'reset' not in calls and 'pane' in json.loads(r.backup.read_text())
    else:
        r.force()
        assert calls.index('guard-codex') < calls.index('_t_open_recovery_stop') < calls.index('released') < calls.index('guard-claude') < calls.index('reset') < calls.index('_dev_resume_session_claimed')


def test_recovery_menu_has_only_action_and_cancel(recovery,monkeypatch):
    monkeypatch.setattr(recovery.sys.stdin,'isatty',lambda:False)
    assert not recovery.ask({},'Title','Reason','Repair')
    monkeypatch.setattr(recovery.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(recovery.sys.stdout,'isatty',lambda:True)
    class UI:
        def __init__(self,api): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def intro(self,*a): pass
        def choose(self,title,rows):
            assert [r['label'] for r in rows]==['Repair','Cancel']
            return 'go'
    monkeypatch.setattr(recovery.runpy,'run_path',lambda *a,**kw:{'ShareUI':UI})
    assert recovery.ask({},'Title','Reason','Repair')


@pytest.mark.parametrize('outcome', ['repaired', 'force', 'cancel'])
def test_failed_open_prompts_for_repair_before_force(recovery, monkeypatch, outcome):
    calls = []
    monkeypatch.setattr(recovery.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(recovery.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(recovery.runpy, 'run_path', lambda *a, **kw: {'Config': lambda: None})
    def ask(*args):
        calls.append(args[-1])
        return outcome != 'cancel'
    monkeypatch.setattr(recovery, 'ask', ask)
    class Target:
        def __init__(self, *args):
            self.backup = None
        def repair(self):
            calls.append('repair')
            if outcome == 'force':
                raise ValueError('original open failure')
        def force(self, *, confirmed):
            assert not confirmed  # Force retains its own confirmation after repair.
            calls.append('force')
    monkeypatch.setattr(recovery, 'Recovery', Target)
    assert recovery.main(['repair', 'repo', '3', '0']) == (130 if outcome == 'cancel' else 0)
    assert calls == (['Repair'] if outcome == 'cancel' else
                     ['Repair', 'repair', 'force'] if outcome == 'force' else
                     ['Repair', 'repair'])


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_repair_retains_explicit_agent(recovery, monkeypatch, agent):
    """Repair must not silently fall back to the repository's default agent."""
    monkeypatch.setenv('T_OPEN_RECOVERY_AGENT', agent)
    monkeypatch.setattr(recovery, 'CACHE', lambda *a: None)
    r = object.__new__(recovery.Recovery)
    r.repo, r.slot = 'repo', '3'
    r.owner = lambda: None
    calls = []
    r.shell = lambda *args: calls.append(args)
    r.repair()
    assert calls == [('_t_dev', 'repo', '3', '--' + agent)]


def test_repair_rejects_invalid_agent_without_launch(recovery, monkeypatch):
    monkeypatch.setenv('T_OPEN_RECOVERY_AGENT', 'invalid-agent')
    monkeypatch.setattr(recovery, 'CACHE', lambda *a: None)
    r = object.__new__(recovery.Recovery)
    r.owner = lambda: None
    r.shell = lambda *a: pytest.fail('invalid agent must not launch')
    with pytest.raises(ValueError, match='unsupported recovery agent'):
        r.repair()


@pytest.mark.skipif(not shutil.which('zsh'), reason='zsh required')
def test_captured_recovery_shell_preserves_terminal_foreground(tmp_path):
    home = tmp_path/'terminal-home'
    home.mkdir()
    (home/'.zshenv').write_text('skip_global_compinit=1\n')
    (home/'.zshrc').write_text('')
    script = tmp_path/'terminal.py'
    script.write_text(f'''import json, os, runpy, sys
m = runpy.run_path({str(REPO_ROOT/'libexec/t_open_recovery.py')!r})
r = object.__new__(m['Recovery'])
r.api = {{'ZSH_PRELUDE': ''}}
before = os.tcgetpgrp(0)
child = json.loads(r.shell(sys.executable, '-c',
    'import json,os; print(json.dumps(dict(sid=os.getsid(0),pgrp=os.getpgrp(),stdin=os.isatty(0))))'))
print(json.dumps(dict(before=before,after=os.tcgetpgrp(0),sid=os.getsid(0),child=child)), flush=True)
# A stopped parent cannot service the repair menu. Keep the test bounded even
# when the unfixed scanner leaves this process in a background process group.
if os.tcgetpgrp(0) == os.getpgrp():
    print('INPUT_READY', flush=True)
    print('INPUT=' + sys.stdin.readline().strip(), flush=True)
''')
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(HOME=str(home), ZDOTDIR=str(home), XDG_CONFIG_HOME=str(home/'config'),
               XDG_STATE_HOME=str(home/'state'), XDG_CACHE_HOME=str(home/'cache'), TERM='xterm-256color')
    master, slave = pty.openpty()
    def control_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    process = subprocess.Popen([sys.executable, str(script)], env=env,
                               stdin=slave, stdout=slave, stderr=slave,
                               preexec_fn=control_terminal)
    os.close(slave)
    output = b''
    sent = False
    deadline = time.monotonic()+10
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if b'INPUT_READY' in output and not sent:
                    os.write(master, b'retry\n')
                    sent = True
            elif process.poll() is not None:
                break
        assert process.wait(timeout=1) == 0, output.decode(errors='replace')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)
    text = output.decode(errors='replace')
    report = next(json.loads(line) for line in text.splitlines() if line.startswith('{'))
    assert report['after'] == report['before'], text
    assert report['child']['sid'] != report['sid'], text
    assert report['child']['stdin'] is False, text
    assert 'INPUT=retry' in text, text
