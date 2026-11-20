"""End-to-end saved resume with real Git/tmux and a synthetic native agent."""
import json
from pathlib import Path
import time

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
    from test_session_lifecycle import _shell_tty, _until, UNINDEXED

    base, env = lifecycle
    workspace = saved_source(base, env, 'codex')
    stopped = _shell(base, env, '_dev_kill_one dev-repo-1 1')
    assert stopped.returncode == 0, stopped.stderr
    # Select the exact row through the public numbered resume's history picker.
    picker = base / 'bin/fzf'
    picker.write_text("#!/bin/sh\nprintf '\\n\\n'\nhead -n 1\n")
    picker.chmod(0o755)
    other = base / 'unrelated'
    other.mkdir()
    sibling = subprocess.Popen([str(base / 'bin/codex'), '--enable', 'fast_mode', '--no-daemon'],
                               cwd=other, env={**env, 'T_TEST_FRESH_SID': UNINDEXED[0]})
    try:
        lock = Path(env['CODEX_HOME']) / 'thread-writer-locks' / (UNINDEXED[0] + '.lock')
        _until(lock.exists)
        result = _shell_tty(base, env, 't resume repo 1', timeout=60)
        assert result.returncode == 0, result.stdout
        assert 'Waiting for conversation ownership' not in result.stdout
        owner = _shell(base, env, '_dev_session_sid dev-repo-1')
        assert owner.stdout.strip() == SID, result.stdout
        assert str(workspace) in (base / 'agents.log').read_text().splitlines()[-1]
        assert sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=5)
