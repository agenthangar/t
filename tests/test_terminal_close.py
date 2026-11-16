"""Terminal-loss cleanup without native desktop windows or real agent state."""
import json
import fcntl
import os
from pathlib import Path
import pty
import shutil
import shlex
import sqlite3
import subprocess
import time

import pytest

from conftest import load_script, REPO_ROOT
from test_session_lifecycle import lifecycle, _shell, _run, _until, SID


@pytest.fixture
def close():
    return load_script(REPO_ROOT / 'libexec/t_terminal_close.py', 'terminal_close')


def test_real_terminal_loss_differs_from_detach(close):
    master, slave = pty.openpty()
    try:
        assert not close.hung_up(slave)
        os.close(master)
        master = None
        assert close.hung_up(slave)
    finally:
        if master is not None:
            os.close(master)
        os.close(slave)


@pytest.mark.parametrize('agent,screen,cursor,expected', [
    ('codex', 'OpenAI Codex\n› \n\n\x1b[2m? for shortcuts\x1b[0m\n', '1', True),
    ('claude', 'Welcome back\n❯ \n\x1b[2m─────\x1b[0m\n', '1', True),
    ('codex', '› \n? for shortcuts\n', '0', None),
    ('claude', '❯ \n─────\n', '0', None),
    ('codex', '› unsent draft\n', '0', False),
    ('codex', '› \x1b[2mImplement {feature}\x1b[0m\n', '0', True),
    ('codex', '› Implement {feature}\n', '0', False),
    ('codex', '› \nmore draft\n', '0', None),
    ('codex', '› previous prompt\nApproval needed\n', '1', None),
    ('codex', '› \nesc to interrupt\n', '0', None),
    ('cursor', '› \n', '0', None),
    ('codex', '', '0', None),
    ('codex', '› \n', '-', None),
])
def test_composer_proves_blank_current_input(close, agent, screen, cursor, expected):
    assert close.composer_empty(agent, screen, cursor) is expected


@pytest.mark.parametrize('kinds,expected', [
    ([], 'empty'), (['task_started'], 'busy'),
    (['task_started', 'task_complete'], 'idle'),
    (['task_started', 'task_complete', 'user_message'], 'busy'),
    (['task_started', 'turn_aborted'], 'unknown'),
    (['agent_message'], 'unknown'),
])
def test_codex_turn_state(close, tmp_path, kinds, expected):
    path = tmp_path / 'rollout'
    path.write_text(''.join(json.dumps({'type': 'event_msg', 'payload': {'type': kind}}) + '\n' for kind in kinds))
    assert close.transcript_state('codex', str(path))[0] == expected


def test_claude_turns_and_corrupt_evidence(close, tmp_path):
    path = tmp_path / 'transcript'
    records = [{'type': 'user', 'message': {'content': 'hello'}},
               {'type': 'assistant', 'message': {'stop_reason': 'end_turn'}}]
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
    assert close.transcript_state('claude', path)[0] == 'idle'
    with path.open('a') as file:
        file.write('{')
    assert close.transcript_state('claude', path)[0] == 'unknown'
    assert close.transcript_state('claude', '')[0] == 'unknown'


@pytest.mark.parametrize('response,expected', [('[]', True), ('[{"number": 1}]', False), ('error', None)])
def test_open_pr_check_needs_positive_evidence(close, monkeypatch, response, expected):
    def command(args, **kwargs):
        text = ('git@github.com:owner/repo.git' if 'get-url' in args else
                'dev/topic' if 'symbolic-ref' in args else response)
        return subprocess.CompletedProcess(args, 0, text, '')
    monkeypatch.setattr(close, 'run', command)
    assert close.prs_complete('/workspace', set()) is expected


def _attach(base, env, socket, count=1):
    master, slave = pty.openpty()
    process = subprocess.Popen([shutil.which('tmux'), '-S', socket, 'attach-session', '-t', '=dev-repo-1:'],
                               stdin=slave, stdout=slave, stderr=slave,
                               env={**env, 'TERM': 'xterm-256color'}, start_new_session=True)
    os.close(slave)
    _until(lambda: _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:',
                         '#{session_attached}'], env=env, cwd=base).stdout.strip() == str(count))
    _until(lambda: any(path.read_text() == str(process.pid) for path in
                      (Path(env['XDG_STATE_HOME']) / 't/terminal-close').glob('*.ready')))
    return master, process


@pytest.mark.parametrize('case', ['empty', 'completed', 'draft', 'dirty', 'open-pr', 'busy', 'detach', 'background', 'another-client'])
def test_public_terminal_closure(lifecycle, case):
    base, env = lifecycle
    screen = '\x1b[2J\x1b[HOpenAI Codex (v0.1)\r\n\r\n› ' + ('keep this draft' if case == 'draft' else '')
    env = {**env, 'T_TEST_AGENT_SCREEN': screen, 'T_TEST_AGENT_SECONDS': '180'}
    if case in ('completed', 'busy'):
        env['T_TEST_FRESH_SID'] = SID
    result = _shell(base, env, 't session open repo 1 --codex')
    assert result.returncode == 0, result.stderr
    worktree = base / 'worktrees/repo/repo-1'
    assert _run(['git', '-C', str(worktree), 'remote', 'set-url', 'origin', 'https://github.com/example/test.git'], env=env, cwd=base).returncode == 0
    gh = base / 'bin/gh'
    gh.write_text('#!/bin/sh\nprintf \'%s\\n\' \'' + ('[{"number":1}]' if case == 'open-pr' else '[]') + "'\n")
    gh.chmod(0o755)
    if case in ('completed', 'busy'):
        path = base / 'rollout.jsonl'
        records = [{'type': 'event_msg', 'payload': {'type': 'task_started'}}]
        if case == 'completed':
            records.append({'type': 'event_msg', 'payload': {'type': 'task_complete'}})
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        cache = Path(env['XDG_CACHE_HOME']) / 'claude-sessions/rollouts'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / SID).write_text(str(path))
    if case == 'dirty':
        (worktree / 'unfinished.txt').write_text('keep')
    background = subprocess.Popen(['sleep', '180'], cwd=worktree, env=env) if case == 'background' else None
    socket = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:', '#{socket_path}'], env=env, cwd=base).stdout.strip()
    hooks = _run([str(base / 'bin/tmux'), 'show-hooks', '-t', '=dev-repo-1:'], env=env, cwd=base)
    assert 't_terminal_close.py' in hooks.stdout
    master, process = _attach(base, env, socket)
    second = _attach(base, env, socket, 2) if case == 'another-client' else None
    try:
        if case == 'detach':
            _run([str(base / 'bin/tmux'), 'detach-client', '-s', 'dev-repo-1'], env=env, cwd=base)
            process.wait(timeout=5)
            # Leave the terminal alive until the detach observer has exited.
            time.sleep(1)
        else:
            os.close(master)
            master = None
            process.wait(timeout=5)
        def alive():
            return _run([str(base / 'bin/tmux'), 'has-session', '-t', '=dev-repo-1:'], env=env, cwd=base).returncode == 0
        if case in ('empty', 'completed'):
            _until(lambda: not alive(), seconds=25)
        else:
            _until(lambda: list((Path(env['XDG_STATE_HOME']) / 't/terminal-close').glob('*.json')), seconds=15)
            assert alive()
        assert (worktree / '.git').exists()
        if case == 'dirty':
            assert (worktree / 'unfinished.txt').read_text() == 'keep'
    finally:
        if master is not None:
            os.close(master)
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        if second:
            os.close(second[0])
            second[1].wait(timeout=5)
        if background:
            background.terminate()
            background.wait(timeout=5)


@pytest.mark.parametrize('change', ['none', 'reattach', 'replacement', 'new-draft', 'new-revision'])
def test_eligibility_rechecks_after_pr_lookup(close, monkeypatch, change):
    current = dict(workspace='/workspace', cursor='0')
    changed = False
    monkeypatch.setattr(close, 'owner', lambda *args: None if changed and change in ('reattach', 'replacement') else current)
    monkeypatch.setattr(close, 'revision', lambda *args: ('new' if changed and change == 'new-revision' else 'old'))
    def screen(*args):
        return subprocess.CompletedProcess(args, 0, '› draft' if changed and change == 'new-draft' else '› \n', '')
    monkeypatch.setattr(close, 'tmux', screen)
    monkeypatch.setattr(close, 'transcript_state', lambda *args: ('idle', set()))
    def prs(*args):
        nonlocal changed
        changed = True
        return True
    monkeypatch.setattr(close, 'prs_complete', prs)
    result = close.eligibility('/socket', '$1', '%1', '123', 'codex', '/rollout', '0', '1', '1')
    assert result['decision'] == ('terminate' if change == 'none' else 'preserve')


@pytest.mark.parametrize('row', [
    '$2\tdev-repo-1\t%1\t123\t/work\t0\t0\t0',
    '$1\tdev-repo-1\t%2\t123\t/work\t0\t0\t0',
    '$1\tdev-repo-1\t%1\t124\t/work\t0\t0\t0',
    '$1\tdev-repo-1\t%1\t123\t/work\t1\t0\t0',
    '$1\tdev-repo-1\t%1\t123\t/work\t0\t1\t0',
    '$1\tunmanaged\t%1\t123\t/work\t0\t0\t0',
    'bad',
])
def test_owner_never_follows_changed_identity(close, monkeypatch, row):
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 0, row, ''))
    assert close.owner('/socket', '$1', '%1', '123') is None


@pytest.mark.parametrize('failure', ['none', 'unmanaged', 'detach-hook', 'attach-hook'])
def test_installer_pins_owner_and_preserves_other_hooks(close, monkeypatch, failure):
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if 'display-message' in args:
            return subprocess.CompletedProcess(args, 0, ('other' if failure == 'unmanaged' else 'dev-repo-1') + '\t$1\t/socket\t%1\t123\n', '')
        code = int((failure == 'detach-hook' and close.HOOK in args) or
                   (failure == 'attach-hook' and 'client-attached[50100]' in args))
        return subprocess.CompletedProcess(args, code, '', '')
    monkeypatch.setattr(close, 'run', command)
    assert close.install('dev-repo-1') == (0 if failure == 'none' else 1)
    if failure == 'none':
        assert len(calls) == 4
        assert all('-g' not in args and '-u' not in args for args in calls)
        assert "'$1' %1 123" in shlex.split(calls[1][-1])[-1]


@pytest.mark.parametrize('closed', [False, True])
def test_attachment_passes_original_fd_and_cleans_up(close, monkeypatch, tmp_path, closed):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    master, slave = pty.openpty()
    tty = os.ttyname(slave)
    calls = []
    monkeypatch.setattr(close.subprocess, 'Popen', lambda args, **kwargs: calls.append((args, kwargs)))
    try:
        if closed:
            os.close(master)
            master = None
        assert close.attached('/socket', tty, '123') == int(closed)
        assert bool(calls) != closed
        if calls:
            args, options = calls[0]
            assert options['start_new_session'] and len(options['pass_fds']) == 2
            for descriptor in options['pass_fds']:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
        assert close.attached('/socket', '/not-a-terminal', '123') == 1
        lock = os.open(str(close.event_path('/socket', tty)) + '.lock', os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            assert close.attached('/socket', tty, '123') == 0
        finally:
            os.close(lock)
    finally:
        if master is not None:
            os.close(master)
        os.close(slave)


@pytest.mark.parametrize('lost', [False, True])
def test_watcher_uses_retained_fd_and_exact_event(close, monkeypatch, tmp_path, lost):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    master, slave = pty.openpty()
    tty = os.ttyname(slave)
    lock = os.open(tmp_path / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
    close.notify_detached('/socket', '$1', '%2', '123', tty)
    calls = []
    monkeypatch.setattr(close, 'detached', lambda *args: calls.append((args, close.hung_up(args[-1]))) or 0)
    if lost:
        os.close(master)
    try:
        assert close.watch('/socket', tty, '123', str(slave), str(lock)) == 0
        assert calls == [(('/socket', '$1', '%2', '123', tty, slave), lost)]
        assert not Path(str(close.event_path('/socket', tty)) + '.event').exists()
    finally:
        if not lost:
            os.close(master)


@pytest.mark.parametrize('lost', [False, True])
def test_watcher_missing_callback_never_cleans_up(close, monkeypatch, tmp_path, lost):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    master, slave = pty.openpty()
    lock = os.open(tmp_path / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
    tty = os.ttyname(slave)
    if lost:
        os.close(master)
        monkeypatch.setattr(close.os, 'kill', lambda *args: None)
    else:
        monkeypatch.setattr(close.os, 'kill', lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    ticks = iter([1, 2, 8])
    monkeypatch.setattr(close.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(close.time, 'sleep', lambda seconds: None)
    try:
        assert close.watch('/socket', tty, '123', str(slave), str(lock)) == 0
    finally:
        if not lost:
            os.close(master)


@pytest.mark.parametrize('case', ['invalid', 'detach', 'attached', 'close', 'refused'])
def test_detached_dispatch_is_fail_closed(close, monkeypatch, tmp_path, case):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setenv('COV_CORE_FAKE', 'remove')
    monkeypatch.setattr(close, 'hung_up', lambda fd: case != 'detach')
    monkeypatch.setattr(close, 'owner', lambda *args: None if case == 'attached' else {'workspace': '/w'})
    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, int(case == 'refused'), '', '')
    monkeypatch.setattr(close, 'run', command)
    assert close.detached('/socket', 'bad' if case == 'invalid' else '$1', '%1', '123', '/dev/tty1', 99) == int(case in ('invalid', 'refused'))
    assert bool(calls) == (case in ('close', 'refused'))
    if calls:
        assert 'COV_CORE_FAKE' not in calls[0][1]['env']
        assert calls[0][0][0:3] == ['zsh', '-f', '-c']


@pytest.mark.parametrize('case,decision', [
    ('owner', 'preserve'), ('git', 'preserve'), ('process', 'preserve'),
    ('revision', 'unknown'), ('draft', 'preserve'), ('screen', 'unknown'),
    ('busy', 'preserve'), ('unknown', 'unknown'), ('open', 'preserve'),
    ('pr-unknown', 'unknown'), ('fresh', 'terminate')])
def test_eligibility_vetoes(close, monkeypatch, case, decision):
    monkeypatch.setattr(close, 'owner', lambda *args: None if case == 'owner' else dict(workspace='/w', cursor='1'))
    monkeypatch.setattr(close, 'revision', lambda *args: None if case == 'revision' else 'head')
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, int(case == 'screen'),
                       'OpenAI Codex\n› ' + ('draft' if case == 'draft' else ''), ''))
    monkeypatch.setattr(close, 'transcript_state', lambda *args: (case if case in ('busy', 'unknown') else 'unknown' if case == 'fresh' else 'idle', set()))
    monkeypatch.setattr(close, 'prs_complete', lambda *args: None if case == 'pr-unknown' else case != 'open')
    result = close.eligibility('/socket', '$1', '%1', '123', 'codex', '', '1' if case == 'fresh' else '0',
                               '0' if case == 'git' else '1', '0' if case == 'process' else '1')
    assert result['decision'] == decision


@pytest.mark.parametrize('state,expected', [('OPEN', False), ('MERGED', True), ('CLOSED', True), ('UNKNOWN', None)])
def test_associated_prs_are_checked_even_on_clean_branch(close, monkeypatch, state, expected):
    def command(args, **kwargs):
        value = ('https://github.com/o/r.git' if 'get-url' in args else 'topic' if 'symbolic-ref' in args
                 else json.dumps({'state': state}) if 'view' in args else '[]')
        return subprocess.CompletedProcess(args, 0, value, '')
    monkeypatch.setattr(close, 'run', command)
    assert close.prs_complete('/w', {'https://github.com/o/r/pull/1'}) is expected


@pytest.mark.parametrize('record,agent,expected', [
    ([], 'codex', 'unknown'), ({'type': 'event_msg', 'payload': [1]}, 'codex', 'unknown'),
    ({'type': 'response_item', 'payload': [1]}, 'codex', 'unknown'),
    ({'type': 'response_item', 'payload': {'role': 'user'}}, 'codex', 'busy'),
    ({'type': 'assistant', 'message': [1]}, 'claude', 'unknown')])
def test_unknown_or_legacy_transcripts_never_imply_completion(close, tmp_path, record, agent, expected):
    path = tmp_path / 'tx'
    path.write_text(json.dumps(record) + '\n')
    assert close.transcript_state(agent, path)[0] == expected


@pytest.mark.parametrize('case', ['good', 'dirty', 'missing-transcript'])
def test_revision_needs_clean_git_and_readable_transcript(close, monkeypatch, tmp_path, case):
    path = tmp_path / 'transcript'
    if case != 'missing-transcript':
        path.write_text('hello')
    monkeypatch.setattr(close, 'run', lambda args: subprocess.CompletedProcess(args, 0,
                        'head' if 'rev-parse' in args else '?? dirty' if case == 'dirty' else '', ''))
    value = close.revision('/w', str(path))
    assert (value is not None) == (case == 'good')


def test_failed_observations_are_not_empty_evidence(close, monkeypatch, tmp_path):
    assert close.run([str(tmp_path / 'missing')]).returncode != 0
    monkeypatch.setattr(close.subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired('command', 15)))
    assert close.run(['command']).returncode != 0
    monkeypatch.setattr(close.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '', ''))
    assert close.run(['command']).returncode == 0
    assert close.transcript_state('codex', tmp_path / 'missing')[0] == 'unknown'
    assert not close.startup_screen('codex', 'unfinished work\n› \n', '1')
    assert close.composer_empty('codex', '› \n\x1b[2m50% context left\x1b[0m\n', '0') is True
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 1, '', 'failed'))
    assert close.owner('/s', '$1', '%1', '1') is None
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 0, '$1\tdev-repo-1\t%1\t1\t/w\t0\t0\t0', ''))
    assert close.owner('/s', '$1', '%1', '1')['workspace'] == '/w'


@pytest.mark.parametrize('case', ['many', 'origin', 'host', 'branch', 'schema', 'timeout'])
def test_pr_uncertainty_preserves_work(close, monkeypatch, case):
    def command(args, **kwargs):
        code = int(case == 'origin' and 'get-url' in args)
        text = ('https://other.invalid/o/r' if case == 'host' else 'https://github.com/o/r') if 'get-url' in args else (
            '' if case == 'branch' else 'topic') if 'symbolic-ref' in args else ('{}' if case == 'schema' else '[]')
        return subprocess.CompletedProcess(args, code, text, '')
    monkeypatch.setattr(close, 'run', command)
    ticks = iter([0, 20])
    if case == 'timeout':
        monkeypatch.setattr(close.time, 'monotonic', lambda: next(ticks))
    assert close.prs_complete('/w', set(map(str, range(9))) if case == 'many' else {'https://github.com/o/r/pull/1'}) is None


def test_private_state_refuses_symlink_directory(close, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    (tmp_path / 't').mkdir()
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 't/terminal-close').symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe'):
        close.event_path('/socket', '/dev/tty1')


def test_private_dispatch(close, monkeypatch):
    for action, target, values in [('install', 'install', ['session']), ('detached', 'notify_detached', ['s', 'i', 'p', 'pid', 'tty']),
                                  ('attached', 'attached', ['s', 'tty', 'pid']), ('watch', 'watch', ['s', 'tty', 'pid', '1', '2'])]:
        calls = []
        monkeypatch.setattr(close, target, lambda *args: calls.append(args) or 0)
        assert close.main([action, *values]) == 0
        assert calls == [tuple(values)]
    monkeypatch.setattr(close, 'eligibility', lambda *args: {'decision': 'preserve'})
    assert close.main(['check', *([''] * 10)]) == 1
    assert close.main([]) == 2


@pytest.mark.parametrize('mode', ['missing', 'valid', 'corrupt', 'invalid-pr'])
def test_retained_prs_survive_compaction(close, monkeypatch, tmp_path, mode):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    path = tmp_path / 't/history/v1/history.sqlite3'
    if mode != 'missing':
        path.parent.mkdir(parents=True)
        if mode == 'corrupt':
            path.write_text('broken')
        else:
            with sqlite3.connect(path) as db:
                db.execute('create table conversations (record text)')
                db.execute('insert into conversations values (?)', (json.dumps(dict(agent='codex', sid=SID,
                           prs=[dict(repo='owner/repo', number=1 if mode == 'valid' else None)])),))
                db.execute('insert into conversations values (?)', (json.dumps(dict(agent='claude', sid=SID,
                           prs=[dict(repo='unrelated/repo', number=2)])),))
            db.close()
    expected = {'https://github.com/owner/repo/pull/1'} if mode == 'valid' else set() if mode == 'missing' else None
    assert close.history_prs('codex', SID) == expected
    assert path.exists() == (mode != 'missing')


def test_unreadable_retained_prs_veto_termination(close, monkeypatch):
    monkeypatch.setattr(close, 'owner', lambda *args: dict(workspace='/w', cursor='0'))
    monkeypatch.setattr(close, 'revision', lambda *args: 'head')
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 0, '› \n', ''))
    monkeypatch.setattr(close, 'transcript_state', lambda *args: ('idle', set()))
    monkeypatch.setattr(close, 'history_prs', lambda *args: None)
    assert close.eligibility('/s', '$1', '%1', '123', 'codex', '/tx', '0', '1', '1', SID)['decision'] == 'unknown'


def test_inaccessible_history_is_not_missing_history(close, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    original = Path.stat
    def stat(path, *args, **kwargs):
        if path.name == 'history.sqlite3':
            raise PermissionError('inaccessible')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    assert close.history_prs('codex', SID) is None
