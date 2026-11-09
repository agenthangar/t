"""Recent doctor evidence, with disposable logs, receipts and shell failures."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import pytest

from test_lifecycle_engine import engine, ROOT


def record(engine, tmp_path, number=1, **values):
    folder = engine.state_home() / 'operations'
    folder.mkdir(parents=True, exist_ok=True)
    value = dict(version=1, id=f'{number:032x}', verb='resume',
                 arguments=[f'dev-example-{number}', str(tmp_path), 'saved-id', 'claude'],
                 phase='failed', pending='launch', started=number, updated=number,
                 error='respawn pane failed: fork failed: Device not configured', error_code=23,
                 log_start={'offset': 0})
    value.update(values)
    path = folder / (value['id'] + '.json')
    path.write_text(json.dumps(value))
    return path, value


def log(engine, name='dev-example-1', text='Error: connection closed\n'):
    path = Path.home() / '.tmux-logs' / (name + '.log')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_recent_order_outcomes_and_limit_are_read_only(engine, tmp_path):
    record(engine, tmp_path, 1)
    record(engine, tmp_path, 2, phase='complete', pending=None, error=None, error_code=0)
    record(engine, tmp_path, 3, phase='ready', error=None, error_code=None)
    orphan = log(engine, 'dev-older-4', 'a user conversation\nError: old failure\n')
    os.utime(orphan, (0, 0))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path.home().parent.rglob('*') if p.is_file()}
    report = engine.recent_diagnostics(limit=3)
    assert [r['session'] for r in report['records']] == ['dev-example-3', 'dev-example-2', 'dev-example-1']
    rendered = '\n'.join(engine.recent_diagnostic_lines(report))
    assert 'incomplete / outcome unknown' in rendered and 'operation completed' in rendered
    assert 'FAILED' in rendered and 'operation exit 23' in rendered
    assert 'Device not configured' in rendered and 'failure may precede agent startup' in rendered
    assert 'later exit status' in rendered
    all_records = engine.recent_diagnostics(limit=20)
    assert all_records['records'][-1]['phase'] == 'log-only'
    assert all_records['records'][-1]['log_evidence']['lines'] == ['Error: old failure']
    assert 'outcome unknown (log only)' in '\n'.join(engine.recent_diagnostic_lines(all_records))
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path.home().parent.rglob('*') if p.is_file()}
    assert before == after


def test_log_boundaries_redaction_controls_and_stored_older_excerpt(engine, tmp_path):
    path = log(engine, text='Error: earlier attempt\n')
    cursor = engine.diagnostic_log_start({'session': path.stem})
    first, value = record(engine, tmp_path, log_start=cursor)
    with path.open('a') as stream:
        stream.write('\x1b[31mError: token="secret words"\x1b[0m\n')
        stream.write('user conversation contents\n')
        stream.write('\x1b]52;c;clipboard\x07Error: password=hidden Bearer abc123\u202e\n')
        stream.write('fatal: sk-abcdefghijklmnop ghp_abcdefghijklmnop\n')
    evidence = engine.diagnostic_log(value)
    text = '\n'.join(evidence['lines'])
    assert 'earlier attempt' not in text and 'conversation contents' not in text
    for hidden in ('secret words', 'hidden', 'abc123', 'clipboard', 'abcdefghijklmnop', '\x1b', '\u202e'):
        assert hidden not in text
    assert '[redacted]' in text
    value['log_evidence'] = evidence
    first.write_text(json.dumps(value))
    record(engine, tmp_path, 2, arguments=value['arguments'], log_start=engine.diagnostic_log_start({'session': path.stem}))
    with path.open('a') as stream:
        stream.write('Error: newest attempt\n')
    records = engine.recent_diagnostics()['records']
    assert records[0]['log_evidence']['lines'] == ['Error: newest attempt']
    assert 'newest attempt' not in '\n'.join(records[1]['log_evidence']['lines'])
    assert 'earlier attempt' not in '\n'.join(records[1]['log_evidence']['lines'])


def test_log_rotation_truncation_and_missing_boundaries(engine, tmp_path):
    path = log(engine)
    cursor = engine.diagnostic_log_start({'session': path.stem})
    _, value = record(engine, tmp_path, log_start=cursor)
    path.rename(path.with_suffix('.old'))
    path.write_text('Error: unrelated replacement\n')
    assert not engine.diagnostic_log(value)['lines']
    assert 'rotated' in engine.diagnostic_log(value)['note']
    value['log_start'] = {'offset': 9999}
    assert 'truncated' in engine.diagnostic_log(value)['note']
    value.pop('log_start')
    assert 'no log boundary' in engine.diagnostic_log(value)['note']
    assert engine.diagnostic_log({})['note'] == 'no session log identity'


def test_bounded_tail_and_line_count(engine, tmp_path):
    path = log(engine, text='Error: very old\n' + 'x' * 20000 + '\n' + ''.join(f'Error: failure {n}\n' for n in range(20)))
    _, value = record(engine, tmp_path)
    evidence = engine.diagnostic_log(value)
    assert evidence['lines'] == [f'Error: failure {n}' for n in range(12, 20)]
    assert 'truncated' in engine.diagnostic_text('x' * 5000)
    assert 'very old' not in '\n'.join(evidence['lines'])


@pytest.mark.parametrize('bad', ['{', '[]', '{"version":99}', '{"version":1,"arguments":[]}', 'null'])
def test_malformed_old_receipts_do_not_break_report(engine, tmp_path, bad):
    path, value = record(engine, tmp_path)
    path.write_text(bad)
    report = engine.recent_diagnostics()
    assert report['records'] == []
    assert 'No recent lifecycle' in '\n'.join(engine.recent_diagnostic_lines(report))


def test_safe_reads_refuse_symlinks_fifos_and_oversized_receipts(engine, tmp_path):
    path, value = record(engine, tmp_path)
    path.write_bytes(b'x' * (128 * 1024 + 1))
    assert 'skipped' in engine.recent_diagnostics()['notes'][0]
    secret = tmp_path / 'secret'
    secret.write_text('Error: private secret\n')
    path = log(engine)
    path.unlink()
    path.symlink_to(secret)
    assert 'unsafe' in engine.diagnostic_log(value)['note']
    assert engine.diagnostic_log_start({'session': path.stem}) is None
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    with pytest.raises(OSError, match='regular'):
        engine.diagnostic_read(fifo, 100)
    with pytest.raises(OSError, match='symlink'):
        engine.diagnostic_read(path, 100)
    assert engine.diagnostic_log_start({}) is None
    directory = tmp_path / 'linked'
    directory.symlink_to(secret.parent, target_is_directory=True)
    notes = []
    assert not engine.diagnostic_candidates(directory, '.log', notes)
    assert notes


def test_invalid_context_timestamps_and_missing_phase(engine, tmp_path):
    assert engine.diagnostic_context('new', ['../evil', '/work']) == {}
    assert engine.diagnostic_context('new', [1, 2]) == {}
    assert engine.diagnostic_context('unknown', []) == {}
    ctx = engine.diagnostic_context('move', ['a', 'b', '/work', 'sid', 'codex', 'repo', '2', '1'])
    assert ctx['session'] == 'dev-repo-2' and ctx['sid'] == 'sid'
    record(engine, tmp_path, started=float('inf'))
    assert engine.recent_diagnostics()['notes']
    path, value = record(engine, tmp_path, started=10**100)
    value.pop('phase')
    path.write_text(json.dumps(value))
    lines = engine.recent_diagnostic_lines(engine.recent_diagnostics())
    assert any('timestamp unavailable' in line for line in lines)


def test_real_shell_failure_is_retained_before_log_exists(engine, tmp_path):
    script = '''
source "$TEST_ROOT/zsh/lifecycle.zsh"
_t_lifecycle_effect() {
  [[ $1 == preflight ]] && return 0
  print -u2 'respawn pane failed: fork failed: Device not configured; token=private'
  return 23
}
_t_lifecycle_run_impl new dev-example-1 "$HOME/work" dev/example 1 claude
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(TEST_ROOT=str(ROOT), T_HOME=str(ROOT))
    env.pop('T_LIFECYCLE_ID', None)
    result = subprocess.run(['zsh', '-f', '-c', script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 23, result.stderr
    assert 'fork failed: Device not configured' in result.stderr
    report = engine.recent_diagnostics()
    value = report['records'][0]
    assert value['phase'] == 'failed' and value['pending'] == 'launch'
    assert value['error_code'] == 23 and 'Device not configured' in value['error']
    assert 'private' not in value['error'] and '[redacted]' in value['error']
    assert 'missing' in value['log_evidence']['note']
    assert Path(value['record']).stat().st_mode & 0o777 == 0o600


def test_doctor_renders_recent_failure_and_accepts_count(engine, tmp_path, t_mod, monkeypatch, capsys):
    record(engine, tmp_path)
    log(engine)
    monkeypatch.setattr(t_mod, 'HOME', str(Path.home()))
    monkeypatch.setattr(t_mod, '_perm_root', lambda: None)
    monkeypatch.setattr(t_mod, '_install_probe', lambda: {name: {'version': None, 'logged_in': None} for name in t_mod.INSTALL_AGENTS})
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda args, **kw: subprocess.CompletedProcess(args, 1, '', ''))
    before = sorted(str(p) for p in Path.home().parent.rglob('*'))
    args = t_mod.build_parser().parse_args(['doctor', '--recent', '1'])
    assert t_mod.cmd_doctor(argparse.Namespace(repos={}, worktree_root=str(tmp_path)), args) == 0
    output = capsys.readouterr().out
    assert 'Recent session attempts' in output and 'Device not configured' in output
    assert 'Error: connection closed' in output
    assert sorted(str(p) for p in Path.home().parent.rglob('*')) == before
    for invalid in ('0', '21', 'bad'):
        with pytest.raises(SystemExit):
            t_mod.build_parser().parse_args(['doctor', '--recent', invalid])


def test_scan_limits_and_unreadable_directory(engine, tmp_path, monkeypatch):
    folder = tmp_path / 'records'
    folder.mkdir()
    for n in range(105):
        (folder / f'{n}.json').write_text('{}')
    notes = []
    assert len(engine.diagnostic_candidates(folder, '.json', notes)) == 100
    assert '100 newest' in notes[0]
    class Many:
        def __enter__(self):
            return iter([type('Entry', (), {'name': 'ignored'})()] * 2002)
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(engine.os, 'scandir', lambda _: Many())
    notes = []
    assert engine.diagnostic_candidates(folder, '.json', notes) == []
    assert '2000 entries' in notes[0]
    def denied(_):
        raise PermissionError('denied')
    monkeypatch.setattr(engine.os, 'scandir', denied)
    assert engine.diagnostic_candidates(folder, '.json', notes) == []
    assert 'unreadable' in notes[-1]


def test_empty_report_does_not_create_state_and_old_attempts_have_no_invented_log(engine, tmp_path):
    assert not engine.state_home().exists()
    assert engine.recent_diagnostics()['records'] == []
    assert not engine.state_home().exists()
    first, value = record(engine, tmp_path)
    value.pop('log_start')
    first.write_text(json.dumps(value))
    record(engine, tmp_path, 2, arguments=value['arguments'])
    report = engine.recent_diagnostics()
    assert report['records'][1]['log_evidence']['lines'] == []
    assert 'no saved log excerpt' in report['records'][1]['log_evidence']['note']


def test_adapter_attach_keeps_terminal_stderr(engine, tmp_path):
    import pty
    import select
    import time
    root = tmp_path / 'runtime'
    (root / 'libexec').mkdir(parents=True)
    # Tiny peer for the real zsh bridge; it requests attachment, then exits.
    (root / 'libexec/t_lifecycle.py').write_text('''
import json, sys
if sys.argv[1] == 'bridge':
 print(json.dumps(dict(call='attach', args=['', 'dev-example-1', '0'])), flush=True)
 reply=json.loads(sys.stdin.readline())
 print(json.dumps(dict(done=reply['code'])), flush=True)
elif sys.argv[1] == 'decode':
 print("_t_lifecycle_effect attach '' dev-example-1 0")
elif sys.argv[1] == 'reply':
 print(json.dumps(dict(code=int(sys.argv[2]), output='')))
''')
    script = '''source "$TEST_ROOT/zsh/lifecycle.zsh"
_t_lifecycle_effect() { [[ -t 2 ]] || return 91; print -u2 'ATTACHED WITH TERMINAL'; }
_t_lifecycle_run_impl ignored
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(TEST_ROOT=str(ROOT), T_HOME=str(root))
    master, slave = pty.openpty()
    proc = subprocess.Popen(['zsh', '-f', '-c', script], env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
        assert proc.wait(timeout=1) == 0, output
        assert b'ATTACHED WITH TERMINAL' in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)


@pytest.mark.parametrize('argv', [['doctor', '--recent', '20'], ['system', 'diagnose', '--recent', '2']])
def test_recent_alias_dispatch_stays_read_only(t_mod, monkeypatch, argv):
    seen = []
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: pytest.fail('automatic update'))
    monkeypatch.setitem(t_mod.IMPLEMENTED, 'doctor', lambda cfg, args: seen.append(args.recent) or 0)
    assert t_mod._command_readonly(argv)
    assert t_mod.main(argv) == 0
    assert seen == [int(argv[-1])]


def test_reply_keeps_last_error_and_old_protocol_compatibility(engine, tmp_path, capsys):
    output, error = tmp_path / 'stdout', tmp_path / 'stderr'
    output.write_text('protocol payload\n')
    error.write_text('x' * 30000 + '\nerror: latest failure password="private value"\n')
    assert engine.main(['reply', '17', str(output), str(error)]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply['code'] == 17 and reply['output'] == 'protocol payload\n'
    assert 'latest failure' in reply['stderr'] and 'private value' not in reply['stderr']
    assert len(reply['stderr']) <= 4096
    assert engine.main(['reply', '0', str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == {'code': 0, 'output': 'protocol payload\n'}


def test_latest_legacy_receipt_can_show_clearly_uncorrelated_shared_log(engine, tmp_path):
    path, value = record(engine, tmp_path)
    value.pop('log_start')
    path.write_text(json.dumps(value))
    log(engine, text='Error: an older failure\n')
    report = engine.recent_diagnostics()
    evidence = report['records'][0]['log_evidence']
    assert evidence['lines'] == ['Error: an older failure']
    assert 'may include earlier attempts' in evidence['note']


def test_new_log_identity_is_pinned_and_long_errors_keep_the_cause(engine, tmp_path):
    operation = engine.Operation('new', ['dev-example-1', str(tmp_path), 'branch', '1', 'claude'])
    assert operation.value['log_start'] == {'offset': 0}
    path = log(engine)
    error = engine.diagnostic_error('x' * 7000 + '\nError: final cause')
    operation.save('failed', error=error, error_code=1)
    assert operation.value['log_start']['identity']
    report = engine.recent_diagnostics()
    assert 'Error: final cause' in '\n'.join(engine.recent_diagnostic_lines(report))
    path.rename(path.with_suffix('.old'))
    path.write_text('Error: unrelated new log\n')
    evidence = engine.recent_diagnostics()['records'][0]['log_evidence']
    assert 'rotated' in evidence['note'] and 'saved excerpt' in evidence['note']
    assert evidence['lines'] == ['Error: connection closed']


def test_remote_attempt_never_reads_same_named_local_log(engine, tmp_path):
    arguments = ['', 'other-host', '/work', 'sid', 'codex', 'example', '1', '1']
    context = engine.diagnostic_context('move', arguments)
    assert engine.diagnostic_log_start(context) is None
    record(engine, tmp_path, verb='move', arguments=arguments)
    path = log(engine, text='Error: local session only\n')
    os.utime(path, (0, 0))
    report = engine.recent_diagnostics()
    remote, local = report['records']
    assert remote['log_evidence']['lines'] == []
    assert 'run t doctor on other-host' in remote['log_evidence']['note']
    assert local['log_evidence']['lines'] == ['Error: local session only']
    assert 'destination host: other-host' in '\n'.join(engine.recent_diagnostic_lines(report))
