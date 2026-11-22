"""Terminal-loss cleanup without native desktop windows or real agent state."""
import json
import fcntl
import os
from pathlib import Path
import pty
import shutil
import shlex
import sqlite3
import signal
import sys
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


@pytest.mark.parametrize('case', ['empty', 'completed', 'draft', 'dirty', 'open-pr', 'busy', 'detach', 'background', 'another-client',
    'completed-preview', 'busy-preview', 'draft-preview', 'dirty-preview', 'unmerged-preview', 'open-pr-preview', 'mixed-preview'])
def test_public_terminal_closure(lifecycle, close, case):
    base, env = lifecycle
    preview = case.endswith('-preview')
    case = case.removesuffix('-preview')
    preview_dir = base / 'preview-outside-worktree'
    preview_dir.mkdir()
    if preview:
        env = {**env, 'T_TEST_STATIC_PREVIEW': str(preview_dir)}
        if case == 'mixed':
            env['T_TEST_EXTRA_WORK'] = '1'
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
    if case == 'unmerged':
        (worktree / 'new.txt').write_text('unpublished')
        assert _run(['git', '-C', str(worktree), 'add', 'new.txt'], env=env, cwd=base).returncode == 0
        assert _run(['git', '-C', str(worktree), '-c', 'user.name=Test', '-c',
                     'user.email=test@example.invalid', 'commit', '-qm', 'pending'], env=env, cwd=base).returncode == 0
    preview_pid = None
    if preview:
        _until(lambda: (preview_dir / 'preview.pid').exists())
        preview_pid = int((preview_dir / 'preview.pid').read_text())
        _until(lambda: close.preview_listener(preview_pid, {}))
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
            if preview_pid:
                _until(lambda: preview_pid not in close.process_table() or
                       close.process_table()[preview_pid]['state'].startswith('Z'))
        else:
            _until(lambda: list((Path(env['XDG_STATE_HOME']) / 't/terminal-close').glob('*.json')), seconds=15)
            assert alive()
            if preview_pid:
                assert close.preview_listener(preview_pid, {})  # no TERM on any veto
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
        if preview_pid:
            try:
                os.kill(preview_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if background:
            background.terminate()
            background.wait(timeout=5)


def test_failed_observer_never_covers_the_agent(lifecycle, close, monkeypatch):
    base, env = lifecycle
    env = {**env, 'T_TEST_AGENT_SCREEN': '\x1b[2J\x1b[HAgent remains visible\r\n'}
    assert _shell(base, env, 't session open repo 1 --codex').returncode == 0
    marker = base / 'observer-ran'
    broken = base / 'broken-observer'
    broken.write_text('#!/bin/sh\ntouch ' + shlex.quote(str(marker)) + '\necho unavailable\nexit 7\n')
    broken.chmod(0o755)
    monkeypatch.setattr(close.sys, 'executable', str(broken))
    monkeypatch.setattr(close, 'run', lambda args, **kwargs: _run(args, env=env, cwd=base))
    binding = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:',
                    '#{session_id}|#{pane_id}|#{pane_pid}'], env=env, cwd=base).stdout.strip().split('|')
    commands = close.hook_commands('/unused', dict(identity=binding[0], pane=binding[1], pane_pid=binding[2]))
    for hook, command in commands.items():
        assert _run([str(base / 'bin/tmux'), 'set-hook', '-t', '=dev-repo-1:', hook, command], env=env, cwd=base).returncode == 0
    socket = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', 'dev-repo-1', '#{socket_path}'], env=env, cwd=base).stdout.strip()
    master, slave = pty.openpty()
    process = subprocess.Popen([shutil.which('tmux'), '-S', socket, 'attach-session', '-t', 'dev-repo-1'],
                               stdin=slave, stdout=slave, stderr=slave,
                               env={**env, 'TERM': 'xterm-256color'}, start_new_session=True)
    os.close(slave)
    try:
        _until(marker.exists)
        # A foreground run-shell failure enters copy mode after displaying its
        # result. Wait for the result rather than checking before the hook ran.
        time.sleep(.2)
        mode = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', 'dev-repo-1', '#{pane_in_mode}'], env=env, cwd=base)
        assert mode.stdout.strip() == '0'
        screen = _run([str(base / 'bin/tmux'), 'capture-pane', '-p', '-t', 'dev-repo-1'], env=env, cwd=base)
        assert 'Agent remains visible' in screen.stdout
        assert 'unavailable' not in screen.stdout
    finally:
        os.close(master)
        process.wait(timeout=5)


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
    helpers = close.runtime()
    helpers['socket_identity'] = lambda socket: (1, 2)
    monkeypatch.setattr(close, 'runtime', lambda: helpers)
    calls = []
    hooks = {}
    def command(args, **kwargs):
        calls.append(args)
        if 'display-message' in args:
            return subprocess.CompletedProcess(args, 0, ('other' if failure == 'unmanaged' else 'dev-repo-1') + '\t$1\t/socket\t%1\t123\n', '')
        if 'show-hooks' in args:
            return subprocess.CompletedProcess(args, 0, '\n'.join(key + ' ' + value for key, value in hooks.items()), '')
        if 'set-hook' in args:
            hooks[args[-2]] = args[-1]
        code = int((failure == 'detach-hook' and close.HOOK in args) or
                   (failure == 'attach-hook' and 'client-attached[50100]' in args))
        return subprocess.CompletedProcess(args, code, '', '')
    monkeypatch.setattr(close, 'run', command)
    assert close.install('dev-repo-1') == (0 if failure == 'none' else 1)
    if failure == 'none':
        writes = [args for args in calls if 'set-hook' in args]
        assert len(writes) == 3
        assert all('-g' not in args and '-u' not in args for args in calls)
        assert "'$1' %1 123" in shlex.split(writes[0][-1])[-1]
        assert all('>/dev/null 2>&1 || :' in shlex.split(args[-1])[-1] for args in writes)
        assert '#{q:hook_client}' in writes[1][-1] and '#{q:client_pid}' not in writes[1][-1]


@pytest.mark.parametrize('closed', [False, True])
def test_attachment_passes_original_fd_and_cleans_up(close, monkeypatch, tmp_path, closed):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    helpers = close.runtime()
    helpers.update(canonical_socket=lambda value: value, socket_identity=lambda value: (1, 2),
                   adopt_attachment=lambda *args: True)
    monkeypatch.setattr(close, 'runtime', lambda: helpers)
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


@pytest.mark.parametrize('listing', ['correct', 'missing', 'ambiguous', 'invalid'])
def test_attachment_resolves_saved_hook_client(close, monkeypatch, tmp_path, listing):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    helpers = close.runtime()
    helpers.update(canonical_socket=lambda value: value, socket_identity=lambda value: (1, 2),
                   adopt_attachment=lambda *args: True)
    monkeypatch.setattr(close, 'runtime', lambda: helpers)
    master, slave = pty.openpty()
    tty = os.ttyname(slave)
    text = tty + '|' + ('bad' if listing == 'invalid' else '123') + '\n'
    if listing == 'missing':
        text = '/dev/other|456\n'
    if listing == 'ambiguous':
        text *= 2
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 0, text, ''))
    monkeypatch.setattr(close.subprocess, 'Popen', lambda *args, **kwargs: None)
    try:
        assert close.attached('/socket', tty) == (0 if listing == 'correct' else 1)
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize('lost', [False, True])
def test_watcher_uses_retained_fd_and_exact_event(close, monkeypatch, tmp_path, lost):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    helpers = close.runtime()
    helpers.update(canonical_socket=lambda value: value, socket_identity=lambda value: (1, 2),
                   adopt_attachment=lambda *args: True)
    monkeypatch.setattr(close, 'runtime', lambda: helpers)
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
    helpers = close.runtime()
    helpers.update(canonical_socket=lambda value: value, socket_identity=lambda value: (1, 2),
                   adopt_attachment=lambda *args: True)
    monkeypatch.setattr(close, 'runtime', lambda: helpers)
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


@pytest.mark.parametrize('arguments,accepted', [
    (['0', '--bind', 'localhost'], True),
    (['--bind', '127.0.0.1', '-d', '.', '-p', 'HTTP/1.1', '8000'], True),
    (['-b', '::1'], True), ([], False), (['--bind', '0.0.0.0'], False),
    (['--bind', '::'], False), (['--bind', 'example.com'], False),
    (['--bind', 'localhost', '--cgi'], False), (['--bi', 'localhost'], False),
    (['--bind=localhost'], False), (['--bind', 'localhost', '99999'], False),
    (['--bind', 'localhost', '80', '81'], False),
    (['--bind', 'localhost', '-d', 'missing'], False),
    (['--bind', 'localhost', '--bind', 'localhost'], False),
    (['--bind'], False), (['--bind', 'localhost', '--protocol', 'unknown'], False),
])
def test_static_preview_allowlist(close, tmp_path, arguments, accepted):
    detail = dict(argv=['python3', '-m', 'http.server', *arguments],
                  executable='/usr/bin/python3', cwd=str(tmp_path), birth=1, import_safe=True)
    assert close.static_preview(detail) is accepted
    if accepted:
        (tmp_path / 'http.py').touch()
        assert not close.static_preview(detail)  # a local module is not stdlib http.server


def test_native_preview_identity_and_listener(close, tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COV_CORE_', 'PYTHON'))}
    process = subprocess.Popen([sys.executable, '-m', 'http.server', '0', '--bind', '127.0.0.1'],
                               cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _until(lambda: close.preview_listener(process.pid, {}))
        detail = close.process_detail(process.pid)
        assert detail['argv'][1:] == ['-m', 'http.server', '0', '--bind', '127.0.0.1']
        assert os.path.realpath(detail['cwd']) == str(tmp_path.resolve())
        assert close.static_preview(detail)
        assert close.process_detail(process.pid) == detail
        assert process.poll() is None  # inspection is read-only
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize('case', ['preview', 'root-outside', 'transfer', 'descendant', 'detached',
                                  'wrong-user', 'wrapper', 'native-race', 'table-race', 'new-child', 'new-grandchild'])
def test_process_plan_is_read_only_and_all_or_nothing(close, monkeypatch, tmp_path, case):
    table = {2: dict(parent=1, uid=os.geteuid(), state='S', start='a'),
             10: dict(parent=2, uid=os.geteuid(), state='S', start='b'),
             20: dict(parent=2, uid=os.geteuid(), state='S', start='c'),
             30: dict(parent=20, uid=os.geteuid(), state='S', start='d'),
             11: dict(parent=10, uid=os.geteuid(), state='S', start='e')}
    detail = dict(argv=['python3', '-m', 'http.server', '0', '-b', 'localhost'],
                  executable='/usr/bin/python3', cwd=str(tmp_path), birth='native', import_safe=True)
    if case in ('descendant', 'wrapper'):
        table[12] = dict(parent=11, uid=os.geteuid(), state='S', start='f')
    if case == 'detached':
        table[11]['parent'] = 2
    if case == 'wrong-user':
        table[11]['uid'] += 1
    if case == 'transfer':
        detail['argv'] = ['python3', '/tmp/backup.py']
    count = 0
    def native(pid):
        nonlocal count
        count += 1
        return {**detail, 'birth': str(count) if case == 'native-race' else 'native'}
    observations = 0
    def snapshot():
        nonlocal observations
        observations += 1
        if observations > 1:
            if case == 'table-race':
                return {**table, 11: {**table[11], 'start': 'changed'}}
            if case in ('new-child', 'new-grandchild'):
                return {**table, 12: dict(parent=10 if case == 'new-child' else 11,
                                          uid=os.geteuid(), state='S', start='new')}
        return table
    monkeypatch.setattr(close, 'process_table', snapshot)
    monkeypatch.setattr(close, 'process_detail', native)
    monkeypatch.setattr(close, 'rooted_processes', lambda workspace: set() if case == 'root-outside' else {10, 11})
    monkeypatch.setattr(close, 'preview_listener', lambda *args: True)
    monkeypatch.setattr(close.os, 'getpid', lambda: 30)
    signals = []
    monkeypatch.setattr(close.os, 'kill', lambda *args: signals.append(args))
    if case in ('preview', 'root-outside'):
        assert set(close.process_plan('10', '/workspace', '20')['previews']) == {11}
    else:
        with pytest.raises(ValueError):
            close.process_plan('10', '/workspace', '20')
    assert signals == []


@pytest.mark.parametrize('case', ['success', 'busy', 'draft', 'git', 'mixed', 'owner', 'revision',
                                  'pid-race', 'term-denied', 'term-survives', 'partial', 'conversation'])
def test_preview_cleanup_revalidates_and_never_escalates(close, monkeypatch, case, capsys):
    from copy import deepcopy
    detail = dict(argv=['python3', '-m', 'http.server', '0', '-b', 'localhost'],
                  executable='/usr/bin/python3', cwd='/tmp', birth='native', import_safe=True)
    original = dict(agent={'birth': 'owner'}, previews={11: dict(detail)})
    if case == 'partial':
        original['previews'][12] = dict(detail)
    current = dict(workspace='/workspace', cursor='0')
    signals, observations = [], []
    def plan(*args):
        if case == 'mixed':
            raise ValueError('unknown background transfer')
        live = deepcopy(original)
        for pid, _ in signals:
            live['previews'].pop(pid, None)
        return live
    def observed_owner(*args):
        observations.append(True)
        return None if case == 'owner' and len(observations) > 1 else current
    def terminate(pid, sig):
        if case == 'term-denied':
            raise PermissionError('TERM denied')
        signals.append((pid, sig))
    monkeypatch.setattr(close, 'owner', observed_owner)
    revisions = []
    def revision(*args):
        revisions.append(True)
        return 'changed' if ((case == 'revision' and len(revisions) > 1) or
                             (case == 'partial' and signals)) else 'original'
    monkeypatch.setattr(close, 'revision', revision)
    marks = []
    def conversation_mark(*args):
        marks.append(True)
        return 'new' if case == 'conversation' and len(marks) > 1 else 'old'
    monkeypatch.setattr(close, 'conversation_mark', conversation_mark)
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 0, '› \n', ''))
    monkeypatch.setattr(close, 'git_completed', lambda *args: case != 'git')
    monkeypatch.setattr(close, 'process_plan', plan)
    monkeypatch.setattr(close, 'eligibility', lambda *args: dict(
        decision='preserve' if case in ('busy', 'draft') else 'terminate', reason=case))
    monkeypatch.setattr(close, 'process_detail', lambda pid: {**detail, 'birth': 'reused'} if case == 'pid-race' else detail)
    monkeypatch.setattr(close, 'preview_running', lambda *args: case == 'term-survives')
    monkeypatch.setattr(close.os, 'kill', terminate)
    ticks = iter(range(100))
    monkeypatch.setattr(close.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(close.time, 'sleep', lambda *args: None)
    check = ['/socket', '$1', '%1', '10', 'codex', '/rollout', '0', '1', '1', 'sid']
    assert close.stop_previews(check, '10', '20') == int(case != 'success')
    assert signals == ([(11, signal.SIGTERM)] if case in ('success', 'term-survives', 'partial') else [])
    receipt = json.loads(capsys.readouterr().err)
    assert receipt['preview_term_sent'] == [pid for pid, _ in signals]
    assert receipt['reason']


@pytest.mark.parametrize('target', ['main', 'master', 'custom'])
def test_cleanup_git_proof_uses_same_default_branch_as_shell(close, tmp_path, target):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'HOME': str(tmp_path), 'XDG_CONFIG_HOME': str(tmp_path / 'config')}
    def git(*args):
        result = subprocess.run(['git', '-C', str(tmp_path), *args], env=env, text=True,
                                capture_output=True, check=True)
        return result.stdout.strip()
    git('init', '-q')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'initial')
    head = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/' + target, head)
    if target == 'custom':
        git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/custom')
    assert close.git_completed(str(tmp_path))
    (tmp_path / 'dirty').touch()
    assert not close.git_completed(str(tmp_path))


@pytest.mark.parametrize('key', ['PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONPLATLIBDIR',
                                 'PYTHONSTARTUP', 'PYTHONINSPECT'])
def test_import_environment_verdict_does_not_retain_values(close, key):
    secret = b'/private/secret-import-path'
    assert not close.import_environment_safe([key.encode() + b'=' + secret])
    assert close.import_environment_safe([key.encode() + b'=', b'PRIVATE_TOKEN=secret'])


def test_native_custom_pythonpath_server_is_preserved(close, tmp_path):
    custom = tmp_path / 'custom'
    package = custom / 'http'
    package.mkdir(parents=True)
    (package / '__init__.py').touch()
    (package / 'server.py').write_text(
        'import socket, time\n'
        's = socket.socket()\ns.bind(("127.0.0.1", 0))\ns.listen()\n'
        'while True: time.sleep(1)\n')
    serving = tmp_path / 'serving'
    serving.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COV_CORE_', 'PYTHON'))}
    env['PYTHONPATH'] = str(custom)
    process = subprocess.Popen([sys.executable, '-m', 'http.server', '0', '--bind', '127.0.0.1'],
                               cwd=serving, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _until(lambda: close.preview_listener(process.pid, {}))
        detail = close.process_detail(process.pid)
        assert not detail['import_safe']
        assert not close.static_preview(detail)
        assert str(custom) not in json.dumps(detail)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_linux_native_identity_socket_and_exit_probes(close, monkeypatch, tmp_path):
    root = tmp_path / 'proc' / '123'
    (root / 'fd').mkdir(parents=True)
    (root / 'net').mkdir()
    (root / 'exe').symlink_to('/usr/bin/python3')
    (root / 'cwd').symlink_to(tmp_path)
    (root / 'cmdline').write_bytes(b'\0'.join(
        [b'python3', b'-m', b'http.server', b'0', b'--bind', b'127.0.0.1']) + b'\0')
    (root / 'environ').write_bytes(b'PYTHONPATH=\0PRIVATE_TOKEN=secret\0')
    fields = ['S', '10', *(['0'] * 17), '9876']
    (root / 'stat').write_text('123 (python3) ' + ' '.join(fields))
    (root / 'fd' / '3').symlink_to('socket:[456]')
    (root / 'net/tcp').write_text('header\n0: 0100007F:1F40 00000000:0000 0A 0:0 0:0 0 1000 0 456\n')
    (root / 'net/tcp6').write_text('header\n')
    monkeypatch.setattr(close.sys, 'platform', 'linux')
    monkeypatch.setattr(close, 'Path', lambda value: tmp_path / 'proc' if value == '/proc' else Path(value))
    detail = close.process_detail(123)
    assert detail['birth'] == '9876' and detail['import_safe']
    assert detail['argv'] == ['python3', '-m', 'http.server', '0', '--bind', '127.0.0.1']
    assert close.preview_listener(123, detail)
    assert close.preview_running(123, '9876')
    fields[0] = 'Z'
    (root / 'stat').write_text('123 (python3) ' + ' '.join(fields))
    assert not close.preview_running(123, '9876')
    with pytest.raises(ValueError, match='reused'):
        close.preview_running(123, 'different')
    (root / 'net/tcp').write_text('header\n0: 00000000:1F40 00000000:0000 0A 0:0 0:0 0 1000 0 456\n')
    assert not close.preview_listener(123, detail)
    (root / 'stat').unlink()
    assert not close.preview_running(123, '9876')


def test_conversation_mark_ignores_its_own_registry_read_atime(close, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    registry = tmp_path / 'claude-sessions/123'
    registry.parent.mkdir()
    registry.write_text('sid\t/workspace\n')
    info = registry.stat()
    os.utime(registry, ns=(1, info.st_mtime_ns))
    monkeypatch.setattr(close, 'tmux', lambda *args: subprocess.CompletedProcess(args, 0, 'stable', ''))
    first = close.conversation_mark('/socket', '$1', '%1', 123)
    assert close.conversation_mark('/socket', '$1', '%1', 123) == first
    registry.write_text('changed\t/workspace\n')
    assert close.conversation_mark('/socket', '$1', '%1', 123) != first
