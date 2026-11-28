"""Installed command regression with a real terminal and blocked history worker."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from _install_helpers import run_install
from test_session_lifecycle import lifecycle

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed(lifecycle, tmp_path):
    base, _ = lifecycle
    if not shutil.which('fzf'):
        pytest.skip('fzf required')
    source, home, binary = (tmp_path / name for name in ('installed-source', 'installed-home', 'installed-bin'))
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', 'artifacts', '.coverage'))
    (source / '.t-release-version').write_text('v0.6.1\n')
    home.mkdir()
    # Ubuntu's global zshrc runs compinit before the installed user startup.
    # Keep this standalone fixture independent of host completion permissions.
    (home / '.zshenv').write_text('skip_global_compinit=1\n')
    binary.mkdir()
    shutil.copy2(base / 'bin/codex', binary / 'codex')
    (binary / 'python3').symlink_to(sys.executable)
    with tempfile.TemporaryDirectory(prefix='t-detail-', dir='/tmp') as temporary:
        runtime = Path(temporary).resolve()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('T_', 'COV_CORE_', 'CLAUDE_', 'CODEX_'))
               and key not in ('TMUX', 'TMUX_PANE')}
        env.update(HOME=str(home), ZDOTDIR=str(home), CODEX_HOME=str(home / '.codex'),
                   XDG_CONFIG_HOME=str(home / '.config'), XDG_CACHE_HOME=str(home / '.cache'),
                   XDG_STATE_HOME=str(home / '.local/state'), XDG_DATA_HOME=str(home / '.local/share'),
                   XDG_RUNTIME_DIR=str(runtime), TMUX_TMPDIR=str(runtime),
                   T_NO_MCP='1', T_NO_PERMISSIONS='1', T_NO_TRUST='1', T_NO_UPDATE_CHECK='1',
                   T_RECOVERY_DISABLE='1', T_TEST_AGENT_LOG=str(tmp_path / 'agents.log'),
                   T_TEST_AGENT_SECONDS='180', TERM='xterm-256color')
        installed = run_install(source, home, **{key: value for key, value in env.items()
                                               if key not in ('HOME',)})
        assert installed.returncode == 0, installed.stdout + installed.stderr
        env['PATH'] = str(binary) + os.pathsep + str(home / 'bin') + os.pathsep + str(home / '.local/bin') + os.pathsep + env['PATH']
        with (home / '.zshrc').open('a') as startup:
            startup.write('\nexport PATH=' + shlex.quote(env['PATH']) + '\n')
        release = tmp_path / 'release-history'
        real_zsh = shutil.which('zsh')
        hold = tmp_path / 'hold-details'
        wrapper = (
            '#!/bin/sh\ncase "$*" in\n'
            '*"_dev_session_rows --details"*)\n'
            f'  touch {shlex.quote(str(tmp_path / "collecting"))}\n'
            f'  while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.05; done;;\n'
            '*"_dev_session_rows --lightweight"*)\n'
            f'  while [ -f {shlex.quote(str(hold))} ]; do sleep 0.05; done;;\n'
            f'esac\nexec {shlex.quote(real_zsh)} "$@"\n'
        )
        (binary / 'zsh').write_text(wrapper)
        (binary / 'zsh').chmod(0o755)
        repo = tmp_path / 'api'
        repo.mkdir()
        config = home / '.config/t/local.zsh'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('DEV_REPOS[api]=' + shlex.quote(str(repo)) + '\n')
        (home / '.cache/claude-sessions').mkdir(parents=True, exist_ok=True)
        (home / '.codex').mkdir(exist_ok=True)
        def run(*args, timeout=30):
            return subprocess.run(args, env=env, cwd=repo, text=True, capture_output=True, timeout=timeout)
        def shell(command, timeout=30):
            return run(real_zsh, '-lic', command, timeout=timeout)
        # The sandbox never bootstraps a job into the test user's login domain.
        service = subprocess.Popen([sys.executable, str(source/'libexec/t_session_cache.py'), 'serve-managed'],
                                   cwd=home, env=env)
        try:
            check = shell('t --version')
            assert str(source) in check.stdout, check.stdout + check.stderr
            yield dict(run=run, shell=shell, env=env, root=tmp_path, home=home, repo=repo,
                       binary=binary, source=source, zsh=real_zsh, release=release)
        finally:
            release.touch()
            service.terminate()
            service.wait(timeout=8)
            run('tmux', 'kill-server')
            for line in subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True).splitlines():
                fields = line.strip().split(None, 1)
                if len(fields) == 2 and str(tmp_path) in fields[1]:
                    try:
                        os.kill(int(fields[0]), 15)
                    except ProcessLookupError:
                        pass


def create(installed, number):
    sid = f'11111111-1111-4111-8111-{number:012d}'
    title = f'Session {number} invoice investigation'
    path = installed['home'] / f'.codex/sessions/2026/11/27/rollout-{sid}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(record) for record in [
        dict(type='session_meta', payload=dict(id=sid, cwd=str(installed['repo']), source='cli')),
        dict(type='response_item', timestamp='2026-11-27T20:00:00Z', payload=dict(type='message', role='user', content=[dict(type='input_text', text=title)])),
        dict(type='turn_context', timestamp='2026-11-27T20:00:00Z', payload=dict(model='gpt-6.2-sol')),
        dict(type='event_msg', timestamp='2026-11-27T20:00:00Z', payload=dict(type='task_started')),
    ]) + '\n')
    with sqlite3.connect(installed['home'] / '.codex/state_5.sqlite') as db:
        db.execute('create table if not exists threads (id, rollout_path, cwd, name, title, updated_at, archived, source, first_user_message, model)')
        db.execute('insert into threads values (?,?,?,?,?,?,?,?,?,?)',
                   (sid, str(path), str(installed['repo']), title, title, 1, 0, 'cli', title, 'gpt-6.2-sol'))
    command = 'T_TEST_FRESH_SID=' + sid + ' ' + shlex.quote(str(installed['binary'] / 'codex'))
    result = installed['run']('tmux', 'new-session', '-d', '-s', f'dev-api-{number}', '-c', str(installed['repo']), command)
    assert result.returncode == 0, result.stderr
    # The launch stamp deliberately points elsewhere. The current process wins.
    for name, value in [('DEV_AGENT', 'codex'), ('CLAUDE_RESUME_ID', '22222222-2222-4222-8222-222222222222')]:
        installed['run']('tmux', 'set-environment', '-t', f'dev-api-{number}', name, value)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        entries = list((installed['home'] / '.cache/claude-sessions').glob('[0-9]*'))
        if any(sid in p.read_text() for p in entries if p.is_file()):
            return title
        time.sleep(.03)
    pytest.fail('synthetic agent did not register')


def test_installed_first_list_and_picker_have_metadata(installed):
    title = create(installed, 1)
    pending = installed['repo'] / 'pending.txt'
    pending.write_text('preserve these edits\n')
    for command in ('t ls api', 't ls api -w'):
        listed = installed['shell'](command)
        assert listed.returncode == 0, listed.stderr
        assert all(text in listed.stdout for text in (title, 'gpt-6.2-sol', '2026-11-27')), listed.stdout + listed.stderr
        assert '\x1b[2K' not in listed.stderr
        assert not installed['release'].exists()
        deadline = time.monotonic() + 5
        while not (installed['root'] / 'collecting').exists() and time.monotonic() < deadline:
            time.sleep(.03)
        assert (installed['root'] / 'collecting').exists()
        assert pending.read_text() == 'preserve these edits\n'
        assert installed['run']('tmux', 'has-session', '-t', '=dev-api-1:').returncode == 0
    run = installed['run']
    hold = installed['root'] / 'hold-details'
    hold.touch()
    launched = run('tmux', 'new-session', '-d', '-x', '180', '-y', '36', '-s', 'picker',
                   installed['zsh'] + ' -lic ' + shlex.quote('t open api -w'))
    assert launched.returncode == 0, launched.stderr
    # Wait for actual repaint, including when the shell is capturing scanner stdout.
    frames = set()
    deadline = time.monotonic() + 15
    try:
        while len(frames) < 2 and time.monotonic() < deadline:
            contents = run('tmux', 'capture-pane', '-p', '-t', 'picker:').stdout
            frames.update(glyph for glyph in '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' if glyph in contents)
            time.sleep(.05)
        assert len(frames) >= 2, contents
    finally:
        hold.unlink(missing_ok=True)
    deadline = time.monotonic() + 15
    frame = ''
    while time.monotonic() < deadline:
        frame = run('tmux', 'capture-pane', '-p', '-t', 'picker:').stdout
        if title in frame:
            break
        time.sleep(.1)
    assert title in frame and '2026-11-27' in frame, frame
    assert not installed['release'].exists()
    run('tmux', 'send-keys', '-t', 'picker:', 'C-_')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        details = run('tmux', 'capture-pane', '-p', '-t', 'picker:').stdout
        if 'gpt-6.2-sol' in details:
            break
        time.sleep(.05)
    assert 'gpt-6.2-sol' in details, details
    run('tmux', 'send-keys', '-t', 'picker:', 'Escape')


def test_installed_61_slots_have_metadata_while_history_is_blocked(installed):
    titles = [create(installed, number) for number in range(1, 62)]
    started = time.monotonic()
    result = installed['shell']('t ls api -w', timeout=120)
    elapsed = time.monotonic() - started
    print(f'61-slot first details completed in {elapsed:.2f}s')
    assert result.returncode == 0, result.stderr
    assert all(title in result.stdout for title in titles), result.stdout + result.stderr
    assert result.stdout.count('gpt-6.2-sol') == 61
    assert (installed['root'] / 'collecting').exists()
    assert not installed['release'].exists()
