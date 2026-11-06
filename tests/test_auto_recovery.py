"""Automatic recovery uses durable intent, exact identities, and native lifecycle evidence."""
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT, load_script

SID = '01234567-89ab-cdef-0123-456789abcdef'


def result(stdout='', rc=0, stderr=''):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@pytest.fixture
def auto(tmp_path, monkeypatch):
    for name, suffix in (('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')):
        monkeypatch.setenv(name, str(tmp_path/suffix))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.delenv('T_RECOVERY_TOKEN', raising=False)
    monkeypatch.delenv('T_RECOVERY_RESTORE_GENERATION', raising=False)
    mod = load_script(REPO_ROOT/'libexec/t_auto_recovery.py', 't_auto_test')
    monkeypatch.setattr(mod, 'boot', lambda: 'boot-one')
    monkeypatch.setattr(mod, 'run', lambda *a, **k: result(rc=1))
    return mod


def enable(auto):
    auto.write(auto.settings_path(), {'enabled': True})


def session(auto, tmp_path, **values):
    cwd = tmp_path/'workspace'
    cwd.mkdir(exist_ok=True)
    row = dict(agent='claude', sid=SID, cwd=str(cwd), workspace=auto.workspace(cwd),
               generation='generation', boot='old-boot', phase='running', open=True, paused=False,
               session='dev-api-1', executable='claude', options=[], safe=True, updated=1)
    row.update(values)
    auto.update(lambda data: data['sessions'].update({auto.key(row['agent'], SID): row}))
    return row


class FakeRuntime:
    def __init__(self, owners=(), available=True, error=None):
        self.owners, self.online, self.error = list(owners), available, error
        self.started, self.stopped = [], []

    def live(self):
        return self.owners

    def available(self, row):
        return self.online

    def stop(self, row, owner):
        self.stopped.append(owner)

    def restore(self, row, prompt):
        if self.error:
            raise ValueError(self.error)
        self.started.append((row, prompt))


def test_status_is_readonly_and_does_not_create_state(auto, capsys):
    assert auto.status() == {'enabled': False, 'running': False, 'sessions': []}
    assert not auto.root().exists()
    auto.command(SimpleNamespace(scope='system', action='status', json=True))
    assert json.loads(capsys.readouterr().out)['enabled'] is False
    auto.command(SimpleNamespace(scope='system', action='status', json=False))
    assert 'No enrolled sessions' in capsys.readouterr().out
    assert not auto.root().exists()
    assert auto.prefix('codex') == []
    assert auto.detail('codex', SID) is None


def test_private_atomic_state_and_rejected_inputs(auto, tmp_path):
    p = auto.root()/'test.json'
    auto.write(p, {'ok': True})
    assert p.stat().st_mode & 0o777 == 0o600
    assert auto.root().stat().st_mode & 0o777 == 0o700
    link = auto.root()/'link'
    link.symlink_to(p)
    for fn in (lambda: auto.read(link), lambda: auto.write(link, {})):
        with pytest.raises(ValueError, match='symlink|unsafe'):
            fn()
    p.write_text('[]')
    with pytest.raises(ValueError, match='invalid'):
        auto.read(p)
    with pytest.raises(ValueError):
        auto.key('other', SID)
    with pytest.raises(ValueError):
        auto.key('codex', '../escape')
    assert auto.read(tmp_path/'absent', {'ok': 1}) == {'ok': 1}


@pytest.mark.parametrize('phase,continue_work', [('running', True), ('outage', True), ('idle', False),
    ('waiting', False), ('interrupted', False), ('unknown', False), ('attention', None)])
def test_reboot_continues_only_verified_interrupted_work(auto, tmp_path, phase, continue_work):
    enable(auto)
    session(auto, tmp_path, phase=phase)
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    if continue_work is None:
        assert not runtime.started
    else:
        assert len(runtime.started) == 1
        assert bool(runtime.started[0][1]) == continue_work
        if continue_work:
            assert 'Verify the outcome' in runtime.started[0][1]


@pytest.mark.parametrize('change', [{'paused': True}, {'open': False}, {'next_retry': 10**12}])
def test_explicit_pause_close_and_backoff_prevent_restore(auto, tmp_path, change):
    enable(auto)
    session(auto, tmp_path, **change)
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    assert not runtime.started


def test_live_owners_are_reused_and_failed_owner_is_stopped_before_restore(auto, tmp_path):
    enable(auto)
    row = session(auto, tmp_path)
    runtime = FakeRuntime([row])
    auto.Engine(runtime).tick()
    assert not runtime.started
    auto.update(lambda d: d['sessions']['claude:'+SID].update(phase='outage'))
    auto.Engine(runtime).tick()
    assert runtime.stopped == [row]
    assert len(runtime.started) == 1
    runtime = FakeRuntime([row, row])
    auto.Engine(runtime).tick()
    assert auto.detail('claude', SID)['phase'] == 'attention'


def test_outage_waits_indefinitely_without_starting_agents(auto, tmp_path):
    enable(auto)
    session(auto, tmp_path)
    runtime = FakeRuntime(available=False)
    engine = auto.Engine(runtime)
    for _ in range(12):
        engine.tick()
        auto.update(lambda d: d['sessions']['claude:'+SID].update(next_retry=0))
    assert not runtime.started
    row = auto.read(auto.root()/'journal.json')['sessions']['claude:'+SID]
    assert row['attempts'] == 12
    assert 'Waiting for connection' == row['reason']
    assert 270 <= auto.retry_delay(200) <= 300


def test_repeated_launch_failures_stop_without_changing_workspace(auto, tmp_path):
    enable(auto)
    row = session(auto, tmp_path)
    runtime = FakeRuntime(error='guard refused unknown remote owner')
    engine = auto.Engine(runtime)
    for _ in range(3):
        engine.tick()
        auto.update(lambda d: d['sessions']['claude:'+SID].update(next_retry=0))
    assert auto.detail('claude', SID)['phase'] == 'attention'
    assert Path(row['cwd']).is_dir()


def test_crash_during_delivery_never_submits_twice(auto, tmp_path):
    enable(auto)
    session(auto, tmp_path, submission={'marker': 'unique', 'state': 'prepared'})
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    assert not runtime.started
    assert 'uncertain' in auto.detail('claude', SID)['reason']


def test_workspace_replacement_and_stale_engine_snapshot(auto, tmp_path):
    enable(auto)
    row = session(auto, tmp_path)
    auto.update(lambda d: d['sessions']['claude:'+SID]['workspace'].update(inode=-1))
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    assert not runtime.started
    auto.Engine(runtime).save('claude:'+SID, 'old-generation', phase='idle')
    assert auto.detail('claude', SID)['phase'] == 'running'
    assert 'replaced' in auto.detail('claude', SID)['reason']


def test_completed_native_transcript_overrides_stale_running_journal(auto, tmp_path):
    enable(auto)
    tx = tmp_path/'transcript.jsonl'
    tx.write_text(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'end_turn'}})+'\n')
    row = session(auto, tmp_path, transcript=str(tx))
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    assert runtime.started[0][1] == ''
    tx.write_text(tx.read_text()+json.dumps({'type': 'user', 'message': {'content': 'Continue'}})+'\n')
    assert auto.transcript_state(row) == 'running'
    tx.write_text('garbage\n'+json.dumps({'type': 'event_msg', 'payload': {'type': 'turn_aborted'}}))
    assert auto.transcript_state({**row, 'agent': 'codex'}) == 'interrupted'
    tx.unlink()
    with pytest.raises(ValueError, match='missing'):
        auto.transcript_state(row)


@pytest.mark.parametrize('agent', ['codex', 'claude', 'cursor'])
def test_exact_resume_preserves_explicit_launch_settings(auto, agent):
    executable = 'cursor-agent' if agent == 'cursor' else agent
    spec = auto.launch_options(agent, [executable, '--model', 'pinned-model', '--', 'private prompt'])
    assert 'private prompt' not in json.dumps(spec)
    args = auto.resume_argv({**spec, 'agent': agent, 'sid': SID}, 'recover')
    assert SID in args and 'pinned-model' in args
    assert args[-2:] == ['--', 'recover']
    assert '--no-daemon' in args if agent == 'codex' else '--resume' in args


@pytest.mark.parametrize('argv,safe', [
    (['claude', '-r', SID, '--effort', 'high'], True),
    (['cursor-agent', '--resume='+SID, '--force'], True),
    (['codex', 'resume', SID, '--cd', '/tmp', '--no-daemon'], True),
    (['codex', '--model'], False), (['codex', '--profile', 'custom'], False),
    (['codex', '-c', 'api_key=private'], False), (['claude', '--settings', '{"fastMode":true}'], True),
    (['codex', '-c', 'mcp_servers.private.args=credentials'], False),
    (['claude', '--settings', '/private/config.json'], False),
])
def test_launch_option_validation(auto, argv, safe):
    spec = auto.launch_options('codex', argv)
    assert spec['safe'] == safe
    assert 'private' not in json.dumps(spec)


def test_native_lifecycle_deduplicates_generation_and_preserves_pause(auto, tmp_path):
    enable(auto)
    cwd = tmp_path/'work'; cwd.mkdir()
    token = auto.begin('claude', ['claude'], str(cwd))
    auto.event('claude', {'session_id': SID, 'event': 'SessionStart'}, token)
    auto.pause('claude:'+SID, True)
    auto.event('claude', {'session_id': SID, 'event': 'UserPromptSubmit', 'prompt': 'do work'}, token)
    assert auto.detail('claude', SID)['phase'] == 'running'
    assert auto.detail('claude', SID)['paused']
    auto.event('claude', {'session_id': SID, 'event': 'PermissionRequest'}, token)
    assert auto.detail('claude', SID)['phase'] == 'waiting'
    auto.event('claude', {'session_id': SID, 'event': 'StopFailure', 'last_assistant_message': 'API Error: Connection error.'}, token)
    assert auto.detail('claude', SID)['phase'] == 'outage'
    new = auto.begin('claude', ['claude', '--resume', SID], str(cwd))
    auto.event('claude', {'session_id': SID, 'event': 'Stop'}, new)
    auto.event('claude', {'session_id': SID, 'event': 'UserPromptSubmit'}, token)
    assert auto.detail('claude', SID)['phase'] == 'idle'
    auto.retire('claude', SID)
    assert not auto.detail('claude', SID)['open']


@pytest.mark.parametrize('agent,kind,payload,expected', [
    ('claude', 'PostToolUse', {}, 'running'), ('claude', 'Elicitation', {}, 'waiting'),
    ('claude', 'PostToolUseFailure', {'is_interrupt': True}, 'interrupted'),
    ('claude', 'StopFailure', {'last_assistant_message': 'API Error: 401 unauthorized'}, 'attention'),
    ('cursor', 'stop', {'status': 'completed'}, 'idle'), ('cursor', 'stop', {'status': 'aborted'}, 'interrupted'),
    ('cursor', 'stop', {'status': 'error'}, 'attention'),
    ('cursor', 'sessionEnd', {'reason': 'error', 'error_message': 'Connection failed'}, 'outage'),
    ('claude', 'SessionEnd', {'reason': 'prompt_input_exit'}, 'unknown'),
])
def test_agent_adapters_distinguish_wait_cancel_failure_and_completion(auto, tmp_path, agent, kind, payload, expected):
    enable(auto)
    token = auto.begin(agent, [agent], str(tmp_path))
    auto.event(agent, {'session_id': SID, 'event': kind, **payload}, token)
    assert auto.detail(agent, SID)['phase'] == expected
    if payload.get('reason') == 'prompt_input_exit':
        assert not auto.detail(agent, SID)['open']


def test_unmanaged_and_malformed_hook_events_do_not_enroll(auto, tmp_path):
    auto.event('claude', {})
    enable(auto)
    auto.event('claude', {}, 'unknown')
    token = auto.begin('claude', ['claude'], str(tmp_path))
    auto.event('codex', {'session_id': SID}, token)
    auto.event('claude', {'session_id': '../bad'}, token)
    assert not auto.status()['sessions']


def test_codex_observer_tracks_exact_primary_thread_and_approvals(auto, tmp_path):
    enable(auto)
    token = auto.begin('codex', ['codex'], str(tmp_path))
    observer = auto.CodexObserver(token)
    observer.emit('completed')
    observer.client({'id': 1, 'method': 'thread/start'})
    observer.server({'id': 1, 'result': {'thread': {'id': SID}}})
    observer.client({'method': 'turn/start', 'params': {'threadId': SID, 'input': [{'type': 'text', 'text': 'work'}]}})
    observer.server({'method': 'turn/started', 'params': {'threadId': SID, 'turn': {'id': 'turn1'}}})
    observer.server({'method': 'turn/completed', 'params': {'threadId': 'other', 'turn': {'status': 'completed'}}})
    assert auto.detail('codex', SID)['phase'] == 'running'
    observer.server({'method': 'thread/status/changed', 'params': {'threadId': SID, 'status': {'activeFlags': ['waitingOnApproval']}}})
    assert auto.detail('codex', SID)['phase'] == 'waiting'
    observer.server({'method': 'item/tool/requestApproval', 'params': {'threadId': SID}})
    observer.client({'method': 'turn/interrupt', 'params': {'threadId': SID}})
    assert auto.detail('codex', SID)['phase'] == 'interrupted'
    observer.server({'method': 'turn/completed', 'params': {'threadId': SID, 'turn': {'status': 'failed', 'error': {
        'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': None}}}}}})
    assert auto.detail('codex', SID)['phase'] == 'outage'


def test_submission_is_prepared_before_native_start_and_acknowledged_once(auto, tmp_path):
    enable(auto)
    row = session(auto, tmp_path, submission={'marker': 'receipt', 'state': 'queued'})
    token = auto.begin('claude', auto.resume_argv(row, auto.continuation('receipt')), row['cwd'])
    assert auto.read(auto.root()/'journal.json')['sessions']['claude:'+SID]['submission']['state'] == 'prepared'
    auto.event('claude', {'session_id': SID, 'event': 'UserPromptSubmit', 'prompt': auto.continuation('receipt')}, token)
    assert auto.read(auto.root()/'journal.json')['sessions']['claude:'+SID]['submission']['state'] == 'acknowledged'


def test_config_service_is_opt_in_and_preserves_existing_hooks(auto, tmp_path, monkeypatch):
    root = tmp_path/'release'; root.mkdir()
    monkeypatch.setattr(auto, 'ROOT', root)
    monkeypatch.setattr(auto.sys, 'platform', 'darwin')
    monkeypatch.setattr(auto, 'snapshot', lambda: [])
    calls = []
    monkeypatch.setattr(auto, 'run', lambda args, **kw: calls.append(args) or result())
    path = Path.home()/'.claude/settings.json'
    auto.write(path, {'permissions': {'keep': True}, 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'my-hook'}]}]}})
    auto.configure(True, False)
    assert auto.enabled() and auto.service_path().exists()
    plist = auto.plistlib.loads(auto.service_path().read_bytes())
    assert plist['RunAtLoad'] and plist['KeepAlive']
    assert plist['ProgramArguments'][0] == str(root/'bin/t')
    assert auto.read(path)['permissions'] == {'keep': True}
    assert auto.read(path)['hooks']['Stop'][0]['hooks'][0]['command'] == 'my-hook'
    assert calls[-1][:2] == ['launchctl', 'bootstrap']
    auto.configure(False, True)
    assert not auto.enabled() and not auto.service_path().exists()
    assert auto.read(path)['hooks']['Stop'] == [{'hooks': [{'type': 'command', 'command': 'my-hook'}]}]
    with pytest.raises(ValueError, match='changed'):
        auto.configure(True, True)


def test_service_failure_is_reported_as_enabled_but_stopped(auto, tmp_path, monkeypatch):
    monkeypatch.setattr(auto.sys, 'platform', 'darwin')
    monkeypatch.setattr(auto, 'ROOT', tmp_path)
    monkeypatch.setattr(auto, 'snapshot', lambda: [])
    with pytest.raises(ValueError, match='saved as enabled'):
        auto.configure(True, False)
    assert auto.enabled() and not auto.status()['running']
    (tmp_path/'.git').write_text('gitdir: elsewhere')
    with pytest.raises(ValueError, match='development worktree'):
        auto.configure(True, True)
    monkeypatch.setattr(auto.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='requires macOS'):
        auto.configure(True, True)


def test_menu_stages_changes_without_writing(auto):
    choices = iter(['toggle', 'back'])
    ui = SimpleNamespace(pick=lambda *a, **k: next(choices))
    assert auto.menu(ui, False) is True
    assert not auto.settings_path().exists()


def test_commands_pause_resume_and_status(auto, tmp_path, capsys):
    row = session(auto, tmp_path)
    args = SimpleNamespace(scope='session', action='pause', repo='api', slot='1', json=False)
    assert auto.command(args) == 0
    assert auto.detail('claude', SID)['paused']
    args.action = 'resume'; auto.command(args)
    assert not auto.detail('claude', SID)['paused']
    capsys.readouterr()
    args.action, args.json = 'status', True
    auto.command(args)
    assert json.loads(capsys.readouterr().out)['sessions'][0]['sid'] == SID
    args.json = False; auto.command(args)
    assert SID in capsys.readouterr().out
    args.repo = 'missing'
    with pytest.raises(ValueError, match='specify one'):
        auto.command(args)
    with pytest.raises(ValueError, match='not enrolled'):
        auto.pause('missing', True)


def test_public_routes_and_readonly_modes(t_mod):
    parser = t_mod.build_parser()
    for words in (['system', 'recovery', 'status'], ['session', 'recovery', 'status', 'api', '1']):
        args = parser.parse_args(t_mod._canonical_argv(words + ['--json']))
        assert args.action == 'status' and args.json
        assert t_mod._command_readonly(words)
    assert not t_mod._command_readonly(['session', 'recovery', 'pause', 'api', '1'])
    assert 't config' in t_mod._group_help('system recovery')
    assert 'No picker' in t_mod._group_help('session recovery')
    for action in ('pause', 'resume'):
        assert parser.parse_args(t_mod._canonical_argv(['session', 'recovery', action, 'api', '1'])).action == action


def test_ssh_reconnect_pins_identity_and_stops_on_detach(auto, monkeypatch):
    enable(auto)
    expected = {'sid': SID, 'agent': 'claude', 'session': 'dev-api-1', 'cwd': '/repo'}
    monkeypatch.setattr(auto, 'ssh_probe', lambda *a: result(json.dumps(expected)))
    responses = iter([(255, 'Connection reset'), (0, '')])
    calls = []
    monkeypatch.setattr(auto, 'ssh_once', lambda *a: calls.append(a) or next(responses))
    monkeypatch.setattr(auto.time, 'sleep', lambda n: None)
    assert auto.reconnect('host', 'api', '1') == 0
    assert len(calls) == 2 and calls[0][1] == calls[1][1] == expected


@pytest.mark.parametrize('reason', ['Permission denied', 'Host key verification failed', 'REMOTE HOST IDENTIFICATION HAS CHANGED'])
def test_ssh_auth_errors_never_retry(auto, monkeypatch, reason):
    enable(auto)
    monkeypatch.setattr(auto, 'ssh_probe', lambda *a: result('{}'))
    monkeypatch.setattr(auto, 'ssh_once', lambda *a: (255, reason))
    assert auto.reconnect('host', 'api', '1') == 255


def test_ssh_reused_slot_and_cancel_stop_reconnect(auto, monkeypatch):
    enable(auto)
    probes = iter([result('{"sid":"original"}'), result('{"sid":"replacement"}')])
    monkeypatch.setattr(auto, 'ssh_probe', lambda *a: next(probes))
    monkeypatch.setattr(auto, 'ssh_once', lambda *a: (255, 'connection reset'))
    monkeypatch.setattr(auto.time, 'sleep', lambda n: None)
    with pytest.raises(ValueError, match='changed'):
        auto.reconnect('host', 'api', '1')
    monkeypatch.setattr(auto, 'ssh_probe', lambda *a: result('{}'))
    monkeypatch.setattr(auto.time, 'sleep', lambda n: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert auto.reconnect('host', 'api', '1') == 130


def test_enrollment_preserves_identity_pause_and_detects_lossy_settings(auto, tmp_path, monkeypatch):
    row = session(auto, tmp_path, pid='42')
    auto.pause('claude:'+SID, True)
    auto.enroll([row])
    assert auto.detail('claude', SID)['paused']
    recovery = {'process': lambda pid: {'args': 'claude --model pinned'},
                'panes': lambda s: [row], 'owner': lambda s,p: row}
    monkeypatch.setattr(auto, 'helper', lambda name: recovery)
    monkeypatch.setattr(auto, 'run', lambda *a, **k: result('/tmp/socket\n'))
    assert auto.snapshot() == [row]
    auto.update(lambda d: d['sessions'].clear())
    auto.enroll([{**row, 'pid': '42'}])
    enrolled = auto.status()['sessions'][0]
    assert enrolled['sid'] == SID and enrolled['options'] == ['--model', 'pinned']
    assert enrolled['phase'] == 'unknown'
    auto.update(lambda d: d['sessions'].clear())
    recovery['process'] = lambda pid: {'args': 'codex -c key=value'}
    auto.enroll([{**row, 'pid': '42'}])
    assert auto.status()['sessions'][0]['safe'] is False
    auto.update(lambda d: d['sessions'].clear())
    recovery['process'] = lambda pid: {'args': '"unterminated'}
    auto.enroll([{**row, 'pid': '42'}])
    assert auto.status()['sessions'][0]['safe'] is False


def test_supervisor_inflight_and_changed_generation_prevent_second_launch(auto, tmp_path, monkeypatch):
    enable(auto)
    row = session(auto, tmp_path, boot='boot-one', supervisor_pid=42, supervisor_start='stamp')
    real = auto.helper
    monkeypatch.setattr(auto, 'helper', lambda n: {'process': lambda p: {'start': 'stamp'}} if n=='t_recovery' else real(n))
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    assert not runtime.started
    auto.write(auto.settings_path(), {'enabled': False})
    auto.Engine(runtime).tick()
    assert not runtime.started


def test_launch_tracks_close_and_recovery_stop_without_native_agents(auto, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto.subprocess, 'call', lambda a: 0)
    assert auto.launch('claude', ['claude']) == 0
    enable(auto)
    def native(argv):
        auto.event('claude', {'session_id': SID, 'event': 'SessionStart'})
        return 0
    monkeypatch.setattr(auto.subprocess, 'call', native)
    assert auto.launch('claude', ['claude']) == 0
    assert not auto.detail('claude', SID)['open']
    def interrupted(argv):
        auto.event('claude', {'session_id': SID, 'event': 'SessionStart'})
        raise KeyboardInterrupt
    monkeypatch.setattr(auto.subprocess, 'call', interrupted)
    assert auto.launch('claude', ['claude']) == 130
    assert auto.detail('claude', SID)['phase'] == 'interrupted'
    actual = auto.helper
    monkeypatch.setattr(auto, 'helper', lambda n: {**actual(n), 'supervise': lambda *a, **k: 7} if n=='t_offline' else actual(n))
    assert auto.launch('codex', ['codex']) == 7
    assert auto.prefix('claude')[-2:] == ['launch', 'claude']


def test_guard_requires_enabled_identity_and_native_writer_release(auto, tmp_path, monkeypatch):
    row = session(auto, tmp_path, agent='codex')
    with pytest.raises(ValueError, match='disabled'):
        auto.guard('codex', SID, row['cwd'])
    enable(auto)
    auto.update(lambda d: d['sessions']['codex:'+SID]['workspace'].update(inode=-1))
    with pytest.raises(ValueError, match='workspace changed'):
        auto.guard('codex', SID, row['cwd'])
    row = session(auto, tmp_path, agent='codex')
    cache = Path(auto.os.environ['XDG_CACHE_HOME'])/'claude-sessions/rollouts'/SID
    cache.parent.mkdir(parents=True); cache.write_text('/saved/transcript.jsonl')
    calls = []
    monkeypatch.setattr(auto, 'helper', lambda n: {'assert_released': lambda *a, **k: calls.append((a,k)),
                                                'cursor_process': lambda info: 'cursor-agent' in info['args']})
    auto.guard('codex', SID, row['cwd'])
    assert calls[0][1]['rollout'] == '/saved/transcript.jsonl'
    row = session(auto, tmp_path, agent='cursor')
    with pytest.raises(ValueError, match='Cursor clients'):
        auto.guard('cursor', SID, row['cwd'])
    monkeypatch.setattr(auto, 'run', lambda args, **k: result('1 init\n') if args[0]=='ps' else result(rc=1))
    auto.guard('cursor', SID, row['cwd'])


def test_runtime_restore_uses_strict_locked_launcher_and_preserves_flags(auto, tmp_path, monkeypatch):
    row = session(auto, tmp_path)
    calls = []
    def runner(args, **kwargs):
        if args[0] == 'git':
            return result(rc=1)
        calls.append((args, kwargs)); return result()
    monkeypatch.setattr(auto, 'run', runner)
    auto.Runtime().restore(row, 'continue')
    args, kw = calls[0]
    assert args[:2] == ['zsh', '-lic']
    assert '_T_RESUME_ALLOW_UNAVAILABLE=0' in args[2]
    assert '_dev_with_launch_locks' in args[2] and '_t_auto_restore_claimed' in args[2]
    assert SID in args[2] and '--resume' in args[2]
    with pytest.raises(ValueError, match='settings'):
        auto.Runtime().restore({**row, 'safe': False}, '')
    with pytest.raises(ValueError, match='identity'):
        auto.Runtime().restore({**row, 'workspace': {}}, '')
    monkeypatch.setattr(auto, 'run', lambda *a, **k: result(rc=1, stderr='blocked'))
    with pytest.raises(ValueError, match='blocked'):
        auto.Runtime().restore(row, '')


def test_runtime_stop_preserves_draft_and_waits_for_verified_supervisor(auto, tmp_path, monkeypatch):
    enable(auto)
    row = session(auto, tmp_path, phase='outage', supervisor_pid=42, supervisor_start='stamp')
    owner = dict(sid=SID, agent='claude', pid='43', socket='/socket', pane='%1', session='dev-api-1', pane_pid='40')
    states = iter([{'start':'stamp'}, {'start':'stamp'}, {}])
    calls = []
    recovery = {'current': lambda o: True, 'process': lambda p: next(states),
                'tmux': lambda *a: calls.append(a) or result('unsent draft'),
                'panes': lambda s: [owner], 'owner': lambda s,p: {}}
    monkeypatch.setattr(auto, 'helper', lambda n: recovery)
    monkeypatch.setattr(auto.os, 'kill', lambda p,s: calls.append((p,s)))
    monkeypatch.setattr(auto.time, 'sleep', lambda n: None)
    auto.Runtime().stop(row, owner)
    assert auto.read(auto.root()/'draft-generation.json')['screen'] == 'unsent draft'
    assert any('kill-session' in call for call in calls)
    recovery['current'] = lambda o: False
    with pytest.raises(ValueError, match='no longer'):
        auto.Runtime().stop(row, owner)
    recovery['current'] = lambda o: True
    recovery['process'] = lambda p: {}
    with pytest.raises(ValueError, match='supervisor'):
        auto.Runtime().stop(row, owner)
    recovery['process'] = lambda p: {'start':'stamp'}
    recovery['tmux'] = lambda *a: result(rc=1)
    with pytest.raises(ValueError, match='draft'):
        auto.Runtime().stop(row, owner)


def test_connection_probe_is_bounded_and_never_submits_work(auto, monkeypatch):
    seen = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(auto.socket, 'create_connection', lambda *a, **k: seen.append((a,k)) or Connection())
    assert auto.Runtime().available({'agent': 'claude'})
    assert seen[0][1]['timeout'] == 3
    monkeypatch.setattr(auto.socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert not auto.Runtime().available({'agent': 'claude'})


def test_identity_and_internal_attach_never_choose_reused_slot(auto, monkeypatch):
    row = dict(sid=SID, agent='claude', session='dev-api-1', cwd='/repo', socket='/fixture-socket', workspace={'inode': 2})
    monkeypatch.setattr(auto, 'workspace', lambda cwd: {'inode': 2})
    monkeypatch.setattr(auto, 'snapshot', lambda: [row])
    assert auto.identity('api', '1') == row
    calls = []
    monkeypatch.setattr(auto.subprocess, 'call', lambda args: calls.append(args) or 0)
    assert auto.internal(['attach', SID, 'api', '1', auto.fingerprint(row)]) == 0
    assert calls == [['tmux', '-S', '/fixture-socket', 'attach-session', '-t', '=dev-api-1']]
    with pytest.raises(ValueError, match='another'):
        auto.internal(['attach', 'different', 'api', '1', auto.fingerprint(row)])
    with pytest.raises(ValueError, match='another'):
        auto.internal(['attach', SID, 'api', '1', 'replaced-workspace'])
    assert auto.fingerprint(row) == auto.fingerprint({**row, 'socket': '/new-socket'})
    with pytest.raises(ValueError, match='not a verified'):
        auto.identity('api', '2')
    with pytest.raises(ValueError, match='unknown'):
        auto.internal([])
    auto.internal(['identity', 'api', '1'])
    monkeypatch.setattr(auto, 'watch', lambda: 4)
    assert auto.internal(['watch']) == 4


def test_ssh_probe_preserves_login_shell_and_does_not_prompt(auto, monkeypatch):
    calls = []
    monkeypatch.setattr(auto, 'run', lambda args, **kw: calls.append((args,kw)) or result())
    auto.ssh_probe('remote', 'api', '1')
    assert 'BatchMode=yes' in calls[0][0]
    assert calls[0][0][-1].startswith('zsh -lic ')


def test_watch_excludes_other_workers_and_records_failure(auto, monkeypatch):
    sequence = iter([True, False])
    monkeypatch.setattr(auto, 'enabled', lambda: next(sequence))
    monkeypatch.setattr(auto.Engine, 'tick', lambda self: (_ for _ in ()).throw(ValueError('fixture unavailable')))
    monkeypatch.setattr(auto.time, 'sleep', lambda n: None)
    assert auto.watch() == 0

    assert auto.read(auto.root()/'error.json')['reason'] == 'fixture unavailable'
    monkeypatch.setattr(auto.fcntl, 'flock', lambda *a: (_ for _ in ()).throw(BlockingIOError()))
    assert auto.watch() == 0


def test_ssh_terminal_passthrough_and_cancel(auto, monkeypatch):
    calls = []
    class Process:
        def __init__(self): self.stderr = io.BytesIO(b'Connection lost\n'); self.cancel = False
        def wait(self):
            if self.cancel:
                self.cancel = False
                raise KeyboardInterrupt
            return 255
        def terminate(self): calls.append('terminate')
    process = Process()
    monkeypatch.setattr(auto.subprocess, 'Popen', lambda args, **kw: calls.append(args) or process)
    monkeypatch.setattr(auto.sys, 'stderr', SimpleNamespace(buffer=io.BytesIO()))
    expected = {'sid': SID}
    rc, text = auto.ssh_once('host', expected, 'api', '1')
    assert rc == 255 and 'Connection lost' in text
    assert 'ServerAliveInterval=15' in calls[0]
    assert 'recovery_rc=254' in calls[0][-1]
    process = Process(); process.cancel = True
    assert auto.ssh_once('host', expected, 'api', '1')[0] == 130
    assert 'terminate' in calls


def test_ssh_unavailable_probe_retries_but_auth_probe_stops(auto, monkeypatch):
    enable(auto)
    probes = iter([result('{}'), result(rc=1, stderr='connection refused'), result(rc=1, stderr='Permission denied')])
    monkeypatch.setattr(auto, 'ssh_probe', lambda *a: next(probes))
    monkeypatch.setattr(auto, 'ssh_once', lambda *a: (255, 'reset'))
    monkeypatch.setattr(auto.time, 'sleep', lambda n: None)
    assert auto.reconnect('host', 'api', '1') == 255
    monkeypatch.setattr(auto, 'ssh_probe', lambda *a: result(rc=1))
    with pytest.raises(ValueError, match='cannot pin'):
        auto.reconnect('host', 'api', '1')


def test_atomic_failure_cleans_temporary_file(auto, monkeypatch):
    path = auto.root()/'file.json'
    monkeypatch.setattr(auto.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('fixture')))
    with pytest.raises(OSError):
        auto.write(path, {})
    assert list(auto.root().iterdir()) == []


def test_birth_record_stamps_only_matching_managed_pane(auto, tmp_path, monkeypatch):
    enable(auto)
    monkeypatch.setenv('TMUX', '/socket,1,0'); monkeypatch.setenv('TMUX_PANE', '%1')
    calls = []
    def runner(args, **kw):
        calls.append(args)
        return result(f'dev-api-1\t{tmp_path}\t/socket\t%1\t40\n') if args[:2]==['tmux', 'display-message'] else result(rc=1)
    monkeypatch.setattr(auto, 'run', runner)
    token = auto.begin('claude', ['claude'], str(tmp_path))
    auto.event('claude', {'session_id': SID, 'event': 'SessionStart', 'transcript_path': '/fixture'}, token)
    assert auto.status()['sessions'][0]['session'] == 'dev-api-1'
    assert any('CLAUDE_RESUME_ID' in args and SID in args for args in calls)
    newer = '11234567-89ab-cdef-0123-456789abcdef'
    auto.event('claude', {'session_id': newer, 'event': 'SessionStart'}, token)
    assert not auto.detail('claude', SID)['open']


def test_disable_preserves_sibling_hooks_in_shared_group(auto):
    path = Path.home()/'.claude/settings.json'
    auto.write(path, {'hooks': {'Stop': [{'matcher': '*', 'hooks': [
        {'command': 'python /t/libexec/t_auto_recovery.py event claude Stop'},
        {'command': 'user-owned-hook', 'type': 'command'}]}]}})
    auto.hooks(False)
    assert auto.read(path)['hooks']['Stop'] == [{'matcher': '*', 'hooks': [
        {'command': 'user-owned-hook', 'type': 'command'}]}]


def test_codex_retry_exhaustion_and_auxiliary_threads(auto, tmp_path):
    enable(auto)
    token = auto.begin('codex', ['codex'], str(tmp_path))
    observer = auto.CodexObserver(token)
    observer.client({'id': 1, 'method': 'thread/resume'})
    observer.server({'id': 1, 'result': {'thread': {'id': SID}}})
    observer.client({'id': 2, 'method': 'thread/start', 'params': {'ephemeral': True}})
    observer.server({'id': 2, 'result': {'thread': {'id': 'auxiliary'}}})
    assert observer.sid == SID
    observer.server({'method': 'thread/status/changed', 'params': {'threadId': SID,
        'status': {'type': 'active', 'activeFlags': []}}})
    assert auto.detail('codex', SID)['phase'] == 'running'
    observer.server({'method': 'error', 'params': {'threadId': SID, 'error': {
        'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': None}}}}})
    observer.server({'method': 'turn/completed', 'params': {'threadId': SID, 'turn': {
        'status': 'failed', 'error': {'codexErrorInfo': {'responseTooManyFailedAttempts': {'httpStatusCode': None}}}}}})
    assert auto.detail('codex', SID)['phase'] == 'outage'


def test_unsupported_codex_invocation_launches_normally_without_enrollment(auto, monkeypatch):
    enable(auto)
    calls = []
    monkeypatch.setattr(auto.subprocess, 'call', lambda args: calls.append(args) or 4)
    argv = ['codex', '--profile', 'custom']
    assert auto.launch('codex', argv) == 4
    assert calls == [argv]
    assert not auto.status()['sessions']


def test_new_user_turn_supersedes_failed_recovery_attempt(auto, tmp_path):
    enable(auto)
    session(auto, tmp_path)
    class UserActed(FakeRuntime):
        def restore(self, row, prompt):
            auto.update(lambda data: data['sessions']['claude:'+SID].update(phase='idle', turn='new'))
            raise ValueError('stale attempt')
    auto.Engine(UserActed()).tick()
    assert auto.detail('claude', SID)['phase'] == 'idle'


def test_close_pause_and_disable_win_before_native_launch(auto, tmp_path, monkeypatch):
    enable(auto)
    row = session(auto, tmp_path)
    monkeypatch.setenv('T_RECOVERY_RESTORE_GENERATION', 'generation')
    auto.pause('claude:'+SID, True)
    with pytest.raises(ValueError, match='intent changed'):
        auto.begin('claude', auto.resume_argv(row), row['cwd'])
    auto.pause('claude:'+SID, False)
    auto.retire('claude', SID)
    with pytest.raises(ValueError, match='intent changed'):
        auto.begin('claude', auto.resume_argv(row), row['cwd'])
    auto.write(auto.settings_path(), {'enabled': False})
    with pytest.raises(ValueError, match='disabled before launch'):
        auto.launch('claude', auto.resume_argv(row))


@pytest.mark.parametrize('record', [
    {'type': 'user', 'message': {'content': '[Request interrupted by user]'}},
    {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}},
    {'type': 'system', 'subtype': 'turn_duration'},
])
def test_native_claude_cancellation_never_receives_continuation(auto, tmp_path, record):
    enable(auto)
    tx = tmp_path/'native.jsonl'; tx.write_text(json.dumps(record)+'\n')
    session(auto, tmp_path, transcript=str(tx))
    runtime = FakeRuntime()
    auto.Engine(runtime).tick()
    assert runtime.started[0][1] == ''
