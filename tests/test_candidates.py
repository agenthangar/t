"""Saved discovery cache reuse and invalidation, without personal agent stores."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest

from test_agent_seam import zsh  # noqa: F401


@pytest.fixture
def candidates():
    spec = importlib.util.spec_from_file_location('t_candidates', Path(__file__).parents[1] / 'libexec/t_candidates.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(tmp_path):
    home, cache = tmp_path / 'codex', tmp_path / 'cache'
    day = home / 'sessions/2026/10/24'
    day.mkdir(parents=True)
    return home, cache, day


def rollout(path, cwd='/work', **extra):
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': cwd, **extra}}) + '\n')
    return str(path)


def database(home):
    connection = sqlite3.connect(home / 'state_5.sqlite')
    connection.execute('create table threads (cwd, rollout_path, archived, source, updated_at)')
    connection.commit()
    return connection


def test_unchanged_candidates_skip_reads_and_writes(candidates, store, monkeypatch):
    home, cache, day = store
    path = rollout(day / 'rollout-one.jsonl')
    with database(home) as connection:
        connection.execute('insert into threads values (?, ?, 0, ?, 1)', ('/work', path, 'cli'))
    assert candidates.discover(home, cache) == [('/work', path)]
    def unexpected(*args):
        pytest.fail('unchanged sources were reread or rewritten')
    monkeypatch.setattr(candidates, 'rollout_metadata', unexpected)
    monkeypatch.setattr(candidates, 'index_rows', unexpected)
    monkeypatch.setattr(candidates, 'save', unexpected)
    assert candidates.discover(home, cache) == [('/work', path)]


def test_source_changes_and_home_isolation(candidates, store):
    home, cache, day = store
    path = day / 'rollout-one.jsonl'
    rollout(path)
    assert candidates.discover(home, cache) == [('/work', str(path))]
    # Same inode, same size, and restored mtime: ctime still invalidates.
    before = path.stat()
    rollout(path, '/next')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert candidates.discover(home, cache) == [('/next', str(path))]
    # Incomplete first write must not pin an empty cwd forever.
    path.write_text('{')
    assert candidates.discover(home, cache) == []
    rollout(path, '/done')
    second = day / 'rollout-two.jsonl'
    rollout(second, '/new')
    assert set(candidates.discover(home, cache)) == {('/done', str(path)), ('/new', str(second))}
    path.unlink()
    replacement = day / 'replacement'
    rollout(replacement, '/swap')
    replacement.replace(second)
    assert candidates.discover(home, cache) == [('/swap', str(second))]
    second.unlink()
    assert candidates.discover(home, cache) == []
    other = home.parent / 'other'
    assert candidates.discover(other, cache) == []
    assert len(list((cache / 'claude-sessions').glob('candidates-*.json'))) == 2


def test_database_wal_and_missing_paths(candidates, store):
    home, cache, day = store
    path = rollout(day / 'rollout-one.jsonl', '/original')
    connection = database(home)
    connection.execute('pragma journal_mode=wal')
    connection.execute('insert into threads values (?, ?, 0, ?, 1)', ('/one', path, 'cli'))
    connection.commit()
    assert candidates.discover(home, cache) == [('/one', path), ('/original', path)]
    before = candidates.stamp(home / 'state_5.sqlite')
    connection.execute("update threads set cwd='/two'")
    connection.commit()
    assert candidates.stamp(home / 'state_5.sqlite') == before
    assert candidates.discover(home, cache) == [('/two', path), ('/original', path)]
    connection.execute('update threads set archived=1')
    connection.commit()
    assert candidates.discover(home, cache) == [('/original', path)]
    connection.close()
    Path(path).unlink()
    assert candidates.discover(home, cache) == []


@pytest.mark.parametrize('content', ['{', '[]', '{"version":0}', '{"version":1,"rollouts":[],"index":[]}'])
def test_corrupt_cache_is_disposable(candidates, store, content):
    home, cache, day = store
    path = rollout(day / 'rollout-one.jsonl')
    candidates.discover(home, cache)
    file, = (cache / 'claude-sessions').glob('candidates-*.json')
    file.write_text(content)
    assert candidates.discover(home, cache) == [('/work', path)]


def test_rollout_only_subagents_malformed_and_unwritable(candidates, store):
    home, cache, day = store
    path = rollout(day / 'rollout-good.jsonl')
    rollout(day / 'rollout-sub.jsonl', source={'subagent': {}})
    rollout(day / 'rollout-sub2.jsonl', thread_source='subagent')
    (day / 'rollout-bad.jsonl').write_text('[]')
    (day / 'rollout-nonmeta.jsonl').write_text('{"type":"event_msg"}')
    rollout(day / 'rollout-noncwd.jsonl', cwd=123)
    with database(home) as connection:
        connection.execute('insert into threads values (?, ?, 0, ?, 1)', ('/moved', path, 'cli'))
    candidates.discover(home, cache)
    assert candidates.discover(home, cache, rollouts_only=True) == [('/work', path)]
    blocked = cache / 'blocked'
    blocked.write_text('file')
    assert candidates.discover(home, blocked) == [('/moved', path), ('/work', path)]


def test_failed_index_retried_and_changed_read_not_cached(candidates, store, monkeypatch):
    home, cache, day = store
    path = Path(rollout(day / 'rollout-one.jsonl'))
    original = candidates.rollout_metadata
    def changing(file):
        result = original(file)
        with open(file, 'a') as stream:
            stream.write('\n')
        return result
    monkeypatch.setattr(candidates, 'rollout_metadata', changing)
    assert candidates.discover(home, cache) == []
    monkeypatch.setattr(candidates, 'rollout_metadata', original)
    with database(home) as connection:
        connection.execute('insert into threads values (?, ?, 0, ?, 1)', ('/indexed', str(path), 'cli'))
    assert candidates.discover(home, cache) == [('/indexed', str(path)), ('/work', str(path))]


def test_snapshot_reused_only_within_command_and_progress_is_terminal_only(zsh):
    snippet = '''
_codex_candidate_rows() { print -r -- $'/work\\t/missing'; }
local -A _T_CODEX_CANDIDATES
local _T_CODEX_CANDIDATES_READY=0
_codex_candidate_snapshot
python3() { print -u2 'unexpected scan'; return 1; }
_dev_agent_transcripts_for_cwd codex /work
_T_PROGRESS=1
_t_progress 'Finding saved conversations'
'''
    result = zsh(snippet)
    assert result.stdout == result.stderr == ''
    terminal = zsh(snippet, _tty=True)
    assert 'Finding saved conversations' in terminal.stdout
    assert 'unexpected scan' not in terminal.stdout
    silent = zsh('_T_PROGRESS=0; _t_progress hidden', _tty=True)
    assert 'hidden' not in silent.stdout


@pytest.mark.parametrize('stdout_tty,stderr_tty,machine,visible', [
    (True, True, False, True), (False, True, False, False),
    (True, False, False, False), (True, True, True, False)])
def test_python_progress_keeps_machine_and_redirected_output_clean(t_mod, monkeypatch, capsys,
                                                                  stdout_tty, stderr_tty, machine, visible):
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: stdout_tty)
    monkeypatch.setattr(t_mod.sys.stderr, 'isatty', lambda: stderr_tty)
    t_mod._progress('Checking sessions', machine=machine)
    output = capsys.readouterr()
    assert output.out == ''
    assert ('Checking sessions' in output.err) == visible


def test_resume_takes_one_snapshot_and_fresh_owners_each_time(zsh, tmp_path):
    from test_resume_multiselect import FZF_ALL
    fzf = tmp_path / 'stubbin/fzf'
    fzf.write_text(FZF_ALL)
    fzf.chmod(0o755)
    home = zsh.home
    day = home / '.codex/sessions/2026/10/24'
    day.mkdir(parents=True)
    for slot in (3, 4):
        wt = home / f'code/.worktrees/api/{slot}'
        wt.mkdir(parents=True)
        file = day / f'rollout-2026-10-24T00-00-00-aaaaaaaa-0000-0000-0000-{slot:012}.jsonl'
        rollout(file, str(wt))
        with file.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': f'Task {slot}'}]}}, separators=(',', ':')) + '\n')
    calls = tmp_path / 'calls'
    snippet = f'''
_codex_candidate_rows() {{
  print snapshot >> {calls}
  python3 "$T_HOME/libexec/t_candidates.py" candidates
}}
_dev_session_rows() {{ print owners >> {calls}; }}
_t_resume api --days all; print rc=$?
_t_resume api --days all; print rc=$?
'''
    result = zsh(snippet, _tty=True, FZF_LOG=str(tmp_path / 'fzf.log'), PICK_CANCEL='1')
    assert result.stdout.count('rc=130') == 2, result.stdout
    assert calls.read_text().splitlines().count('snapshot') == 2
    assert calls.read_text().splitlines().count('owners') == 2
    assert 'Finding saved conversations' in result.stdout
    assert 'Loading metadata for 2 saved conversation(s)' in result.stdout
    rows = (tmp_path / 'fzf.log').read_text()
    assert 'Task 3' in rows and 'Task 4' in rows


def test_unavailable_index_retries_without_poisoning_cache(candidates, store, monkeypatch):
    home, cache, day = store
    path = rollout(day / 'rollout-one.jsonl')
    with database(home) as connection:
        connection.execute('insert into threads values (?, ?, 0, ?, 1)', ('/indexed', path, 'cli'))
        connection.execute('insert into threads values (null, null, 0, ?, 1)', ('cli',))
    original = candidates.index_rows
    calls = []
    def unavailable(db):
        calls.append(db)
        return None
    monkeypatch.setattr(candidates, 'index_rows', unavailable)
    assert candidates.discover(home, cache) == [('/work', path)]
    assert candidates.discover(home, cache) == [('/work', path)]
    assert len(calls) == 2
    monkeypatch.setattr(candidates, 'index_rows', original)
    assert candidates.discover(home, cache) == [('/indexed', path), ('/work', path)]
    (home / 'state_5.sqlite').write_text('not sqlite')
    assert candidates.discover(home, cache) == [('/work', path)]


def test_disappearing_files_and_failed_cache_replace(candidates, store, monkeypatch):
    home, cache, day = store
    path = rollout(day / 'rollout-one.jsonl')
    assert candidates.rollout_metadata(home / 'missing') == ['', False]
    original = candidates.stamp
    monkeypatch.setattr(candidates, 'stamp', lambda file: None if str(file) == path else original(file))
    assert candidates.discover(home, cache) == []
    monkeypatch.setattr(candidates, 'stamp', original)
    monkeypatch.setattr(candidates.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('unwritable')))
    assert candidates.discover(home, cache) == [('/work', path)]
    assert len(list((cache / 'claude-sessions').iterdir())) == 1
