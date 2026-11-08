"""Public moves between two isolated hosts using real tmux, Git and rsync.

The hosts share a disposable repository/worktree filesystem (as with a network
mount), but have separate agent homes, registries and tmux servers. SSH and the
agents are simulated; saved-resume cases also simulate GitHub rename proofs and
picker input. No production lifecycle functions are replaced.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import uuid

import pytest

from test_session_lifecycle import lifecycle, _shell, _shell_tty, _run, _until, ROOT, SID


@pytest.fixture
def two_hosts(lifecycle):
    base, first = lifecycle
    if not shutil.which('rsync'):
        pytest.skip('rsync is required')
    first = {**first, 'CODEX_HOME': str(base / 'home/.codex'),
             'GIT_AUTHOR_NAME': 'Test', 'GIT_COMMITTER_NAME': 'Test',
             'GIT_AUTHOR_EMAIL': 'test@example.invalid', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
    (base / 'home/.codex').symlink_to(base / 'codex', target_is_directory=True)
    second = dict(first)
    other = base / 'second'
    for name in ('home', 'bin', 'config', 'cache', 'state', 'data'):
        (other / name).mkdir(parents=True)
    second.update(HOME=str(other / 'home'), ZDOTDIR=str(other / 'home'),
                  CODEX_HOME=str(other / 'home/.codex'), XDG_CONFIG_HOME=str(other / 'config'),
                  XDG_CACHE_HOME=str(other / 'cache'), XDG_STATE_HOME=str(other / 'state'),
                  XDG_DATA_HOME=str(other / 'data'), T_TEST_AGENT_LOG=str(other / 'agents.log'),
                  PATH=str(other / 'bin') + os.pathsep + first['PATH'])
    (other / 'home/.codex/thread-writer-locks').mkdir(parents=True)
    (other / 'home/.zshenv').write_text('skip_global_compinit=1\n')
    (other / 'home/.zshrc').write_text('export PATH=' + shlex.quote(second['PATH']) + '\nsource ' + shlex.quote(str(ROOT / 't.plugin.zsh')) + '\n')
    (other / 'config/t').mkdir()
    config = (base / 'config/t/local.zsh').read_text()
    (base / 'config/t/local.zsh').write_text(config + 'REMOTE_HOSTS[second]=second.test\n')
    (other / 'config/t/local.zsh').write_text(config + 'REMOTE_HOSTS[first]=first.test\n')
    socket = '/tmp/t-move-' + uuid.uuid4().hex[:12] + '.sock'
    tmux = other / 'bin/tmux'
    tmux.write_text('#!/bin/sh\nexec ' + shlex.quote(shutil.which('tmux')) + ' -S ' + shlex.quote(socket) + ' -f /dev/null "$@"\n')
    tmux.chmod(0o755)
    # Store only sandbox overrides, never the developer's inherited environment.
    keys = ('HOME', 'ZDOTDIR', 'CODEX_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
            'XDG_STATE_HOME', 'XDG_DATA_HOME', 'T_TEST_AGENT_LOG', 'PATH')
    hosts = base / 'hosts.json'
    hosts.write_text(json.dumps({host: {key: env[key] for key in keys}
                                for host, env in [('first.test', first), ('second.test', second)]}))
    first['T_TEST_HOSTS'] = second['T_TEST_HOSTS'] = str(hosts)
    ssh = base / 'bin/ssh'
    ssh.write_text('''#!/usr/bin/env python3
import base64, json, os, re, shlex, subprocess, sys
args = sys.argv[1:]
while args and args[0].startswith('-'):
    flag = args.pop(0)
    if flag in ('-o', '-p', '-l', '-i', '-F'):
        args.pop(0)
host = args.pop(0)
overrides = json.load(open(os.environ['T_TEST_HOSTS']))[host]
env = dict(os.environ, **overrides)
os.chdir(env['HOME'])
command = args[0] if len(args) == 1 else shlex.join(args)
fault = os.environ.get('T_TEST_MOVE_FAULT')
if fault:
    setting = json.load(open(fault))
    frames = re.findall(r'[A-Za-z0-9+/=]{80,}', command)
    for frame in frames:
        try:
            request = json.loads(base64.b64decode(frame))
        except (ValueError, UnicodeError):
            continue
        if request.get('stage') == setting['stage'] and not setting.get('fired'):
            setting['fired'] = True
            with open(fault, 'w') as stream:
                json.dump(setting, stream)
            if setting.get('lost_reply'):
                result = subprocess.run(['/bin/sh', '-c', command], env=env, capture_output=True)
                if result.returncode:
                    sys.stderr.buffer.write(result.stderr)
                    sys.exit(result.returncode)
            print('injected SSH disconnect', file=sys.stderr)
            sys.exit(42)
os.execve('/bin/sh', ['sh', '-c', command], env)
''')
    ssh.chmod(0o755)
    try:
        yield base, first, second
    finally:
        _run([str(tmux), 'kill-server'], env=second, cwd=base, timeout=5)
        Path(socket).unlink(missing_ok=True)


def saved_source(base, env, agent):
    workspace = base / 'worktrees/repo/repo-1'
    result = _run(['git', '-C', str(base / 'repo'), 'worktree', 'add', '-qb',
                   'dev/repo-1', str(workspace), 'origin/main'], env=env, cwd=base)
    assert result.returncode == 0, result.stderr
    if agent == 'codex':
        home = Path(env['CODEX_HOME'])
        (home / 'thread-writer-locks').mkdir()
        path = home / 'sessions/2026/11/07' / ('rollout-2026-11-07T00-00-00-' + SID + '.jsonl')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload': dict(id=SID, cwd=str(workspace), source='cli', cli_version='0.160.0')}) + '\n')
        with sqlite3.connect(home / 'state_5.sqlite') as db:
            db.execute('create table threads (id text, rollout_path text, cwd text, name text, title text, updated_at integer, archived integer, source text, first_user_message text, history_mode text)')
            db.execute('insert into threads values (?,?,?,?,?,?,?,?,?,?)',
                       (SID, str(path), str(workspace), 'Saved work', 'Saved work', 1, 0, 'cli', 'Saved work', 'legacy'))
    else:
        encoded = ''.join(c if c.isalnum() else '-' for c in str(workspace))
        path = Path(env['HOME']) / '.claude/projects' / encoded / (SID + '.jsonl')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(type='user', cwd=str(workspace), sessionId=SID,
                                       message=dict(role='user', content='Move this work')), separators=(',', ':')) + '\n')
    result = _shell(base, env, f'_dev_history_record {SID} {agent} {shlex.quote(str(workspace))} resume')
    assert result.returncode == 0, result.stderr
    result = _shell(base, env, 't session resume repo 1 --cli')
    assert result.returncode == 0, result.stderr or result.stdout
    _until(lambda: (base / 'agents.log').exists())
    return workspace


@pytest.mark.parametrize('agent', ['codex', 'claude'])
@pytest.mark.parametrize('direction', ['send', 'receive'])
def test_public_move_keeps_one_owner_and_transfers_saved_work(two_hosts, agent, direction):
    base, first, second = two_hosts
    workspace = saved_source(base, first, agent)
    (workspace / 'unfinished.txt').write_text('carry these edits\n')
    env = first if direction == 'send' else second
    command = ('t move repo 1 --host second --detach' if direction == 'send' else
               't session move repo 1 --from first --host local --detach')
    moved = _run(['zsh', '-lic', command], env=env, cwd=base, timeout=60)
    assert moved.returncode == 0, moved.stderr or moved.stdout
    source = _run([str(base / 'bin/tmux'), 'list-sessions', '-F', '#{session_name}'], env=first, cwd=base)
    assert 'dev-repo-1' not in source.stdout
    destination = _run([str(base / 'second/bin/tmux'), 'list-sessions', '-F', '#{session_name}|#{session_path}'], env=second, cwd=base)
    assert destination.returncode == 0, destination.stderr
    owners = destination.stdout.strip().splitlines()
    assert len(owners) == 1, owners
    label, path = owners[0].split('|', 1)
    assert (Path(path) / 'unfinished.txt').read_text() == 'carry these edits\n'
    launches = _until(lambda: (base / 'second/agents.log').read_text().splitlines()
                      if (base / 'second/agents.log').exists() else None)
    assert len(launches) == 1 and SID in launches[0], launches
    assert len((base / 'agents.log').read_text().splitlines()) == 1
    owner = _shell(base, second, '_dev_session_sid ' + shlex.quote(label))
    assert owner.stdout.strip() == SID
    tx = _shell(base, second, f'_dev_agent_transcript {agent} {SID} {shlex.quote(path)}')
    assert tx.returncode == 0, tx.stderr
    assert SID in Path(tx.stdout.strip()).read_text()


@pytest.mark.parametrize('stage,lost_reply,source_running', [
    ('preflight', False, True), ('publish', False, False), ('launch', True, False),
])
def test_public_move_retry_reconciles_disconnect_without_duplicate_owner(two_hosts, stage, lost_reply, source_running):
    base, first, second = two_hosts
    workspace = saved_source(base, first, 'codex')
    (workspace / 'unfinished.txt').write_text('recover these edits\n')
    fault = base / 'fault.json'
    fault.write_text(json.dumps(dict(stage=stage, lost_reply=lost_reply)))
    # Receive puts source effects on SSH; send puts launch and preflight there.
    receiver = stage == 'publish'
    env = dict(second if receiver else first, T_TEST_MOVE_FAULT=str(fault))
    command = ('t session move repo 1 --from first --host local --detach' if receiver
               else 't move repo 1 --host second --detach')
    result = _run(['zsh', '-lic', command], env=env, cwd=base, timeout=60)
    assert result.returncode != 0 and json.loads(fault.read_text())['fired'], result
    source = _run([str(base / 'bin/tmux'), 'has-session', '-t', '=dev-repo-1:'], env=first, cwd=base)
    assert (source.returncode == 0) == source_running
    retry = _run(['zsh', '-lic', 't resume repo 1'], env=env, cwd=base, timeout=60)
    assert retry.returncode == 0, retry.stderr or retry.stdout
    assert len((base / 'second/agents.log').read_text().splitlines()) == 1
    assert len((base / 'agents.log').read_text().splitlines()) == 1
    assert _run([str(base / 'bin/tmux'), 'has-session', '-t', '=dev-repo-1:'], env=first, cwd=base).returncode != 0
    owners = _run([str(base / 'second/bin/tmux'), 'list-sessions', '-F', '#{session_path}'], env=second, cwd=base)
    assert len(owners.stdout.strip().splitlines()) == 1
    assert (Path(owners.stdout.strip()) / 'unfinished.txt').read_text() == 'recover these edits\n'


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_public_restart_preserves_exact_pane_conversation_and_work(two_hosts, agent):
    base, first, second = two_hosts
    workspace = saved_source(base, first, agent)
    (workspace / 'unfinished.txt').write_text('keep edits during restart\n')
    tmux = str(base / 'bin/tmux')
    before = _run([tmux, 'display-message', '-p', '-t', '=dev-repo-1:', '#{session_id}|#{pane_id}'], env=first, cwd=base).stdout
    restarted = _run(['zsh', '-lic', 't session restart repo 1'], env=first, cwd=base, timeout=40)
    assert restarted.returncode == 0, restarted.stderr or restarted.stdout
    after = _run([tmux, 'display-message', '-p', '-t', '=dev-repo-1:', '#{session_id}|#{pane_id}'], env=first, cwd=base).stdout
    assert before == after
    launches = (base / 'agents.log').read_text().splitlines()
    assert len(launches) == 2 and all(SID in row for row in launches)
    assert (workspace / 'unfinished.txt').read_text() == 'keep edits during restart\n'
    assert not (base / 'second/agents.log').exists()


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_public_pop_releases_tmux_owner_before_foreground_resume(two_hosts, agent):
    base, first, second = two_hosts
    workspace = saved_source(base, first, agent)
    tmux = str(base / 'bin/tmux')
    old_pid = _shell(base, first, '_dev_session_claude_pid dev-repo-1').stdout.strip()
    assert old_pid.isdigit()
    result = _run(['zsh', '-lic', 't session pop repo 1'],
                  env=dict(first, T_TEST_AGENT_SECONDS='1'), cwd=base, timeout=40)
    assert result.returncode == 0, result.stderr or result.stdout
    assert _run([tmux, 'has-session', '-t', '=dev-repo-1:'], env=first, cwd=base).returncode != 0
    assert _run(['ps', '-p', old_pid, '-o', 'pid='], env=first, cwd=base).returncode != 0
    launches = (base / 'agents.log').read_text().splitlines()
    assert len(launches) == 2 and all(SID in row for row in launches)
    assert str(workspace) in launches[-1]
    assert not (base / 'second/agents.log').exists()


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_public_saved_resume_accepts_equivalent_repository_records(two_hosts, agent):
    base, first, second = two_hosts
    workspace = saved_source(base, first, agent)
    stopped = _shell(base, first, '_dev_kill_one dev-repo-1 1')
    assert stopped.returncode == 0, stopped.stderr
    origin = _run(['git', '-C', str(workspace), 'remote', 'set-url', 'origin',
                   'https://github.com/org/repo.git'], env=first, cwd=base)
    assert origin.returncode == 0, origin.stderr
    # Simulate GitHub's rename proof and a user selecting the actual picker row.
    # All history, source validation, checkpointing and launch code stays real.
    gh = base / 'bin/gh'
    gh.write_text('''#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ['repo', 'view']:
    print(json.dumps(dict(id='R_synthetic', nameWithOwner='org/repo')))
else:
    print(json.dumps(dict(data=dict(repository={}))))
''')
    gh.chmod(0o755)
    fzf = base / 'bin/fzf'
    fzf.write_text('''#!/usr/bin/env python3
import sys
rows = sys.stdin.read().splitlines()
assert len(rows) == 1, rows
print('\\nenter\\n' + rows[0])
''')
    fzf.chmod(0o755)
    recorded = _shell(base, first, f'_dev_history_record {SID} {agent} {shlex.quote(str(workspace))} resume')
    assert recorded.returncode == 0, recorded.stderr
    seed = '''
import runpy
from pathlib import Path
h = runpy.run_path(str(Path(__import__('os').environ['T_TEST_ROOT']) / 'libexec/t_history.py'))
store = h['Store']()
try:
    row = next(row for row in store.records() if row['repo'] == 'org/repo')
    store.merge({**row, 'repo': 'old-org/repo'})
finally:
    store.close()
'''
    seeded = _run(['python3', '-c', seed], env=dict(first, T_TEST_ROOT=str(ROOT)), cwd=base)
    assert seeded.returncode == 0, seeded.stderr
    (workspace / 'unfinished.txt').write_text('keep edits through repository rename\n')
    resumed = _shell_tty(base, first, 't resume repo 1')
    assert resumed.returncode == 0, resumed.stdout
    launches = (base / 'agents.log').read_text().splitlines()
    assert len(launches) == 2 and all(SID in row for row in launches)
    owners = _run([str(base / 'bin/tmux'), 'list-sessions', '-F', '#{session_path}'], env=first, cwd=base)
    assert owners.returncode == 0 and len(owners.stdout.strip().splitlines()) == 1
    assert (Path(owners.stdout.strip()) / 'unfinished.txt').read_text() == 'keep edits through repository rename\n'
    assert not (base / 'second/agents.log').exists()
