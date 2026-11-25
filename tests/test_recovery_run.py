"""On-demand recovery preserves exact identities and worker ownership."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT, load_script
from test_auto_recovery import SID, auto, enable
from test_recovery_tmux import terminal


def test_command_routes_and_status(auto, t_mod, capsys, monkeypatch):
    for action in ('status', 'run', 'agent'):
        args = t_mod.build_parser().parse_args(t_mod._canonical_argv(['system', 'recovery', action]))
        assert args.action == action
    args = t_mod.build_parser().parse_args(t_mod._canonical_argv(['system', 'recovery', 'run', '--all']))
    assert args.all
    assert 'run' in t_mod._group_help('system recovery')
    assert 'agent' in t_mod._group_help('system recovery')
    monkeypatch.setattr(auto, 'status', lambda: {'enabled': True, 'running': True, 'sessions': [{'agent': 'codex', 'sid': SID}]})
    monkeypatch.setattr(auto, 'holding_panes', lambda: pytest.fail('status must not scan panes'))
    auto.command(SimpleNamespace(scope='system', action='status', json=False))
    assert capsys.readouterr().out == 'Automatic recovery  Enabled\nBackground agent    Running\n'
    auto.command(SimpleNamespace(scope='system', action='agent'))
    output = capsys.readouterr().out
    assert 't agent background status\n' in output
    assert 't agent background start recovery' in output


def test_requests_coalesce_without_restarting_worker(auto):
    enable(auto)
    auto.write(auto.root()/'heartbeat.json', {'at': time.time(), 'instance': 'worker-one'})
    passes = []
    engine = SimpleNamespace(tick=lambda **kwargs: passes.append(kwargs))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(auto.request_pass, 3) for _ in range(2)]
        deadline = time.monotonic()+2
        while len(list((auto.root()/'requests').glob('*.json'))) < 2:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert auto.requested_pass(engine, 'worker-one')
        assert [f.result() for f in futures] == [[], []]
    assert passes == [{'force': True}]
    assert not list((auto.root()/'requests').iterdir())


def test_request_refuses_disabled_stopped_and_changed_worker(auto, monkeypatch):
    with pytest.raises(ValueError, match='disabled'):
        auto.request_pass(.01)
    enable(auto)
    with pytest.raises(ValueError, match='unavailable'):
        auto.request_pass(.01)
    auto.write(auto.root()/'heartbeat.json', {'at': time.time(), 'instance': 'old'})
    real_write = auto.write
    def replace(path, data):
        real_write(path, data)
        if path.parent.name == 'requests':
            real_write(auto.root()/'heartbeat.json', {'at': time.time(), 'instance': 'new'})
    monkeypatch.setattr(auto, 'write', replace)
    with pytest.raises(ValueError, match='changed'):
        auto.request_pass(.1)
    assert not list((auto.root()/'requests').iterdir())


def test_force_pass_only_skips_connection_backoff(auto, monkeypatch):
    enable(auto)
    rows = {}
    for i, reason in enumerate(('Waiting for connection', 'Restoring session', 'Other startup failure')):
        rows[str(i)] = dict(agent='codex', sid=SID+str(i), generation=str(i), open=True,
                           safe=True, phase='outage', reason=reason, next_retry=time.time()+300)
    auto.write(auto.root()/'journal.json', {'sessions': rows})
    runtime = SimpleNamespace(live=lambda: [], inventory='cached')
    engine = auto.Engine(runtime)
    called = []
    monkeypatch.setattr(engine, 'recover', lambda *args: called.append(args[0]))
    monkeypatch.setattr(auto, 'enroll', lambda owners: None)
    engine.tick(force=True)
    assert runtime.inventory is None
    assert called == ['0']


def picker_module():
    sys.path.insert(0, str(REPO_ROOT/'libexec'))
    return load_script(REPO_ROOT/'libexec/t_recovery_picker.py', 'recovery_picker_test')


def fixture_rows():
    return [dict(agent='codex', sid=SID+str(i), generation=str(i), repo='api', slot=str(i),
                 cwd='/workspace', host='local', status=status, turn='2026-11-25 09:00',
                 title='Test '+str(i), prs=[], open=True)
            for i, status in enumerate(('Open pane', 'Status unknown', 'Restoring'), 1)]


def test_picker_filters_and_sort(tmp_path, monkeypatch):
    picker = picker_module()
    rows = fixture_rows()
    (tmp_path/'recovery.json').write_text(json.dumps(rows))
    (tmp_path/'refreshed').write_text('09:00')
    monkeypatch.setenv('T_RECOVERY_DATA', str(tmp_path))
    monkeypatch.setenv('T_PICKER_HEADER_HEIGHT', '40')
    monkeypatch.setenv('T_PICKER_COLOR', '1')
    monkeypatch.delenv('T_PICKER_PAN', raising=False)
    picker.render(tmp_path, 'current', 110)
    header = (tmp_path/'current-header').read_text()
    plain = re.sub(r'\x1b\[[0-9;]*m', '', header)
    assert all(s in plain for s in ('FILTER', 'SORT', 'Status unknown', 'Open pane', 'Restoring'))
    assert all(s not in plain for s in ('·', '—', 'Review launch', 'Owner unverified'))
    marker_lines = [line for line in plain.splitlines() if line.startswith(('Status ', 'PRs ', 'Agent '))]
    assert all(line[8] in '●↴>' for line in marker_lines)
    picker.render(tmp_path, 'state', 110)
    assert len((tmp_path/'current').read_text().splitlines()) == 1
    picker.render(tmp_path, 'clear-filters', 110)
    assert len((tmp_path/'current').read_text().splitlines()) == 3
    picker.render(tmp_path, 'next-sort', 110)
    sort = json.loads((tmp_path/'recovery-state.json').read_text())['sort']
    picker.render(tmp_path, 'toggle-sort', 110)
    picker.render(tmp_path, 'toggle-sort', 32)
    assert json.loads((tmp_path/'recovery-state.json').read_text())['sort'] == sort


def test_changed_selection_never_launches(monkeypatch):
    picker = picker_module()
    selected = fixture_rows()[1]
    monkeypatch.setattr(picker, 'recovery', lambda: {'status': lambda: {'sessions': [{**selected, 'generation': 'new'}]}})
    with pytest.raises(ValueError, match='Session changed'):
        picker.selected_command(selected, None, None)


def test_parallel_jobs_overlap_and_keep_failed_results(tmp_path):
    batch = load_script(REPO_ROOT/'libexec/t_session_batch.py', 'session_batch_test')
    script = ('import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); '
              'p.write_text(str(time.monotonic())); time.sleep(.3); '
              'print(sys.argv[2]); sys.exit(int(sys.argv[3]))')
    paths = [tmp_path/str(i) for i in range(3)]
    results = batch.run_jobs([[sys.executable, '-c', script, str(path), str(i), '1' if i == 1 else '0']
                              for i, path in enumerate(paths)])
    starts = [float(path.read_text()) for path in paths]
    assert max(starts)-min(starts) < .25
    assert [result['code'] for result in results] == [0, 1, 0]
    assert [result['output'].strip() for result in results] == ['0', '1', '2']


def test_recovery_shell_pins_source_over_target_worktree(auto, tmp_path, monkeypatch):
    import shutil
    import subprocess
    root = tmp_path/'source'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True)
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    for folder in ('bin', 'libexec', 'zsh'):
        (root/folder).mkdir()
    shutil.copy(REPO_ROOT/'libexec/t_dev.py', root/'libexec/t_dev.py')
    (root/'libexec/t_repos.py').write_text('')
    (root/'.t-install-version').write_text('1')
    (root/'bin/t').write_text('#!/bin/sh\nexit 0\n')
    (root/'bin/t').chmod(0o755)
    (root/'t.plugin.zsh').write_text('''
local here=${${(%):-%x}:A:h}
T_HOME=$(python3 "$here/libexec/t_dev.py" "$here" --explicit)
source "$T_HOME/zsh/worker.zsh"
''')
    (root/'zsh/worker.zsh').write_text('''
_dev_with_launch_locks() { shift 2; "$@"; }
_t_auto_resume_holding_claimed() { print -r -- "$T_HOME"; }
''')
    git('add', '.')
    git('commit', '-qm', 'fixture')
    old = tmp_path/'old'
    git('worktree', 'add', '-qb', 'dev/t-old', str(old))
    (old/'zsh/worker.zsh').write_text('_dev_with_launch_locks() { shift 2; "$@"; }\n')
    home = Path(os.environ['HOME'])
    home.mkdir(exist_ok=True)
    (home/'.zshrc').write_text('')
    monkeypatch.chdir(old)
    monkeypatch.delenv('T_DEV_CONTEXT', raising=False)
    monkeypatch.delenv('T_DEV_CONTEXT_OWNER', raising=False)
    monkeypatch.setattr(auto, 'ROOT', root)
    calls = []
    def execute(argv, **kwargs):
        value = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        calls.append(value)
        return value
    monkeypatch.setattr(auto, 'run', execute)
    auto.Runtime().shell(dict(session='dev-t-old', cwd=str(old), sid=SID, agent='codex'),
                         '_t_auto_resume_holding_claimed', 'unused')
    assert calls[-1].stdout.strip() == str(root)


def test_recovery_picker_keyboard_and_resize(terminal):
    import shlex
    from test_recovery_tmux import until
    _, tmux, _, env, home = terminal
    picker = picker_module()
    rows = fixture_rows()
    (home/'recovery.json').write_text(json.dumps(rows))
    (home/'refreshed').write_text('09:00')
    (home/'rows').write_text('\n'.join(picker.row_line(r, True) for r in rows)+'\n')
    script = home/'picker.sh'
    script.write_text('''#!/bin/sh
export T_HOME="%s" T_RECOVERY_DATA="%s" T_PICKER_SESSION_FILTERS=recovery
export T_PICKER_SOURCE_ROWS="$(cat "$HOME/rows")"
export T_PICKER_COLUMNS="%s"
. "$T_HOME/ui/fzf.sh"
_t_fzf_filtered "$T_PICKER_SOURCE_ROWS" "$T_PICKER_SOURCE_ROWS" open/resume Recovery '' '' --delimiter="$(printf '\\t')" --with-nth=-1 > "$HOME/selected"
printf '%%s' "$?" > "$HOME/done"
sleep 30
''' % (REPO_ROOT, home, picker.COLUMNS))
    assert tmux('new-session', '-d', '-x', '120', '-y', '32', '-s', 'picker', 'sh '+shlex.quote(str(script))).returncode == 0
    frame = lambda: tmux('capture-pane', '-p', '-t', 'picker').stdout
    until(lambda: 'SORT' in frame(), detail=frame)
    assert 'Review launch' not in frame()
    tmux('send-keys', '-t', 'picker', 'C-t')
    until(lambda: 'Status: Open pane' in frame(), detail=frame)
    tmux('send-keys', '-t', 'picker', 'C-f', 'C-y', 'C-s', 'C-s')
    tmux('send-keys', '-t', 'picker', 'Tab')
    tmux('resize-window', '-t', 'picker', '-x', '44', '-y', '20')
    until(lambda: 'STATUS' in frame(), detail=frame)
    tmux('resize-window', '-t', 'picker', '-x', '32', '-y', '12')
    until(lambda: 'STATUS' in frame() and 'api' in frame(), detail=frame)
    tmux('send-keys', '-t', 'picker', 'Tab', 'Enter')
    until(lambda: (home/'done').exists(), detail=frame)
    assert (home/'done').read_text() == '0'
    selected = (home/'selected').read_text().splitlines()
    assert len(selected) == 2
    assert len({tuple(line.split('\t')[:3]) for line in selected}) == 2


def test_cancel_parallel_preparation_terminates_workers(tmp_path, monkeypatch):
    batch = load_script(REPO_ROOT/'libexec/t_session_batch.py', 'cancel_batch_test')
    pid_file = tmp_path/'pid'
    original_sleep = time.sleep
    def cancel(_):
        deadline = time.monotonic()+2
        while not pid_file.exists() and time.monotonic() < deadline:
            original_sleep(.01)
        raise KeyboardInterrupt
    monkeypatch.setattr(batch.time, 'sleep', cancel)
    script = 'import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)'
    results = batch.run_jobs([[sys.executable, '-c', script, str(pid_file)]])
    assert results[0]['code'] == 130
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
