"""Desktop bridge isolation, command contracts, and durable mutation outcomes."""
import io
import json
import re
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'libexec'))
import t_core
import t_bridge


@pytest.fixture
def core(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, '_host_refresh_config', lambda: None)
    monkeypatch.setattr(t_mod, '_t_source_root', lambda: str(ROOT))
    cfg = t_mod.Config()
    cfg.repos = {'demo': str(tmp_path)}
    cfg.agent_default = 'claude'
    cfg.hosts = {'remote': 'user@remote'}
    cfg.beam_host = 'remote'
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    def run(argv, **kwargs):
        text = '12345678-1234-1234-1234-123456789abc\tcodex\t789' if '_dev_core_owner_metadata' in argv[-1] else '123\t$1\t%1\t456\t/tmp'
        return SimpleNamespace(returncode=0, stdout=text)
    monkeypatch.setattr(t_mod, '_run', run)
    return t_core.Core(t_mod, tmp_path / 'operations')


def test_import_dispatch_and_config(t_mod, monkeypatch):
    loaded = t_core.load_cli(ROOT)
    assert loaded.COMMAND_GROUPS == t_mod.COMMAND_GROUPS
    args = SimpleNamespace(verb='known')
    api = {'IMPLEMENTED': {'known': lambda cfg, args: 7}, 'cmd_pending': lambda cfg, args: 9}
    assert t_core.dispatch(api, None, args) == 7
    args.verb = 'unknown'
    assert t_core.dispatch(api, None, args) == 9 and args._verb == 'unknown'
    calls = []
    api = {'_config_diff': lambda a, b: b, '_config_state': lambda cfg: cfg,
           '_config_write': lambda *args: calls.append(args), '_current_local_rc': lambda: '/tmp/local',
           '_config_text': lambda *args: 'updated', '_host_refresh_config': lambda: None}
    assert t_core.persist_config(api, {}, 'old', {}) == {}
    assert t_core.persist_config(api, {'a': 1}, 'old', {}) == {'a': 1}
    assert calls == [('/tmp/local', 'old', 'updated')]


def test_terminal_contract_and_no_replay(core):
    record = core.prepare({'command': ['session', 'read', 'demo', '1'], 'cwd': str(ROOT)})
    assert record['state'] == 'prepared'
    assert record['attachment']['argv'][0:2] == ['zsh', '-lic']
    assert 'source ' in record['attachment']['argv'][2]
    assert 't session read demo 1' in record['attachment']['argv'][2]
    record = core.apply(record['operationId'])
    assert record['state'] == 'ready'
    with pytest.raises(t_core.CoreError, match='cannot be replayed'):
        core.apply(record['operationId'])
    assert core.call('operations.started', record)['state'] == 'running'
    assert core.call('operations.finished', dict(record, exitCode=1))['state'] == 'partial'
    with pytest.raises(t_core.CoreError):
        core.call('operations.finished', dict(record, exitCode=0))
    with pytest.raises(t_core.CoreError):
        core.call('operations.started', record)
    assert core.call('operations.reconcile', record)['state'] == 'partial'
    assert (core.state_root / (record['operationId'] + '.json')).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('params', [{'command': ['oops', 'bad']}, {'command': ['session']},
    {'command': ['session', 'open', 1]}, {'command': ['session', 'open', '\0']},
    {'method': 'oops'}, {'method': 'sessions.open', 'arguments': []},
    {'command': ['session', 'open'], 'cwd': '/nonexistent/t-core'}])
def test_prepare_invalid(core, params):
    with pytest.raises(t_core.CoreError):
        core.prepare(params)


@pytest.mark.parametrize('identifier', [None, '../bad', '0' * 36])
def test_unknown_operation(core, identifier):
    with pytest.raises(t_core.CoreError):
        core._read(identifier)


def test_unknown_survives_process_restart(core, monkeypatch):
    record = core.call('sessions.open', {'repo': 'demo', 'slot': '1'})
    monkeypatch.setattr(core, '_mutate', lambda *args: (_ for _ in ()).throw(OSError('secret error')))
    with pytest.raises(t_core.CoreError, match='may have changed'):
        core.apply(record['operationId'])
    other = t_core.Core(core.cli, core.state_root)
    assert other.call('operations.reconcile', record)['state'] == 'unknown'
    with pytest.raises(t_core.CoreError):
        other.apply(record['operationId'])
    for state in ('applying', 'ready', 'running'):
        core._record(dict(record, state=state))
        assert other.call('operations.reconcile', record)['state'] == 'unknown'


@pytest.mark.parametrize('action', ['open', 'resume', 'close', 'restart'])
def test_lifecycle_calls_guarded_shared_shell(core, monkeypatch, action):
    calls = []
    monkeypatch.setattr(core, '_run_shell', lambda snippet: calls.append(snippet) or {'exitCode': 0})
    monkeypatch.setattr(core.cli, 'zsh_capture', lambda _: 'sid\t/tmp\tdemo-1\tdetached\tactive\ttitle\tcodex')
    monkeypatch.setattr(core.cli, '_slot_select', lambda *args: {'cwd': '/tmp', 'sid': '12345678-1234-1234-1234-123456789abc', 'agent': 'codex'})
    record = core.call('sessions.' + action, {'repo': 'demo', 'slot': '1', 'agent': 'codex'})
    outcome = core.apply(record['operationId'])
    assert outcome['state'] == 'completed'
    assert len(calls) == 1
    if action == 'close':
        assert '_dev_with_launch_locks /tmp' in calls[0] and '_dev_kill_one dev-demo-1 1' in calls[0] and outcome['attachment'] is None
    elif action == 'restart':
        assert '_dev_with_launch_locks /tmp' in calls[0] and '_t_restart_slot dev-demo-1 /tmp 12345678-1234-1234-1234-123456789abc codex restart' in calls[0]
    else:
        assert calls[0].startswith('_t_' + action + ' demo 1')
        assert outcome['attachment']['validation']['session'] == 'dev-demo-1'
    with pytest.raises(t_core.CoreError):
        core.apply(record['operationId'])


def test_new_allocation_and_invalid_slot(core, monkeypatch):
    monkeypatch.setattr(core, '_run_shell', lambda snippet: {'session': 'dev-demo-9'})
    assert core._mutate('sessions.open', {'repo': 'demo', 'new': True})['session'] == 'dev-demo-9'
    monkeypatch.setattr(core, '_run_shell', lambda snippet: {})
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'demo', 'new': True})
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'bad', 'new': True})
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'demo', 'slot': 'fg'})
    with pytest.raises(t_core.CoreError):
        core.attachment('dev-demo-1;evil')


def test_shell_isolation(core, monkeypatch, tmp_path):
    def run(argv, **kwargs):
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['start_new_session']
        assert not any(key.startswith('COV_CORE_') for key in kwargs['env'])
        assert 'T_CORE_DETACH' not in kwargs['env']
        assert 'typeset +x T_CORE_DETACH T_CORE_RESULT' in argv[-1]
        path = re.search(r'T_CORE_RESULT=([^; ]+)', argv[-1])[1]
        Path(path).write_text('dev-demo-8\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setenv('COV_CORE_SOURCE', 'secret')
    monkeypatch.setattr(subprocess, 'run', run)
    assert core._run_shell('_t_open demo new') == {'exitCode': 0, 'session': 'dev-demo-8'}
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(t_core.CoreError):
        core._run_shell('_t_open demo 1')


def test_config_changes_use_same_atomic_writer(core, monkeypatch, tmp_path):
    cli = core.cli
    path = tmp_path / 'local.zsh'
    monkeypatch.setattr(cli, '_current_local_rc', lambda: str(path))
    result = core._mutate('hosts.change', {'action': 'add', 'alias': 'new', 'target': 'user@new'})
    assert result['changes']['REMOTE_HOSTS[new]'] == 'user@new'
    assert 'REMOTE_HOSTS[new]' in path.read_text()
    assert core._mutate('config.set', {'changes': {'DEV_AGENT_DEFAULT': 'codex'}})['changes'] == {'DEV_AGENT_DEFAULT': 'codex'}
    for changes in ([], {'SECRET': 'x'}, {'DEV_BRANCH': 'x\ny'}, {'DEV_BRANCH': 3}):
        with pytest.raises(t_core.CoreError):
            core._mutate('config.set', {'changes': changes})
    monkeypatch.setattr(cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert core._mutate('repositories.register', {'alias': 'newrepo', 'path': str(tmp_path)})['changes']['DEV_REPOS[newrepo]'] == str(tmp_path)
    with pytest.raises(t_core.CoreError):
        core._mutate('repositories.register', {'alias': ';bad', 'path': str(tmp_path)})
    monkeypatch.setattr(cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(t_core.CoreError):
        core._mutate('repositories.register', {'alias': 'good', 'path': str(tmp_path)})
    with pytest.raises(t_core.CoreError):
        core._mutate('bad', {})


def test_all_session_agent_families_and_snapshot(core, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='sid\t/tmp\tdemo-1\tdetached\tactive\tCodex title\tcodex\tmodel\n')
    monkeypatch.setattr(core.cli, '_run', run)
    snapshot = core.call('workspace.snapshot', {})
    assert snapshot['sessions'][0]['agent'] == 'codex'
    assert snapshot['sessions'][0]['title'] == 'Codex title'
    assert snapshot['hosts'][0]['default']
    assert set(tuple(cap['command']) for cap in snapshot['capabilities']) == {(group, action) for group, actions in core.cli.COMMAND_GROUPS.items() for action in actions}
    assert core.call('sessions.list', {})['sessions']
    core.sessions(core.config(), True)
    assert calls[-1][0] == 'ssh'
    assert core.remote_status == [{'host': 'remote', 'status': 'scanned'}]
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(t_core.CoreError):
        core.sessions(core.config())


def test_read_routes(core, monkeypatch):
    monkeypatch.setattr(core.cli, '_t_version', lambda: 'v1.2.3')
    assert core.call('initialize', {})['protocolVersion'] == 1
    assert core.call('repositories.list', {})['repositories'][0]['alias'] == 'demo'
    assert core.call('hosts.list', {})['hosts'][0]['alias'] == 'remote'
    assert 'DEV_BRANCH' in core.call('config.get', {})['config']
    monkeypatch.setattr(core.cli, '_install_probe', lambda: {'codex': 'ok'})
    assert core.call('agents.status', {}) == {'agents': {'codex': 'ok'}}
    monkeypatch.setattr(core.cli, '_mcp_call', lambda cfg, state, name, args: {'name': name})
    assert core.call('sessions.search', {})['name'] == 'find_sessions'
    assert core.call('sessions.detail', {})['name'] == 'session_detail'
    assert core.call('mcp.call', {'name': 'list_sessions'})['name'] == 'list_sessions'
    with pytest.raises(t_core.CoreError):
        core.call('mcp.call', {'name': 'bad'})
    with pytest.raises(t_core.CoreError):
        core.call('bad', {})
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='123\t$1\t%1\t456\t/tmp'))
    assert core.call('sessions.attach', {'repo': 'demo', 'slot': '1'})['attachment']['detachKeepsSession']
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(t_core.CoreError):
        core.call('sessions.attach', {'repo': 'demo', 'slot': '1'})


def test_bridge_wire_and_private_errors(core, monkeypatch):
    def call(method, params):
        print('private transcript')
        print('private stderr', file=sys.stderr)
        if method == 'failed':
            raise RuntimeError('private credential')
        if method == 'safe-error':
            raise t_core.CoreError('review', 'review required')
        return {'ok': True}
    monkeypatch.setattr(core, 'call', call)
    source = io.StringIO('\n'.join([json.dumps({'version': 1, 'id': 2, 'method': 'ok'}), '{bad',
                                   json.dumps({'version': 1, 'id': 3, 'method': 'failed'}),
                                   json.dumps({'version': 1, 'id': 4, 'method': 'safe-error'}),
                                   json.dumps({'version': 1, 'method': [], 'params': []})]) + '\n')
    destination = io.StringIO()
    t_bridge.serve(core, source, destination)
    text = destination.getvalue()
    assert 'private' not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]['result'] == {'ok': True}
    assert rows[1]['error']['code'] == 'invalid_request'
    assert rows[2]['error']['code'] == 'operation_failed'
    assert rows[3]['error']['code'] == 'review'
    assert rows[4]['error']['code'] == 'invalid_request'

from test_session_lifecycle import lifecycle, _run, _until, SID


def _bridge_call(base, env, method, params):
    request = json.dumps({'version': 1, 'id': 1, 'method': method, 'params': params}) + '\n'
    process = subprocess.run([sys.executable, str(ROOT / 'libexec/t_bridge.py')],
                             env=env, cwd=base, input=request, capture_output=True,
                             text=True, timeout=110)
    assert process.returncode == 0, process.stderr
    response = [json.loads(line) for line in process.stdout.splitlines() if 'event' not in json.loads(line)][-1]
    assert 'error' not in response, response
    return response['result']


def test_real_bridge_allocate_attach_and_close(lifecycle):
    base, env = lifecycle
    native_open = base / 'bin' / 'open'
    native_open.write_text('#!/bin/sh\nprintf native-open >> "$T_TEST_APP_LOG"\nexit 99\n')
    native_open.chmod(0o755)
    env['T_TEST_FRESH_SID'] = SID
    (base / 'cache' / 'claude-sessions').mkdir(parents=True)
    prepared = _bridge_call(base, env, 'sessions.open', {'repo': 'repo', 'new': True, 'agent': 'codex'})
    started = _bridge_call(base, env, 'operations.apply', {'operationId': prepared['operationId']})
    session = started['result']['session']
    assert session == 'dev-repo-1'
    log = base / 'agents.log'
    _until(lambda: log.exists() and log.read_text())
    tmux = str(base / 'bin' / 'tmux')
    pid_before = _run([tmux, 'display-message', '-p', '-t', '=' + session, '#{pane_pid}'], env=env, cwd=base).stdout
    attached = _bridge_call(base, env, 'sessions.attach', {'repo': 'repo', 'slot': '1'})
    assert attached['attachment']['validation']['session'] == session
    pid_after = _run([tmux, 'display-message', '-p', '-t', '=' + session, '#{pane_pid}'], env=env, cwd=base).stdout
    assert pid_before == pid_after
    assert _run([tmux, 'show-environment', '-g', 'T_CORE_DETACH'], env=env, cwd=base).returncode != 0
    assert _run([tmux, 'show-environment', '-g', 'T_CORE_RESULT'], env=env, cwd=base).returncode != 0
    assert len(log.read_text().splitlines()) == 1
    assert not (base / 'app.log').exists()
    prepared = _bridge_call(base, env, 'sessions.close', {'repo': 'repo', 'slot': '1'})
    closed = _bridge_call(base, env, 'operations.apply', {'operationId': prepared['operationId']})
    assert closed['state'] == 'completed'
    assert _run([tmux, 'has-session', '-t', '=' + session], env=env, cwd=base).returncode != 0


def test_real_bridge_resume_preserves_saved_conversation(lifecycle):
    import sqlite3
    base, env = lifecycle
    worktree = base / 'worktrees' / 'repo' / '1'
    worktree.parent.mkdir(parents=True)
    result = _run(['git', '-C', str(base / 'repo'), 'worktree', 'add', '-q', '-b',
                   'dev/repo-1', str(worktree), 'origin/main'], env=env, cwd=base)
    assert result.returncode == 0, result.stderr
    transcript = base / 'codex' / ('rollout-2026-10-11T00-00-00-' + SID + '.jsonl')
    transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(worktree), 'source': 'cli', 'cli_version': '0.160.0'}}) + '\n' + json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Resume my saved work'}]}}) + '\n')
    with sqlite3.connect(base / 'codex' / 'state_5.sqlite') as db:
        db.execute('create table threads (id text, rollout_path text, cwd text, name text, title text, updated_at integer, archived integer, source text, first_user_message text)')
        db.execute('insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (SID, str(transcript), str(worktree), None, 'Saved work', 1, 0, 'cli', 'Resume saved work'))
    newer_sid = '22345678-1234-1234-1234-123456789abc'
    newer = base / 'codex' / ('rollout-2026-10-12T00-00-00-' + newer_sid + '.jsonl')
    newer.write_text(transcript.read_text().replace(SID, newer_sid))
    with sqlite3.connect(base / 'codex' / 'state_5.sqlite') as db:
        db.execute('insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (newer_sid, str(newer), str(worktree), None, 'Newer history', 2, 0, 'cli', 'A different conversation'))
    (base / 'cache' / 'claude-sessions').mkdir(parents=True)
    prepared = _bridge_call(base, env, 'sessions.resume', {'repo': 'repo', 'slot': '1', 'sid': SID})
    resumed = _bridge_call(base, env, 'operations.apply', {'operationId': prepared['operationId']})
    assert resumed['attachment']['validation']['session'] == 'dev-repo-1'
    log = base / 'agents.log'
    _until(lambda: log.exists() and log.read_text())
    assert '|resume|' + SID in log.read_text()
    assert len(log.read_text().splitlines()) == 1
    assert transcript.exists()


def test_issue_reads_and_guarded_headless_launch(core, monkeypatch):
    owners = []
    report = {'matches': [], 'incomplete': False}
    class Owner:
        def __init__(self, cfg, api):
            owners.append(self)
        def issues(self, repo, state):
            return [{'repo': repo, 'state': state}]
        def issue(self, targets):
            return targets
        def scan(self, issue):
            return report
        def run_one(self, namespace, targets, attach):
            self.choose(report)
            assert not attach
            return {'host': 'remote', 'session': 'dev-demo-2'}
        def attach_argv(self, row, new_terminal):
            assert new_terminal
            return ['ssh', '-t', 'remote', 'zsh -lic tmux-attach']
    monkeypatch.setattr(t_core.runpy, 'run_path', lambda path: {'IssueCommand': Owner, 'split_targets': lambda words: ('demo', words[1:])})
    assert core.call('issues.list', {'repo': 'demo'}) == {'issues': [{'repo': 'demo', 'state': 'open'}]}
    assert core.call('issues.status', {'targets': ['demo', '1']}) == report
    result = core._mutate('issues.start', {'targets': ['demo', '1', '2']})
    assert len(result['attachments']) == 2
    assert result['attachments'][0]['argv'][0] == 'ssh'
    report['matches'] = [{'verified': True, 'session': 'dev-demo-2'}]
    core._mutate('issues.start', {'targets': ['demo', '1']})
    report['matches'] = [{'verified': True, 'resume': True}]
    assert core._mutate('issues.start', {'targets': ['demo', '1']})['state'] == 'partial'
    with pytest.raises(t_core.CoreError):
        core._mutate('issues.start', {'targets': []})
    with pytest.raises(t_core.CoreError):
        core._mutate('issues.start', {'targets': ['demo']})


def test_remote_attach_diagnostics_and_model_overrides(core, monkeypatch):
    calls = []
    monkeypatch.setattr(core.cli, '_run', lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout='123\t$1\t%1\t456\t/tmp'))
    descriptor = core.call('sessions.attach', {'repo': 'demo', 'slot': '1', 'host': 'remote'})['attachment']
    assert descriptor['argv'][0:3] == ['ssh', '-t', 'user@remote']
    assert 'zsh -lic ' in descriptor['argv'][-1]
    assert 'BatchMode=yes' in calls[0]
    with pytest.raises(t_core.CoreError):
        core.call('sessions.attach', {'repo': 'demo', 'slot': '1', 'host': 'unknown'})
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(t_core.CoreError):
        core.call('sessions.attach', {'repo': 'demo', 'slot': '1', 'host': 'remote'})
    assert core.call('system.diagnostics', {})['protocolVersion'] == 1
    snippets = []
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='123\t$1\t%1\t456\t/tmp'))
    monkeypatch.setattr(core, '_run_shell', lambda snippet: snippets.append(snippet) or {})
    core._mutate('sessions.open', {'repo': 'demo', 'slot': '1', 'agent': 'codex', 'model': 'some model', 'effort': 'high', 'fast': '1'})
    assert "DEV_MODEL[codex]='some model'" in snippets[0]
    assert 'DEV_EFFORT[codex]=high' in snippets[0]
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'demo', 'slot': '1', 'model': 1})
    with pytest.raises(t_core.CoreError):
        core._mutate('config.set', {'changes': {'DEV_AGENT_DEFAULT': 'evil'}})


def test_remote_mutations_share_installed_core_and_preserve_login_shell(core, monkeypatch):
    calls = []
    outcome = {'result': {'session': 'dev-demo-1', 'attachment': core.attachment('dev-demo-1')}}
    monkeypatch.setattr(core.cli, '_run', lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout=json.dumps(outcome)))
    result = core._mutate('sessions.open', {'repo': 'demo', 'slot': '1', 'host': 'remote', 'agent': 'codex'})
    assert result['host'] == 'remote'
    assert result['attachment']['argv'][0:3] == ['ssh', '-t', 'user@remote']
    assert calls[0][0] == 'ssh' and 'zsh -lic ' in calls[0][-1]
    assert 'from t_core import Core' in calls[0][-1]
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'demo', 'slot': '1', 'host': 'absent'})
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'demo', 'slot': '1', 'host': 'remote'})
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='malformed'))
    with pytest.raises(t_core.CoreError):
        core._mutate('sessions.open', {'repo': 'demo', 'slot': '1', 'host': 'remote'})
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({'result': {'session': 'dev-demo-1'}})))
    assert core._mutate('sessions.close', {'repo': 'demo', 'slot': '1', 'host': 'remote'})['host'] == 'remote'


def test_snapshot_preserves_local_state_when_remote_host_offline(core, monkeypatch):
    def run(argv, **kwargs):
        if argv[0] == 'ssh':
            return SimpleNamespace(returncode=255)
        return SimpleNamespace(returncode=0, stdout='sid\t/tmp\tdemo-1\tdetached\tactive\ttitle\tcodex\n')
    monkeypatch.setattr(core.cli, '_run', run)
    result = core.call('workspace.snapshot', {'all_hosts': True})
    assert len(result['sessions']) == 1
    assert result['remoteStatus'] == [{'host': 'remote', 'status': 'incomplete'}]
    core.config().hosts['remote'] = '-unsafe'
    assert core.call('workspace.snapshot', {'all_hosts': True})['remoteStatus'][0]['status'] == 'incomplete'


def test_progress_frames_do_not_replace_results(core, monkeypatch):
    record = core.prepare({'method': 'sessions.open', 'arguments': {'repo': 'demo', 'slot': '1'}})
    monkeypatch.setattr(core, '_mutate', lambda *args: {'session': 'dev-demo-1', 'attachment': core.attachment('dev-demo-1')})
    source = io.StringIO(json.dumps({'version': 1, 'id': 7, 'method': 'operations.apply', 'params': {'operationId': record['operationId']}}) + '\n')
    destination = io.StringIO()
    t_bridge.serve(core, source, destination)
    frames = [json.loads(line) for line in destination.getvalue().splitlines()]
    assert frames[0]['event'] == 'progress' and frames[0]['stage'] == 'applying'
    assert frames[1]['stage'] == 'completed'
    assert frames[-1]['result']['state'] == 'completed'
    core._record(dict(record, state='applying', attachment=core.attachment('dev-demo-1')))
    reconciled = core.call('operations.reconcile', {'operationId': record['operationId']})
    assert reconciled['state'] == 'unknown' and 'attachment' not in reconciled


def test_shell_pins_bundle_against_old_path_command(core, monkeypatch, tmp_path):
    stale = tmp_path / 'stale'
    stale.mkdir()
    command = stale / 't'
    command.write_text('#!/bin/sh\nexit 99\n')
    command.chmod(0o755)
    monkeypatch.setenv('PATH', str(stale) + ':/usr/bin:/bin')
    home = tmp_path / 'home'
    home.mkdir()
    for name in ('HOME', 'ZDOTDIR'):
        monkeypatch.setenv(name, str(home))
    for name in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.delenv('T_LOCAL_RC', raising=False)
    argv = core.shell('whence -p t; whence -p python3')
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            start_new_session=True, timeout=20)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[-2] == str(ROOT / 'bin/t')
    assert Path(lines[-1]).parent == Path(sys.executable).parent
    assert 'T_NO_AUTORELOAD=1' in argv[-1]


def test_config_array_domain_and_restart_refusal(core, monkeypatch, tmp_path):
    path = tmp_path / 'local.zsh'
    monkeypatch.setattr(core.cli, '_current_local_rc', lambda: str(path))
    result = core._mutate('config.set', {'changes': {'DEV_MODEL[codex]': 'desktop-model', 'DEV_AGENT[demo]': 'codex'}})
    assert result['changes']['DEV_MODEL[codex]'] == 'desktop-model'
    assert 'DEV_MODEL[codex]=desktop-model' in path.read_text()
    monkeypatch.setattr(core.cli, 'zsh_capture', lambda *args: '')
    monkeypatch.setattr(core.cli, '_slot_select', lambda *args: {'agent': 'codex', 'sid': '-', 'cwd': str(tmp_path)})
    with pytest.raises(ValueError, match='no verified conversation'):
        t_core.restart_target(core.api, core.config(), 'demo', '1', str(tmp_path))
    monkeypatch.setattr(core.cli, '_slot_select', lambda *args: {'agent': 'codex', 'sid': SID, 'cwd': '/missing'})
    with pytest.raises(ValueError, match='worktree is missing'):
        t_core.restart_target(core.api, core.config(), 'demo', '1', str(tmp_path))


def test_attachment_claim_rejects_replacement_and_changed_host(core, monkeypatch):
    original = core.attachment('dev-demo-1')
    assert core.call('attachments.validate', {'validation': original['validation']}) == {'valid': True}
    assert "exec tmux attach-session -t '$1'" in original['argv'][-1]
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='123\t$2\t%2\t456\t/tmp'))
    with pytest.raises(t_core.CoreError, match='attachment changed'):
        core.call('attachments.validate', {'validation': original['validation']})
    with pytest.raises(t_core.CoreError):
        core.call('attachments.validate', {'validation': []})
    remote = core.attachment('dev-demo-1', host='remote')
    core.config().hosts['remote'] = 'different@host'
    with pytest.raises(t_core.CoreError, match='attachment changed'):
        core.validate_attachment(remote['validation'])
    monkeypatch.setattr(core.cli, '_run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='malformed'))
    with pytest.raises(t_core.CoreError):
        core.attachment('dev-demo-1')


def test_readonly_mcp_routes_and_live_cd_shell(core):
    for flag in ('--install', '--list-tools', '--reindex'):
        command = ['mcp', flag]
        core.cli.build_parser().parse_args(command)
        assert core.prepare({'command': command})['command'] == command
    assert core.call('mcp.capabilities', {})['installCommand'] == ['mcp', '--install']
    record = core.prepare({'command': ['repo', 'cd', 'demo']})
    assert '&& exec zsh -l' in record['attachment']['argv'][-1]


def test_rejected_operation_is_actionable_without_effects(core, monkeypatch):
    record = core.prepare({'method': 'sessions.open', 'arguments': {'repo': 'bad', 'slot': '1'}})
    with pytest.raises(t_core.CoreError) as error:
        core.apply(record['operationId'])
    assert error.value.code == 'invalid_target'
    assert core.call('operations.reconcile', record)['state'] == 'rejected'
    record = core.prepare({'method': 'sessions.open', 'arguments': {'repo': 'demo', 'slot': '1'}})
    monkeypatch.setattr(core, '_mutate', lambda *args: (_ for _ in ()).throw(t_core.CoreError('operation_failed', 'possibly started')))
    with pytest.raises(t_core.CoreError) as error:
        core.apply(record['operationId'])
    assert error.value.code == 'unknown_outcome'
    assert core.call('operations.reconcile', record)['state'] == 'unknown'


def test_multi_issue_batch_retains_success_and_continues_after_conflict(core, monkeypatch):
    completed = []
    class Owner:
        def __init__(self, cfg, api):
            pass
        def run_one(self, namespace, targets, attach):
            completed.append(targets[-1])
            if targets[-1] == '2':
                raise t_core.CoreError('review_required', 'conflicting owner')
            return {'session': 'dev-demo-' + targets[-1], 'host': 'local'}
    monkeypatch.setattr(t_core.runpy, 'run_path', lambda path: {'IssueCommand': Owner, 'split_targets': lambda words: ('demo', words[1:])})
    record = core.prepare({'method': 'issues.start', 'arguments': {'targets': ['demo', '1', '2', '3']}})
    result = core.apply(record['operationId'])
    assert result['state'] == 'partial'
    assert completed == ['1', '2', '3']
    assert len(result['result']['attachments']) == 2
    assert result['result']['errors'][0]['target'] == '2'


def test_remote_repository_alias_is_authoritative_even_when_cwd_matches_local(core, monkeypatch):
    path = core.config().repos['demo']
    def run(argv, **kwargs):
        slot = 'different-1' if argv[0] == 'ssh' else 'demo-1'
        return SimpleNamespace(returncode=0, stdout=f'sid\t{path}\t{slot}\tdetached\tactive\ttitle\tcodex\n')
    monkeypatch.setattr(core.cli, '_run', run)
    result = core.call('workspace.snapshot', {'all_hosts': True})
    remote = next(row for row in result['sessions'] if row['host'] == 'remote')
    assert remote['repo'] == 'different' and remote['slot'] == 'different-1'


def test_reviewed_owner_metadata_and_remote_destination_are_bound(core, monkeypatch):
    before = core.prepare({'method': 'sessions.close', 'arguments': {'repo': 'demo', 'slot': '1', 'sid': SID}})
    assert '_owner' not in before['arguments'] and before['owner']['sid'] == SID
    with pytest.raises(t_core.CoreError) as error:
        core.prepare({'method': 'sessions.close', 'arguments': {'repo': 'demo', 'slot': '1', 'sid': '-'}})
    assert error.value.code == 'stale_target'
    with pytest.raises(t_core.CoreError):
        core.prepare({'method': 'sessions.close', 'arguments': {'repo': 'demo', 'slot': '1', 'sid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}})
    remote = core.prepare({'method': 'sessions.close', 'arguments': {'repo': 'demo', 'slot': '1', 'host': 'remote'}})
    original = remote['owner']
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if 'python3' in argv[-1]:
            assert 'expectedOwner=json.loads(sys.argv[3])' in argv[-1]
            assert original['identity'] in argv[-1].replace('\\t', '\t')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'result': {'session': 'dev-demo-1'}}))
        return SimpleNamespace(returncode=0, stdout=original['identity'])
    monkeypatch.setattr(core.cli, '_run', run)
    assert core.apply(remote['operationId'])['state'] == 'completed'
    second = core._record(dict(remote, operationId='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', state='prepared'))
    core.config().hosts['remote'] = 'changed@host'
    with pytest.raises(t_core.CoreError) as error:
        core.apply(second['operationId'])
    assert error.value.code == 'stale_target'
    assert core._read(second['operationId'])['state'] == 'rejected'


@pytest.mark.parametrize('action', ['close', 'restart'])
def test_destructive_shell_guard_refuses_same_pane_changed_conversation(lifecycle, action):
    from test_desktop_core_integration import _slot_identity, _save_claude_thread
    base, env = lifecycle
    prepared = _bridge_call(base, env, 'sessions.open', {'repo': 'repo', 'slot': '1', 'agent': 'claude'})
    _bridge_call(base, env, 'operations.apply', {'operationId': prepared['operationId']})
    _until(lambda: (base / 'agents.log').exists())
    identity, sid = _slot_identity(base, env)
    _save_claude_thread(base, sid)
    record = _bridge_call(base, env, 'sessions.' + action, {'repo': 'repo', 'slot': '1', 'sid': sid})
    owner = record['owner']
    replacement = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    _save_claude_thread(base, replacement)
    tmux = str(base / 'bin/tmux')
    assert _run([tmux, 'set-environment', '-t', '=dev-repo-1', 'CLAUDE_RESUME_ID', replacement], env=env, cwd=base).returncode == 0
    args = (['_dev_kill_one', owner['session'], '1', owner['identity'], owner['sid'], owner['agent'], owner['clientPid']]
            if action == 'close' else ['_t_restart_slot', owner['session'], str(base / 'worktrees/repo/1'), owner['sid'], owner['agent'], 'restart', owner['clientPid'], '', owner['identity']])
    command = 'source ' + str(ROOT / 't.plugin.zsh') + '; ' + __import__('shlex').join(args)
    guarded = _run(['zsh', '-lic', command], env=env, cwd=base)
    assert guarded.returncode == 75, guarded.stdout + guarded.stderr
    assert _slot_identity(base, env) == (identity, replacement)
    assert len((base / 'agents.log').read_text().splitlines()) == 1


def test_reviewed_idle_slot_close_preserves_empty_owner_contract(lifecycle):
    base, env = lifecycle
    worktree = base / 'worktrees/repo/1'
    worktree.parent.mkdir(parents=True)
    result = _run(['git', '-C', str(base / 'repo'), 'worktree', 'add', '-q', '-b',
                   'dev/repo-1', str(worktree), 'origin/main'], env=env, cwd=base)
    assert result.returncode == 0, result.stderr
    tmux = str(base / 'bin/tmux')
    assert _run([tmux, 'new-session', '-d', '-s', 'dev-repo-1', '-c', str(worktree), 'sleep 60'], env=env, cwd=base).returncode == 0
    record = _bridge_call(base, env, 'sessions.close', {'repo': 'repo', 'slot': '1', 'sid': '-'})
    assert record['owner']['sid'] == '' and record['owner']['clientPid'] == ''
    closed = _bridge_call(base, env, 'operations.apply', {'operationId': record['operationId']})
    assert closed['state'] == 'completed'
    assert _run([tmux, 'has-session', '-t', '=dev-repo-1:'], env=env, cwd=base).returncode != 0
    assert not (base / 'agents.log').exists()
