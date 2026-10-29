"""Selected-source checkpoints use disposable repositories and mocked SSH."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_history_data import history, store, app, conversation
from test_history_resume import git_repo, saved_row

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def protocol():
    spec = importlib.util.spec_from_file_location('resume_source_test', ROOT / 'libexec/t_resume_source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(app, history, store, git_repo, monkeypatch):
    workspace = Path(app.cfg.worktree_root) / 'repo/repo-1'
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(workspace)])
    row = store.merge(saved_row(app, git_repo, workspace))
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'source', lambda *a: Path('/unused-transcript'))
    monkeypatch.setattr(app, 'codex_released', lambda *a, **kw: None)
    request = dict(version=1, identity={k: row[k] for k in ('repo', 'agent', 'sid')},
                   association={**row['associations'][0], 'host': 'other', 'available': 'cached'})
    return workspace, row, request


def test_selected_prefers_lifecycle_over_newer_transcript(protocol):
    row = conversation(associations=[dict(evidence='resume', observed=1), dict(evidence='transcript', observed=20)])
    assert protocol.selected(row)['evidence'] == 'resume'
    assert protocol.selected(conversation()) is None


@pytest.mark.parametrize('dirty', [False, True])
def test_export_carries_actual_unpushed_tip_and_checkpoint(app, source, history, protocol, dirty):
    path, row, request = source
    Path(path, 'tracked').write_text('unpushed\n')
    history.run(['git', '-C', str(path), 'commit', '-am', 'Unpushed'])
    tip = history.run(['git', '-C', str(path), 'rev-parse', 'HEAD']).strip()
    if dirty:
        Path(path, 'tracked').write_text('staged\n')
        history.run(['git', '-C', str(path), 'add', 'tracked'])
        Path(path, 'tracked').write_text('unstaged too\n')
        Path(path, 'new code.py').write_text('ordinary untracked\n')
        Path(path, '.gitignore').write_text('ignored\n')
        Path(path, 'ignored').write_text('private ignored\n')
    result = protocol.export(app, request)
    assert result['checkpoint'] is dirty
    assert result['association'] == request['association']
    assert result['revision'] == history.run(['git', '-C', str(path), 'rev-parse', 'HEAD']).strip()
    assert (result['revision'] != tip) is dirty
    assert not history.run(['git', '-C', str(path), 'status', '--porcelain'])
    if dirty:
        assert history.run(['git', '-C', str(path), 'show', 'HEAD:tracked']) == 'unstaged too\n'
        assert history.run(['git', '-C', str(path), 'show', 'HEAD:new code.py']) == 'ordinary untracked\n'
    data = base64.b64decode(result['bundle'])
    assert hashlib.sha256(data).hexdigest() == result['sha256']
    assert not list(app.store.directory.glob('resume-*'))


@pytest.mark.parametrize('change, message', [('generation', 'association changed'), ('sid', 'identity'), ('branch', 'branch changed'), ('unrelated', 'unrelated'), ('merge', 'unfinished'), ('live', 'live owner'), ('reused', 'another conversation'), ('root', 'Git root'), ('identity', 'repository identity')])
def test_export_refuses_unsafe_source(app, source, history, protocol, store, monkeypatch, change, message):
    path, row, request = source
    if change == 'generation':
        request['association']['generation'] = 'wrong'
    elif change == 'sid':
        request['identity']['sid'] = 'other'
    elif change == 'branch':
        history.run(['git', '-C', str(path), 'checkout', '-b', 'wrong'])
    elif change == 'unrelated':
        history.run(['git', '-C', str(path), 'checkout', '--orphan', 'orphan'])
        history.run(['git', '-C', str(path), 'commit', '-m', 'unrelated'])
        # Keep the expected branch name while replacing its lineage.
        history.run(['git', '-C', str(path), 'branch', '-D', 'dev/repo-1'])
        history.run(['git', '-C', str(path), 'branch', '-m', 'dev/repo-1'])
    elif change == 'merge':
        gitdir = Path(history.run(['git', '-C', str(path), 'rev-parse', '--absolute-git-dir']).strip())
        (gitdir / 'MERGE_HEAD').write_text(row['associations'][0]['commit'])
    elif change == 'live':
        monkeypatch.setattr(app, 'owners', lambda: [dict(sid='selected', cwd=str(path))])
    elif change == 'reused':
        store.merge(saved_row(app, row['associations'][0]['commit'], path, sid='new', observed=2))
    elif change == 'root':
        original = app.run
        monkeypatch.setattr(app, 'run', lambda argv, **kw: '/different/root' if argv[-1] == '--show-toplevel' else original(argv, **kw))
    elif change == 'identity':
        history.run(['git', '-C', str(path), 'remote', 'set-url', 'origin', 'https://github.com/other/repo.git'])
        monkeypatch.setattr(app, 'same_repository', lambda a, b: a == b)
    with pytest.raises(history.HistoryError, match=message):
        protocol.export(app, request)


@pytest.mark.parametrize('name', ['.env', '.codex/auth.json', 'private.pem', '.ssh/id_ed25519', '.npmrc'])
def test_private_files_are_refused_without_partial_checkpoint(app, source, history, protocol, name):
    path, row, request = source
    private = path / name
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text('synthetic secret')
    head = history.run(['git', '-C', str(path), 'rev-parse', 'HEAD'])
    with pytest.raises(history.HistoryError, match='private credentials'):
        protocol.export(app, request)
    assert history.run(['git', '-C', str(path), 'rev-parse', 'HEAD']) == head
    assert private.read_text() == 'synthetic secret'


def test_failed_commit_preserves_original_index_and_work(app, source, history, protocol):
    path, row, request = source
    Path(path, 'tracked').write_text('staged')
    history.run(['git', '-C', str(path), 'add', 'tracked'])
    Path(path, 'tracked').write_text('unstaged')
    index = Path(history.run(['git', '-C', str(path), 'rev-parse', '--git-path', 'index']).strip())
    before = index.read_bytes()
    hooks = path / 'custom-hooks'
    hooks.mkdir()
    hook = hooks / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o700)
    history.run(['git', '-C', str(path), 'config', 'core.hooksPath', str(hooks)])
    with pytest.raises(history.HistoryError, match='checkpoint failed'):
        protocol.export(app, request)
    assert index.read_bytes() == before
    assert Path(path, 'tracked').read_text() == 'unstaged'
    assert history.run(['git', '-C', str(path), 'show', ':tracked']) == 'staged'


def remote_result(app, source, protocol, monkeypatch):
    path, row, request = source
    value = protocol.export(app, request)
    app.cfg.hosts = {'other': 'test.invalid'}
    remote = {**row, 'associations': [request['association']]}
    original = protocol.subprocess.run
    calls = []
    def ssh(argv, **kw):
        if argv[0] == 'ssh':
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')
        return original(argv, **kw)
    monkeypatch.setattr(protocol.subprocess, 'run', ssh)
    return value, remote, calls


def test_reconcile_imports_into_private_ref_and_preserves_destination(app, source, history, protocol, monkeypatch):
    path, row, request = source
    Path(path, 'tracked').write_text('carried\n')
    value, remote, calls = remote_result(app, source, protocol, monkeypatch)
    before = history.run(['git', '-C', app.cfg.repos['repo'], 'rev-parse', 'HEAD'])
    assert protocol.reconcile(app, 'repo', remote) == value['revision']
    assert history.run(['git', '-C', app.cfg.repos['repo'], 'rev-parse', 'HEAD']) == before
    assert not history.run(['git', '-C', app.cfg.repos['repo'], 'for-each-ref', 'refs/t/resume/'])
    assert not list(app.store.directory.glob('resume-*'))
    assert 'zsh -lic' in calls[0][-1] and 'BatchMode=yes' in calls[0]


@pytest.mark.parametrize('change', ['hash', 'identity', 'version', 'token', 'revision', 'bundle', 'error', 'exit', 'oversize', 'json'])
def test_reconcile_rejects_invalid_or_refused_artifact(app, source, history, protocol, monkeypatch, change):
    value, row, calls = remote_result(app, source, protocol, monkeypatch)
    if change == 'hash': value['sha256'] = 'wrong'
    if change == 'identity': value['identity'] = {}
    if change == 'version': value['version'] = 2
    if change == 'token': value['token'] = '../bad'
    if change == 'revision': value['revision'] = 'invalid'
    if change == 'bundle': value['bundle'] = '!!!'
    if change == 'error': value['error'] = 'unsafe workspace'
    if change == 'oversize': monkeypatch.setattr(protocol, 'MAX_BUNDLE', 2)
    original = protocol.subprocess.run
    monkeypatch.setattr(protocol.subprocess, 'run', lambda argv, **kw: SimpleNamespace(returncode=1 if change == 'exit' else 0, stdout='invalid' if change == 'json' else json.dumps(value), stderr='') if argv[0] == 'ssh' else original(argv, **kw))
    with pytest.raises(history.HistoryError):
        protocol.reconcile(app, 'repo', row)


@pytest.mark.parametrize('failure', ['unregistered', 'transport', 'timeout', 'old'])
def test_unavailable_source_warns_and_retains_local_revision(app, source, history, protocol, monkeypatch, capsys, failure):
    value, row, calls = remote_result(app, source, protocol, monkeypatch)
    if failure == 'unregistered': app.cfg.hosts = {}
    def fail(*a, **kw):
        if failure == 'timeout': raise subprocess.TimeoutExpired('ssh', 90)
        return SimpleNamespace(returncode=255 if failure == 'transport' else 2, stdout='', stderr='invalid choice: __resume-source')
    monkeypatch.setattr(protocol.subprocess, 'run', fail)
    assert protocol.reconcile(app, 'repo', row) == ''
    assert 'locally available code' in capsys.readouterr().err


def test_local_resume_does_not_checkpoint(app, source, protocol, monkeypatch):
    path, row, request = source
    monkeypatch.setattr(protocol.subprocess, 'run', lambda *a, **kw: pytest.fail('local resume must not reconcile'))
    assert protocol.reconcile(app, 'repo', row) == ''


def test_checkpoint_respects_existing_git_index_lock(app, source, history, protocol):
    path, row, request = source
    Path(path, 'tracked').write_text('outstanding')
    index = Path(history.run(['git', '-C', str(path), 'rev-parse', '--git-path', 'index']).strip())
    lock = Path(str(index) + '.lock')
    lock.write_text('other Git owner')
    with pytest.raises(history.HistoryError, match='index is being changed'):
        protocol.export(app, request)
    assert lock.read_text() == 'other Git owner'


@pytest.mark.parametrize('phase', ['stage', 'commit', 'bundle'])
def test_checkpoint_races_preserve_work_and_refuse_transfer(app, source, history, protocol, monkeypatch, phase):
    path, row, request = source
    Path(path, 'tracked').write_text('checkpoint work')
    original_run = protocol.subprocess.run
    stages = 0
    def racing(argv, **kw):
        nonlocal stages
        result = original_run(argv, **kw)
        if argv[0] == 'git' and 'add' in argv:
            stages += 1
            if phase == 'stage' and stages == 1:
                Path(path, 'tracked').write_text('newer concurrent work')
        if phase == 'commit' and argv[0] == 'git' and 'commit' in argv:
            Path(path, 'tracked').write_text('newer concurrent work')
        if phase == 'bundle' and argv[0] == 'git' and 'bundle' in argv and 'create' in argv:
            Path(path, 'tracked').write_text('newer concurrent work')
        return result
    monkeypatch.setattr(protocol.subprocess, 'run', racing)
    with pytest.raises(history.HistoryError, match='source changed'):
        protocol.export(app, request)
    assert Path(path, 'tracked').read_text() == 'newer concurrent work'
    index = Path(history.run(['git', '-C', str(path), 'rev-parse', '--git-path', 'index']).strip())
    assert not Path(str(index) + '.lock').exists()


def test_reconcile_refuses_diverged_local_conversation(app, source, history, protocol, monkeypatch):
    path, row, request = source
    Path(path, 'tracked').write_text('source line')
    value, remote, calls = remote_result(app, source, protocol, monkeypatch)
    canonical = app.cfg.repos['repo']
    Path(canonical, 'tracked').write_text('local line')
    history.run(['git', '-C', canonical, 'commit', '-am', 'Local independent work'])
    local = history.run(['git', '-C', canonical, 'rev-parse', 'HEAD']).strip()
    remote['associations'].append(dict(host='local', workspace=canonical, commit=local))
    with pytest.raises(history.HistoryError, match='diverged'):
        protocol.reconcile(app, 'repo', remote)
    assert history.run(['git', '-C', canonical, 'rev-parse', 'HEAD']).strip() == local
    assert not history.run(['git', '-C', canonical, 'for-each-ref', 'refs/t/resume/'])


def test_transcript_transport_failure_tries_local_source(app, history, monkeypatch, tmp_path):
    from test_history_data import codex_rows, transcript_file
    local = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows('/saved/repo', sid='selected'))
    row = conversation(sid='selected', associations=[dict(workspace='/saved/repo')], sources=[
        dict(host='other', path='/remote/rollout-selected.jsonl', updated=20),
        dict(host='local', path=str(local), updated=10)])
    app.cfg.hosts = {'other': 'offline.invalid'}
    original = history.run
    def failed(argv, **kw):
        if argv[0] == 'rsync': raise history.HistoryError('unavailable')
        return original(argv, **kw)
    monkeypatch.setattr(history, 'run', failed)
    assert app.source(row) == local


def test_transcript_transport_failure_uses_validated_private_cache(app, history, monkeypatch, tmp_path):
    from test_history_data import codex_rows, transcript_file
    cached = transcript_file(app.store.directory / 'transcripts/codex/rollout-selected.jsonl', codex_rows('/saved/repo', sid='selected'))
    row = conversation(sid='selected', associations=[dict(workspace='/saved/repo')], sources=[dict(host='other', path='/remote/rollout-selected.jsonl')])
    app.cfg.hosts = {'other': 'offline.invalid'}
    monkeypatch.setattr(history, 'run', lambda *a, **kw: (_ for _ in ()).throw(history.HistoryError('unavailable')))
    assert app.source(row) == cached
    cached.write_text(cached.read_text().replace('selected', 'wrong'))
    with pytest.raises(history.HistoryError, match='cached transcript identity changed'):
        app.source(row)


def test_reconcile_compares_unrecorded_exact_local_tip(app, source, history, protocol, monkeypatch, store):
    path, row, request = source
    value, remote, calls = remote_result(app, source, protocol, monkeypatch)
    # The exact local workspace advances after the shared recorded revision.
    Path(path, 'tracked').write_text('new local work')
    history.run(['git', '-C', str(path), 'commit', '-am', 'New local tip'])
    remote['associations'].append(row['associations'][0])
    with pytest.raises(history.HistoryError, match='diverged'):
        protocol.reconcile(app, 'repo', remote)


def test_native_source_writer_claim_covers_checkpoint(app, source, protocol, monkeypatch):
    from contextlib import contextmanager
    import os
    path, row, request = source
    native = Path(os.environ['CODEX_HOME']) / 'thread-writer-locks'
    native.mkdir()
    states = []
    @contextmanager
    def claim(sid):
        states.append('held')
        yield
        states.append('released')
    original = protocol.checkpoint
    def checked(*args):
        assert states == ['held']
        return original(*args)
    monkeypatch.setattr(app, 'native_writer_claim', claim)
    monkeypatch.setattr(protocol, 'checkpoint', checked)
    protocol.export(app, request)
    assert states == ['held', 'released']


def test_new_private_file_during_staging_is_never_committed(app, source, history, protocol, monkeypatch):
    path, row, request = source
    Path(path, 'tracked').write_text('outstanding')
    original = protocol.subprocess.run
    def stage(argv, **kw):
        if argv[0] == 'git' and 'read-tree' in argv:
            Path(path, '.env').write_text('synthetic concurrent secret')
        return original(argv, **kw)
    monkeypatch.setattr(protocol.subprocess, 'run', stage)
    head = history.run(['git', '-C', str(path), 'rev-parse', 'HEAD'])
    with pytest.raises(history.HistoryError, match='private credentials'):
        protocol.export(app, request)
    assert history.run(['git', '-C', str(path), 'rev-parse', 'HEAD']) == head


def test_saved_resume_preparation_carries_code_into_fresh_destination(app, source, history, protocol, monkeypatch, tmp_path):
    from test_history_data import codex_rows, transcript_file
    path, row, request = source
    Path(path, 'carried.py').write_text('checkpoint on prior host\n')
    value, remote, calls = remote_result(app, source, protocol, monkeypatch)
    transcript = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(path, sid='selected'))
    remote['sources'] = [dict(host='local', path=str(transcript))]
    monkeypatch.setattr(app, 'source', lambda *a: transcript)
    app.cfg.handoff_for = lambda agent: 't'  # Explicit custom transfer opt-in.
    result = app.resume('repo', remote, detached=True, prepare_only=True)
    session, destination = result.split('\t')
    assert session == 'dev-repo-2'
    assert Path(destination, 'carried.py').read_text() == 'checkpoint on prior host\n'
    assert history.run(['git', '-C', destination, 'rev-parse', 'HEAD']).strip() == value['revision']
    assert Path(path, 'carried.py').read_text() == 'checkpoint on prior host\n'
    assert not list(Path(app.cfg.worktree_root).glob('**/*history-reservation'))


@pytest.mark.parametrize('payload', ['not JSON', '{"version":2}'])
def test_private_rpc_returns_safety_errors_as_versioned_json(app, history, t_mod, monkeypatch, capsys, payload):
    original = history.runpy.run_path
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    monkeypatch.setattr(history.runpy, 'run_path', lambda path: {'HistoryCommand': lambda *a: app} if str(path).endswith('t_history.py') else original(path))
    assert t_mod.main(['__resume-source', payload]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['version'] == 1 and result['error']


@pytest.mark.parametrize('local_copy', [False, True])
def test_native_remote_saved_resume_stops_before_any_transfer(app, source, history, monkeypatch, local_copy):
    path, row, request = source
    row['associations'][0]['host'] = 'other'
    row['sources'] = [dict(host='local' if local_copy else 'other', path='/unused', updated=1)]
    def forbidden(*args, **kwargs):
        pytest.fail('native handoff must not transfer or allocate')
    for name in ('source', 'reconcile_source', 'materialize_repository', 'destination', 'install_transcript'):
        monkeypatch.setattr(app, name, forbidden)
    with pytest.raises(history.HistoryError, match='native desktop app.*footer run location'):
        app.resume('repo', row, detached=True, prepare_only=True)
