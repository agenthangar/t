"""End-to-end saved resume with real Git/tmux and a synthetic native agent."""
import json
from pathlib import Path
import time

import pytest

from test_session_lifecycle import lifecycle, _run, _shell, ROOT, SID
from test_lifecycle_transfer_e2e import saved_source


def test_saved_resume_preparation_and_readiness_are_measured_separately(lifecycle):
    base, env = lifecycle
    workspace = saved_source(base, env, 'codex')
    stopped = _shell(base, env, '_dev_kill_one dev-repo-1 1')
    assert stopped.returncode == 0, stopped.stderr
    started = time.time()
    resumed = _run(['python3', str(ROOT / 'bin/t'), '__resume-saved', 'repo', SID, 'codex', 'launch', '-'],
                   env=env, cwd=base, timeout=60)
    finished = time.time()
    assert resumed.returncode == 0, resumed.stderr
    folder = Path(env['XDG_STATE_HOME']) / 't/lifecycle/operations'
    receipts = [json.loads(path.read_text()) for path in folder.glob('*.json')]
    receipt = next(r for r in receipts if r['verb'] == 'resume' and r['started'] >= started)
    assert receipt['phase'] == 'complete'
    stages = receipt['timings']
    assert set(stages) == {'inspect', 'ownership', 'launch', 'ready', 'writer-ready', 'record'}
    assert all(stage['seconds'] >= 0 for stage in stages.values())
    assert started <= stages['launch']['started'] <= finished
    print('\nSYNTHETIC_RESUME_TIMING ' + json.dumps(dict(
        total_seconds=round(finished - started, 4),
        preparation_before_launch_seconds=round(stages['launch']['started'] - started, 4),
        stages={name: round(value['seconds'], 4) for name, value in stages.items()},
        native_codex_measured=False)))
    launches = (base / 'agents.log').read_text().splitlines()
    assert len(launches) == 2 and all(SID in line for line in launches)
    owner = _shell(base, env, '_dev_session_sid ' + resumed.stdout.strip())
    assert owner.returncode == 0 and owner.stdout.strip() == SID
    assert workspace.exists()


def test_numbered_resume_with_unindexed_fast_cli_in_another_workspace(lifecycle):
    import subprocess
    from test_session_lifecycle import _until, UNINDEXED

    base, env = lifecycle
    workspace = saved_source(base, env, 'codex')
    stopped = _shell(base, env, '_dev_kill_one dev-repo-1 1')
    assert stopped.returncode == 0, stopped.stderr
    # Exercise the public picker with both staged and unstaged edits at an advanced tip.
    import shutil
    picker = shutil.which('fzf')
    if not picker:
        pytest.skip('interactive numbered-resume fixture requires fzf')
    git = lambda *args: subprocess.check_output(['git', '-C', str(workspace), *args], env=env)
    tracked = workspace / 'work.txt'
    tracked.write_text('commit\\n')
    git('add', tracked.name)
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'Advance fixture branch')
    tracked.write_text('staged\\n')
    git('add', tracked.name)
    tracked.write_text('working\\n')
    (workspace / 'untracked.txt').write_text('untracked\\n')
    def state():
        return [git(*args) for args in [('rev-parse', 'HEAD'), ('branch', '--show-current'),
                ('worktree', 'list', '--porcelain'), ('diff', '--cached'), ('diff',),
                ('status', '--porcelain')]]
    before = state()
    # Drive fzf through the fixture's private tmux server and actual controlling PTY.
    def tmux(*args):
        return subprocess.check_output([str(base/'bin/tmux'), *args], env=env, cwd=base, text=True)
    other = base / 'unrelated'
    other.mkdir()
    sibling = subprocess.Popen([str(base / 'bin/codex'), '--enable', 'fast_mode', '--no-daemon'],
                               cwd=other, env={**env, 'T_TEST_FRESH_SID': UNINDEXED[0]})
    try:
        lock = Path(env['CODEX_HOME']) / 'thread-writer-locks' / (UNINDEXED[0] + '.lock')
        _until(lock.exists)
        import shlex
        status = base / 'resume.status'
        command = 'unset TMUX; t resume repo 1; print -r -- $? > ' + shlex.quote(str(status))
        tmux('new-session', '-d', '-s', 'picker-test', '-x', '100', '-y', '28',
             'zsh', '-lic', command)
        _until(lambda: 'resume repo' in tmux('capture-pane', '-p', '-t', 'picker-test'), seconds=45)
        tmux('send-keys', '-t', 'picker-test', 'Enter')
        _until(status.exists, seconds=60)
        assert status.read_text().strip() == '0'
        assert state() == before
        owner = _shell(base, env, '_dev_session_sid dev-repo-1')
        assert owner.stdout.strip() == SID
        assert str(workspace) in (base / 'agents.log').read_text().splitlines()[-1]
        assert sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=5)


@pytest.mark.parametrize('cancel', [False, True])
def test_numbered_resume_repair_offers_fresh_worktree(lifecycle, cancel):
    from test_session_lifecycle import _shell_tty, _unindexed_writers, UNINDEXED

    base, env = lifecycle
    workspace = saved_source(base, env, 'codex')
    stopped = _shell(base, env, '_dev_kill_one dev-repo-1 1')
    assert stopped.returncode == 0, stopped.stderr
    saved = _shell(base, env, 'git -C "$T_TEST_WT" rev-parse HEAD').stdout.strip()
    assert saved
    edits = workspace / 'unfinished.txt'
    edits.write_text('Keep these edits in the original worktree.\n')
    picker = base / 'bin/fzf'
    picker.write_text("#!/bin/sh\nhead -n 1\n")
    picker.chmod(0o755)
    attach_log = base / 'attached.log'
    tmux = base / 'bin/tmux'
    tmux.write_text(tmux.read_text().replace('attach-session) shift;',
        'attach-session) printf "%s\\n" "$*" > "$T_TEST_ATTACH_LOG"; shift;'))

    responses = []
    def respond(screen):
        if not responses and b'Could not resume' in screen and b'Cancel' in screen:
            responses.append('repair')
            return b'\r'
        if len(responses) == 1 and b'Resume in fresh worktree' in screen and saved[:12].encode() in screen:
            responses.append('fresh')
            return b'\x1b' if cancel else b'\r'
        return None

    with _unindexed_writers(base, env, sids=UNINDEXED[:1]):
        result = _shell_tty(base, {**env, 'TERM': 'xterm-256color', 'T_TEST_ATTACH_LOG': str(attach_log)},
                            't resume repo 1',
                            timeout=120, respond=respond)
        assert responses == ['repair', 'fresh'], result.stdout
        assert result.returncode == (130 if cancel else 0), result.stdout
        assert 'Local edits stay' in result.stdout
        assert edits.read_text() == 'Keep these edits in the original worktree.\n'
        assert _shell(base, env, 'git -C "$T_TEST_WT" rev-parse HEAD').stdout.strip() == saved
        target = workspace.with_name('repo-2')
        if cancel:
            assert not target.exists()
            assert not attach_log.exists()
            assert len((base / 'agents.log').read_text().splitlines()) == 1
        else:
            assert target.is_dir() and not (target / edits.name).exists()
            pane_id = _run([str(tmux), 'display-message', '-p', '-t', '=dev-repo-2:',
                            '#{pane_id}'], env=env, cwd=base)
            assert pane_id.returncode == 0 and pane_id.stdout.strip().startswith('%')
            assert attach_log.read_text().strip() == f'attach-session -t {pane_id.stdout.strip()}'
            owner = _shell(base, env, '_dev_session_sid dev-repo-2')
            assert owner.returncode == 0 and owner.stdout.strip() == SID
            commit = _run(['git', '-C', str(target), 'rev-parse', 'HEAD'], env=env, cwd=base)
            assert commit.stdout.strip() == saved
            launches = (base / 'agents.log').read_text().splitlines()
            assert len(launches) == 2 and str(target) in launches[-1] and SID in launches[-1]
    assert not list(workspace.parent.glob('.*history-reservation'))


@pytest.mark.parametrize(('agent', 'detached'), [('codex', '0'), ('claude', '1')])
def test_numbered_open_reopens_local_conversation_without_ssh(lifecycle, agent, detached):
    base, env = lifecycle
    workspace = saved_source(base, env, agent)
    stopped = _shell(base, env, '_dev_kill_one dev-repo-1 1')
    assert stopped.returncode == 0, stopped.stderr
    with (base / 'config/t/local.zsh').open('a') as config:
        config.write('REMOTE_HOSTS[offline]=offline.invalid\n')
    ssh = base / 'bin/ssh'
    ssh.write_text('#!/bin/sh\necho "$*" >> "$HOME/ssh-attempts"\nexit 255\n')
    ssh.chmod(0o755)
    marker = workspace / 'unfinished.txt'
    marker.write_text('keep my edits\n')
    result = _shell(base, {**env, 'T_MCP_DETACHED': detached}, 't open repo 1')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (Path(env['HOME']) / 'ssh-attempts').exists()
    assert _shell(base, env, '_dev_session_sid dev-repo-1').stdout.strip() == SID
    launches = (base / 'agents.log').read_text().splitlines()
    assert len(launches) == 2 and all(SID in line for line in launches)
    assert marker.read_text() == 'keep my edits\n'
