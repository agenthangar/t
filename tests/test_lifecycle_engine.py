"""Lifecycle safety and crash recovery, with disposable stores and effect adapters."""
import base64
import fcntl
from contextlib import nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SID = '12345678-1234-1234-1234-123456789abc'
TOKEN = 'a' * 32


@pytest.fixture
def engine(tmp_path, monkeypatch):
    for name in ('HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'CODEX_HOME'):
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.setenv(name, str(folder))
    spec = importlib.util.spec_from_file_location('lifecycle_test', ROOT / 'libexec/t_lifecycle.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Adapter:
    def __init__(self, fail=None):
        self.events = []
        self.fail = fail
        self.output_lines = []
    def call(self, action, *args):
        self.events.append((action, args))
        if action == self.fail:
            raise ValueError('injected failure')
        return 'verified-owner' if action == 'ready' else ''
    def output(self, value):
        self.output_lines.append(value)


@pytest.mark.parametrize('metadata', ['missing', 'elsewhere', 'same', 'corrupt'])
@pytest.mark.parametrize('fresh', [False, True])
def test_new_native_preflight_scopes_writers_and_preserves_store_errors(engine, tmp_path, metadata, fresh):
    home = Path(os.environ['CODEX_HOME'])
    locks = home / 'thread-writer-locks'
    locks.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    info = workspace.stat()
    provenance = f'{info.st_dev}:{info.st_ino}' if fresh else None
    if metadata == 'corrupt':
        (home / 'state_5.sqlite').write_text('not a database')
    elif metadata != 'missing':
        with sqlite3.connect(home / 'state_5.sqlite') as db:
            db.execute('create table threads (id text, cwd text)')
            db.execute('insert into threads values (?, ?)',
                       (SID, str(workspace if metadata == 'same' else tmp_path / 'elsewhere')))
    lock = locks / (SID + '.lock')
    with lock.open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        inode = lock.stat().st_ino
        if metadata in ('same', 'corrupt') or (metadata == 'missing' and not fresh):
            message = {'same': 'owns this workspace', 'corrupt': 'ownership index',
                       'missing': 'could not be matched to its worktree'}[metadata]
            with pytest.raises(ValueError, match=message):
                engine.native_preflight('codex', '', str(workspace), new_workspace=provenance)
        else:
            evidence = engine.native_preflight('codex', '', str(workspace), new_workspace=provenance)
            assert evidence == {'unresolved_writers': [SID] if metadata == 'missing' else []}
        assert lock.stat().st_ino == inode
        assert engine.OWNERSHIP['_lock_held'](str(lock))


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_new_workspace_provenance_rejects_replaced_directory(engine, tmp_path, replacement):
    workspace = tmp_path / 'worktree'
    workspace.mkdir()
    info = workspace.stat()
    provenance = f'{info.st_dev}:{info.st_ino}'
    old = tmp_path / 'old'
    workspace.rename(old)
    if replacement == 'directory':
        workspace.mkdir()
    else:
        workspace.symlink_to(old, target_is_directory=True)
    with pytest.raises(ValueError, match='new workspace identity changed'):
        engine.native_preflight('codex', '', str(workspace), new_workspace=provenance)


@pytest.mark.parametrize('failed', ['preflight', 'resume', 'ready', 'record', None])
def test_resume_never_records_readiness_before_exact_owner(engine, monkeypatch, tmp_path, failed):
    native = []
    monkeypatch.setattr(engine, 'native_preflight', lambda *a: native.append(a))
    monkeypatch.setattr(engine, 'verify_ready', lambda *a: None)
    adapter = Adapter(failed)
    args = ['dev-repo-1', str(tmp_path), SID, 'codex']
    if failed:
        with pytest.raises(ValueError):
            engine.execute('resume', args, adapter, token=TOKEN)
    else:
        engine.execute('resume', args, adapter, token=TOKEN)
    actions = [e[0] for e in adapter.events]
    if failed in ('preflight', 'resume', 'ready'):
        assert 'record' not in actions
    if failed == 'preflight':
        assert not native
    else:
        assert native == [('codex', SID, str(tmp_path))]
    # A retry after an ambiguous launch only observes readiness. It must never
    # repeat a launch which could have succeeded before its reply was lost.
    if failed in ('resume', 'ready', 'record'):
        adapter.fail = None
        before = len(adapter.events)
        engine.execute('resume', args, adapter, token=TOKEN)
        assert 'resume' not in [e[0] for e in adapter.events[before:]]
        assert 'preflight' not in [e[0] for e in adapter.events[before:]]
    if not failed:
        count = len(adapter.events)
        assert engine.execute('resume', args, adapter, token=TOKEN) == 'verified-owner'
        assert len(adapter.events) == count


def test_close_records_only_after_native_release(engine, monkeypatch, tmp_path):
    adapter = Adapter()
    def held(*args):
        assert 'stop' in [e[0] for e in adapter.events]
        raise ValueError('writer held')
    monkeypatch.setattr(engine, 'wait_released', held)
    args = ['dev-repo-1', str(tmp_path), SID, 'codex']
    with pytest.raises(ValueError, match='writer held'):
        engine.execute('close', args, adapter, token=TOKEN)
    assert 'record' not in [e[0] for e in adapter.events]
    adapter.events.clear()
    monkeypatch.setattr(engine, 'wait_released', lambda *a: None)
    engine.execute('close', args, adapter, token=TOKEN)
    assert [e[0] for e in adapter.events] == ['record']


@pytest.mark.parametrize('recycle', [None, 'during-inspection', 'before-stop'])
@pytest.mark.parametrize('path', ['close', 'transfer'])
def test_stop_never_targets_a_recycled_session_name(engine, tmp_path, recycle, path):
    identity, killed = tmp_path / 'identity', tmp_path / 'killed'
    identity.write_text('$1')
    script = '''
source "$TEST_ROOT/zsh/lifecycle.zsh"
source "$TEST_ROOT/zsh/resume.zsh"
tmux() {
  case $1 in
    list-sessions) print dev-repo-1 ;;
    display-message)
      case ${@[-1]} in
        '#{session_id}') cat "$TEST_IDENTITY" ;;
        '#{session_path}') print -r -- "$TEST_WORKSPACE" ;;
      esac ;;
    kill-session)
      print -r -- "${@[-1]}" >> "$TEST_KILLED"
      [[ ${@[-1]} == "$(cat "$TEST_IDENTITY"):" ]] ;;
  esac
}
_dev_session_sid() {
  [[ $TEST_RECYCLE != during-inspection ]] || print -rn -- '$2' > "$TEST_IDENTITY"
  print -r -- "$TEST_SID"
}
_dev_agent_of_session() { print claude; }
_dev_session_claude_pid() {
  [[ $TEST_RECYCLE != before-stop ]] || print -rn -- '$2' > "$TEST_IDENTITY"
  return 0
}
'''
    class ShellClose(Adapter):
        def call(self, action, *args):
            self.events.append((action, args))
            if action == 'record':
                return ''
            if action == 'stop' and recycle == 'before-stop':
                identity.write_text('$2')
            env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
            env.update(TEST_ROOT=str(ROOT), TEST_WORKSPACE=str(tmp_path),
                       TEST_IDENTITY=str(identity), TEST_KILLED=str(killed),
                       TEST_SID=SID, TB_SID=SID, TB_CWD=str(tmp_path), TB_AGENT='claude',
                       TEST_RECYCLE=recycle or '', DEV_WORKTREE_ROOT='')
            command = (['_tbeam_kill_owner'] if action == 'transfer-stop' else
                       ['_t_lifecycle_effect', action, *args])
            result = subprocess.run(['zsh', '-f', '-c', script + '\n' +
                                     shlex.join(command)],
                                    env=env, text=True, capture_output=True, timeout=10)
            if result.returncode:
                raise ValueError(result.stderr or 'selected owner exited')
            return result.stdout.strip()
    adapter = ShellClose()
    args = ['dev-repo-1', str(tmp_path), SID, 'claude']
    def stop():
        if path == 'close':
            return engine.execute('close', args, adapter, token=TOKEN)
        return adapter.call('transfer-stop')
    if recycle:
        with pytest.raises(ValueError):
            stop()
        assert 'record' not in [event[0] for event in adapter.events]
    else:
        stop()
        assert killed.read_text().strip() == '$1:'
    assert not killed.exists() or '$2:' not in killed.read_text()


class MoveAdapter(Adapter):
    def __init__(self, fail=None):
        super().__init__(fail)
        self.receipts = {}
        self.started = False
        self.stopped = False
    def call(self, action, *args):
        if action != 'peer':
            return super().call(action, *args)
        host, encoded = args
        request = json.loads(base64.b64decode(encoded))
        stage = request['stage']
        self.events.append((stage, request['args']))
        if stage == self.fail:
            raise ValueError('injected failure')
        if stage == 'stop':
            self.stopped = True
        if stage == 'launch':
            assert self.stopped
            self.started = True
        values = {'inspect': {'origin': 'origin'}, 'reserve': '/destination', 'prepare': 'refs/heads/t-transfer/' + TOKEN,
                  'manifest': dict(path='/source.jsonl', agent='codex', sid=SID, workspace='/source'),
                  'stage': '/staged.jsonl', 'launch': 'dev-repo-2'}
        return json.dumps(values.get(stage, True))


@pytest.mark.parametrize('failure', ['inspect', 'reserve', 'prepare', 'preflight', 'stop', 'released',
                                    'publish', 'manifest', 'stage', 'install', 'launch', 'ready', 'finish', None])
def test_transfer_failure_boundary_retains_recoverable_receipts(engine, failure):
    adapter = MoveAdapter(failure)
    args = ['', 'peer', '/source', SID, 'codex', 'repo', '1', '1']
    if failure and failure != 'finish':
        with pytest.raises(ValueError):
            engine.execute('move', args, adapter, token=TOKEN)
    else:
        engine.execute('move', args, adapter, token=TOKEN)
    value = engine.Operation('move', args, token=TOKEN).value
    if failure in ('inspect', 'reserve', 'prepare', 'preflight', 'manifest', 'stage'):
        assert not adapter.stopped
    if failure not in ('ready', 'finish', None):
        assert not adapter.started
    assert (value['phase'] == 'complete') == (failure is None)
    if failure == 'finish':
        assert value['phase'] == 'cleanup-pending'
    if failure and failure not in ('manifest', 'stage', 'stop'):
        adapter.fail = None
        engine.execute('move', args, adapter, token=TOKEN)
        assert engine.Operation('move', args, token=TOKEN).value['phase'] == 'complete'


def test_peer_receipt_survives_lost_reply(engine):
    adapter = Adapter()
    request = dict(version=1, id=TOKEN, identity=['/worktree', SID, 'codex'], stage='launch', args=[])
    assert engine.execute_peer(request, adapter) == ''
    assert engine.execute_peer(request, adapter) == ''
    assert [e[0] for e in adapter.events] == ['peer-launch']
    status = engine.execute_peer(dict(request, stage='status'), adapter)
    assert 'launch' in status['receipts']
    for change in (dict(version=2), dict(stage='arbitrary'), dict(identity=['/other', SID, 'codex'])):
        with pytest.raises(ValueError):
            engine.execute_peer(dict(request, **change), adapter)


def test_operation_identity_and_private_atomic_storage(engine, tmp_path):
    operation = engine.Operation('close', ['owner'], token=TOKEN)
    operation.save('created')
    assert operation.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='identity changed'):
        engine.Operation('move', ['owner'], token=TOKEN)
    with pytest.raises(ValueError, match='invalid lifecycle'):
        engine.Operation('close', [], token='../escape')
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    operation.path.unlink()
    operation.path.symlink_to(outside)
    with pytest.raises(ValueError, match='unsafe'):
        operation.save('changed')
    assert outside.read_text() == 'keep'
    with engine.claim(tmp_path / 'claims', 'worktree'):
        assert list((tmp_path / 'claims').glob('*.lock'))
    (tmp_path / 'unsafe').symlink_to(tmp_path / 'claims', target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe'):
        with engine.claim(tmp_path / 'unsafe', 'worktree'):
            pytest.fail('unsafe claim accepted')


def manifest(engine, tmp_path, agent='codex'):
    source = tmp_path / 'rollout.jsonl'
    data = dict(type='session_meta', payload=dict(id=SID, cwd='/original', cli_version='0.160.0')) if agent == 'codex' else dict(sessionId=SID)
    source.write_text(json.dumps(data) + '\n')
    return engine.transcript_manifest(agent, SID, '/source', source)


def test_transfer_validates_bytes_identity_and_divergence(engine, tmp_path, monkeypatch):
    value = manifest(engine, tmp_path)
    home = Path(os.environ['CODEX_HOME'])
    (home / 'thread-writer-locks').mkdir()
    staged = Path(engine.stage_import(TOKEN, value))
    staged.write_bytes(Path(value['path']).read_bytes())
    target = Path(engine.install_transfer(TOKEN, value, '/destination'))
    assert target.read_bytes() == staged.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert engine.selected_rollout(SID) == target
    assert target.relative_to(home).as_posix() == 'sessions/1970/01/01/rollout-1970-01-01T00-00-00-' + SID + '.jsonl'
    assert engine.install_transfer(TOKEN, value, '/destination') == str(target)
    target.write_text('divergent')
    with pytest.raises(ValueError, match='diverges'):
        engine.install_transfer(TOKEN, value, '/destination')
    assert target.read_text() == 'divergent'
    staged.write_text('corrupt')
    with pytest.raises(ValueError):
        engine.install_transfer(TOKEN, value, '/destination')
    assert target.read_text() == 'divergent'


@pytest.mark.parametrize('agent', ['claude', 'codex'])
def test_native_writer_or_unidentified_writer_blocks_publication(engine, tmp_path, monkeypatch, agent):
    value = manifest(engine, tmp_path, agent)
    staged = Path(engine.stage_import(TOKEN, value))
    staged.write_bytes(Path(value['path']).read_bytes())
    monkeypatch.setattr(engine, 'native_preflight', lambda *a: (_ for _ in ()).throw(ValueError('unidentified writer')))
    with pytest.raises(ValueError, match='unidentified'):
        engine.install_transfer(TOKEN, value, '/destination')
    assert not list(Path(os.environ['CODEX_HOME']).glob('sessions/**/*.jsonl'))


def test_claude_transfer_and_invalid_manifest(engine, tmp_path):
    value = manifest(engine, tmp_path, 'claude')
    staged = Path(engine.stage_import(TOKEN, value))
    staged.write_bytes(Path(value['path']).read_bytes())
    target = Path(engine.install_transfer(TOKEN, value, '/destination'))
    assert target.read_bytes() == staged.read_bytes()
    for change in (dict(sid='../escape'), dict(agent='unknown'), dict(workspace='relative')):
        with pytest.raises(ValueError):
            engine.stage_import(TOKEN, dict(value, **change))
    with pytest.raises(ValueError):
        engine.stage_import('../escape', value)
    for agent, data in [('codex', b'{}'), ('claude', b'{}'), ('unknown', b'{}')]:
        with pytest.raises(ValueError):
            engine.verify_transcript(agent, SID, '/source', data)
    with pytest.raises(ValueError, match='missing or unsafe'):
        engine.transcript_manifest('codex', SID, '/source', tmp_path / 'missing')


def test_native_release_is_bounded_and_never_assumes_missing_contract(engine, monkeypatch, tmp_path):
    engine.wait_released('claude', SID)
    monkeypatch.setitem(engine.OWNERSHIP, '_lock_dir', lambda _: None)
    with pytest.raises(ValueError, match='native lock contract'):
        engine.wait_released('codex', SID)
    monkeypatch.setitem(engine.OWNERSHIP, '_lock_dir', lambda _: tmp_path)
    monkeypatch.setitem(engine.OWNERSHIP, '_lock_held', lambda _: True)
    with pytest.raises(ValueError, match='still held'):
        engine.wait_released('codex', SID, timeout=0)
    monkeypatch.setitem(engine.OWNERSHIP, '_lock_held', lambda _: False)
    engine.wait_released('codex', SID)


def test_shell_protocol_handles_disconnects_and_preserves_exit_status(engine, monkeypatch, capsys):
    adapter = engine.ShellAdapter()
    monkeypatch.setattr(engine.sys, 'stdin', io.StringIO(json.dumps(dict(code=0, output='hello\n')) + '\n'))
    assert adapter.call('ready', 'literal; $(no-command)') == 'hello'
    assert json.loads(capsys.readouterr().out)['args'] == ['literal; $(no-command)']
    for response in ('', '{}', json.dumps(dict(code=17, output='')) + '\n'):
        monkeypatch.setattr(engine.sys, 'stdin', io.StringIO(response))
        with pytest.raises(ValueError) as error:
            adapter.call('stop')
        if '17' in response:
            assert error.value.code == 17
    adapter.output('hello')
    adapter.output('')


def test_auxiliary_transcripts_preserve_bytes_and_refuse_divergence(engine, tmp_path):
    source = tmp_path / 'rollout.jsonl'
    value = manifest(engine, tmp_path, 'claude')
    source.with_suffix('.origin').write_text('first-host\n')
    child = tmp_path / SID / 'subagents' / 'agent-child.jsonl'
    child.parent.mkdir(parents=True)
    child.write_text('child conversation\n')
    value = engine.transcript_manifest('claude', SID, '/source', source)
    staged = Path(engine.stage_import(TOKEN, value))
    staged.write_bytes(source.read_bytes())
    for asset in value['assets']:
        (staged.parent / asset['name']).write_bytes(Path(asset['path']).read_bytes())
    destination = Path(engine.install_transfer(TOKEN, value, '/destination'))
    assert destination.with_suffix('.origin').read_text() == 'first-host\n'
    landed_child = destination.parent / SID / 'subagents/agent-child.jsonl'
    assert landed_child.read_text() == child.read_text()
    landed_child.write_text('independent destination changes\n')
    with pytest.raises(ValueError, match='diverges'):
        engine.install_transfer(TOKEN, value, '/destination')
    assert landed_child.read_text() == 'independent destination changes\n'
    assert destination.read_bytes() == source.read_bytes()
    child.unlink()
    child.symlink_to(source)
    with pytest.raises(ValueError, match='unsafe conversation asset'):
        engine.transcript_manifest('claude', SID, '/source', source)
    for name in ('assets/../outside', '/outside', 'assets//outside'):
        with pytest.raises(ValueError, match='unsafe conversation asset name'):
            engine.stage_import(TOKEN, dict(value, assets=[dict(name=name)]))


def test_loaded_writer_readiness_requires_selected_native_lock(engine):
    import fcntl
    folder = Path(os.environ['CODEX_HOME']) / 'thread-writer-locks'
    folder.mkdir()
    with (folder / (SID + '.lock')).open('w') as selected:
        with pytest.raises(ValueError, match='has not loaded'):
            engine.verify_ready('codex', SID, timeout=0)
        fcntl.flock(selected.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine.verify_ready('codex', SID, timeout=0)
        with pytest.raises(ValueError, match='claimed the selected writer'):
            with engine.native_writer_claim(SID):
                pytest.fail('published over a loaded conversation')
        with pytest.raises(ValueError, match='has not loaded'):
            engine.verify_ready('codex', '22345678-1234-1234-1234-123456789abc', timeout=0)
    with engine.native_writer_claim(SID):
        pass


def test_source_generation_change_prevents_checkpoint(engine, tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(work), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q', '-b', 'work')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '-qm', 'initial')
    evidence = engine.workspace_evidence(str(work))
    engine.source_preflight(str(work))
    git('checkout', '-qb', 'another-owner')
    with pytest.raises(ValueError, match='generation changed'):
        engine.checkpoint(str(work), SID, evidence)
    (work / '.git/index.lock').touch()
    with pytest.raises(ValueError, match='changing the source index'):
        engine.source_preflight(str(work))
    (work / '.git/index.lock').unlink()
    git('checkout', '--detach', '-q')
    with pytest.raises(ValueError, match='source is detached'):
        engine.source_preflight(str(work))


def test_final_manifest_stages_assets_created_while_source_was_live(engine, tmp_path):
    value = manifest(engine, tmp_path, 'claude')
    request = dict(version=1, id=TOKEN, identity=['/source', SID, 'claude'],
                   stage='stage', args=[value])
    staged = Path(engine.execute_peer(request, Adapter()))
    child = tmp_path / SID / 'new-subagents' / 'child.jsonl'
    child.parent.mkdir(parents=True)
    child.write_text('written before source exit\n')
    final = engine.transcript_manifest('claude', SID, '/source', tmp_path / 'rollout.jsonl')
    assert engine.execute_peer(dict(request, args=[final]), Adapter()) == str(staged)
    assert (staged.parent / 'assets/new-subagents').is_dir()


def test_cache_invalidation_is_targeted_and_cannot_roll_back_action(engine, monkeypatch):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    monkeypatch.setenv('COV_CORE_SOURCE', 'measured-parent')
    calls = []
    monkeypatch.setattr(engine.subprocess, 'Popen', lambda argv, **kw: calls.append((argv, kw)))
    engine._discovery_changed(['', 'user@mini'])
    assert calls[0][0][-3:] == ['__cache-event', 'local', 'user@mini']
    options = calls[0][1]
    assert options['start_new_session'] and options['close_fds']
    assert all(options[k] == subprocess.DEVNULL for k in ('stdin', 'stdout', 'stderr'))
    assert not any(key.startswith('COV_CORE_') for key in options['env'])
    def unavailable(*args, **kwargs):
        raise OSError('cache process unavailable')
    monkeypatch.setattr(engine.subprocess, 'Popen', unavailable)
    engine._discovery_changed(['local'])
