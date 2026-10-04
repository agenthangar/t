"""Desktop activity requires live backend file ownership, not saved metadata."""
from pathlib import Path
import plistlib
import runpy
import sqlite3
import subprocess

import pytest

SID = '01234567-89ab-cdef-0123-456789abcdef'


@pytest.fixture
def desktop(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'libexec/t_desktop.py'))
    bundle = tmp_path / 'Desktop.app'
    contents = bundle / 'Contents'
    contents.mkdir(parents=True)
    (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex'}))
    rollout = tmp_path / ('rollout-' + SID + '.jsonl')
    rollout.touch()
    db = sqlite3.connect(tmp_path / 'state_5.sqlite')
    db.execute('create table threads (id, cwd, name, title, rollout_path, archived, source)')
    db.execute('insert into threads values (?,?,?,?,?,0,?)',
               (SID, '/worktree/3', '', 'A desktop conversation', str(rollout), 'cli'))
    db.commit(); db.close()
    ps = f'1 0 launchd\n10 1 {bundle}/Contents/MacOS/ChatGPT\n11 10 {bundle}/Contents/Resources/codex\n'
    state = dict(ps=ps, files=f'p11\nn{rollout}\n', code=0, after=None)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['timeout'] == 2
        if state.get('error'):
            raise state['error']
        if argv[0] == 'ps':
            value = state['after'] if len(calls) > 1 and state['after'] is not None else state['ps']
        else:
            assert argv[:4] == ['lsof', '-n', '-P', '-p']
            value = state['files']
        return subprocess.CompletedProcess(argv, state['code'], value, '')
    return module, tmp_path, state, run, calls, contents


def test_live_desktop_loaded_context_then_close(desktop):
    m, home, state, run, calls, _ = desktop
    assert m['loaded_threads'](home, run) == [(SID, '/worktree/3', 'A desktop conversation')]
    state['files'] = ''
    assert m['loaded_threads'](home, run) == []  # index and saved source still exist
    state['ps'] = ''
    assert m['loaded_threads'](home, run) == []


@pytest.mark.parametrize('change', ['dead', 'reparented', 'wrong_owner', 'malformed', 'nonrollout', 'cli', 'nested', 'not_app', 'bad_plist', 'missing_plist', 'failed', 'timeout', 'missing_tool', 'bad_db'])
def test_unverified_ownership_never_marks_active(desktop, change):
    m, home, state, run, _, contents = desktop
    if change == 'dead': state['after'] = ''
    elif change == 'reparented': state['after'] = state['ps'].replace('11 10', '11 1')
    elif change == 'wrong_owner': state['files'] = state['files'].replace('p11', 'p99')
    elif change == 'malformed': state['files'] = state['files'].replace('p11', 'pbad')
    elif change == 'nonrollout': state['files'] = 'p11\nn/tmp/other.jsonl\n'
    elif change == 'cli': state['ps'] = state['ps'].replace('11 10', '11 1')
    elif change == 'nested': state['ps'] = state['ps'].replace('11 10', '11 12') + '12 10 node\n'
    elif change == 'not_app': (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'other'}))
    elif change == 'bad_plist': (contents / 'Info.plist').write_bytes(b'broken')
    elif change == 'missing_plist': (contents / 'Info.plist').unlink()
    elif change == 'failed': state['code'] = 1
    elif change == 'timeout': state['error'] = subprocess.TimeoutExpired('lsof', 2)
    elif change == 'missing_tool': state['error'] = FileNotFoundError('lsof')
    elif change == 'bad_db': (home / 'state_5.sqlite').write_bytes(b'broken')
    assert m['loaded_threads'](home, run) == []


@pytest.mark.parametrize('column,value', [('archived', 1), ('source', '{"subagent":{}}'), ('id', 'bad'), ('cwd', 'relative'), ('rollout_path', '/old/rollout-other.jsonl')])
def test_filters_unrelated_or_invalid_thread_metadata(desktop, column, value):
    m, home, _, run, _, _ = desktop
    db = sqlite3.connect(home / 'state_5.sqlite')
    db.execute(f'update threads set {column}=?', (value,)); db.commit(); db.close()
    assert m['loaded_threads'](home, run) == []


def test_process_parser_ignores_bad_rows_and_lsof_errors(desktop):
    m, home, state, run, _, _ = desktop
    state['ps'] += 'bad\nwrong 1 codex\n4 nope codex\n'
    assert len(m['processes'](run)) == 3
    def failed_lsof(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, '', '') if argv[0] == 'lsof' else run(argv, **kw)
    assert m['loaded_threads'](home, failed_lsof) == []
