"""Structured work evidence and the upgrade worker's durable boundary."""
import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
STAMP = '2026-05-01T12:00:00Z'


@pytest.fixture
def migration():
    return runpy.run_path(str(ROOT / 'libexec/t_data_migrations.py'))


@pytest.fixture
def history_api():
    return runpy.run_path(str(ROOT / 'libexec/t_history.py'))


def codex(kind, **payload):
    return dict(type=kind, timestamp=STAMP, payload=payload)


def codex_plan(sid='saved', status='completed', *, receipt='Plan updated'):
    return [
        codex('session_meta', id=sid),
        codex('event_msg', type='task_started', turn_id='turn'),
        codex('response_item', type='function_call', call_id='plan', name='update_plan',
              arguments=json.dumps(dict(plan=[dict(step='Finish work', status=status)]))),
        codex('response_item', type='function_call_output', call_id='plan', output=receipt),
        codex('event_msg', type='task_complete', turn_id='turn'),
    ]


def as_lines(rows):
    return ''.join(json.dumps(row) + '\n' for row in rows)


@pytest.mark.parametrize(('status', 'extra', 'receipt', 'expected'), [
    ('completed', [], 'Plan updated', 'complete'),
    ('in_progress', [], 'Plan updated', 'unfinished'),
    ('pending', [], 'Plan updated', 'unfinished'),
    ('done', [], 'Plan updated', None),
    ('completed', [], 'Plan rejected', None),
    ('completed', [codex('event_msg', type='task_started')], 'Plan updated', None),
    ('completed', [codex('event_msg', type='user_message')], 'Plan updated', None),
])
def test_codex_requires_a_successful_plan_for_latest_task(migration, history_api, status, extra, receipt, expected):
    rows = codex_plan(status=status, receipt=receipt) + extra
    result = migration['_report'](io.BytesIO(as_lines(rows).encode()), 'codex', 'saved', history_api)
    assert result == ((dict(state=expected, at=history_api['timestamp'](STAMP)), 'plan') if expected else None)


@pytest.mark.parametrize('wrong_parent', [False, True])
def test_claude_requires_exact_todo_result(migration, history_api, wrong_parent):
    def entry(kind, content, **metadata):
        return dict(type=kind, timestamp=STAMP, sessionId='saved', message=dict(content=content), **metadata)
    rows = [
        entry('user', 'Build it'),
        entry('assistant', [dict(type='tool_use', id='todo', name='TodoWrite',
                                 input=dict(todos=[dict(content='Build it', status='pending')]))], uuid='call'),
        entry('user', [dict(type='tool_result', tool_use_id='todo',
                            content='Todos have been modified successfully.')],
              parentUuid='unrelated' if wrong_parent else 'call'),
    ]
    result = migration['_report'](io.BytesIO(as_lines(rows).encode()), 'claude', 'saved', history_api)
    assert result == (None if wrong_parent else (dict(state='unfinished', at=history_api['timestamp'](STAMP)), 'plan'))


def test_plain_outcomes_and_turn_completion_supply_no_status(migration, history_api):
    rows = [codex('session_meta', id='saved'), codex('event_msg', type='task_started'),
            codex('response_item', type='message', role='assistant', content='All work complete'),
            codex('event_msg', type='task_complete')]
    assert migration['_report'](io.BytesIO(as_lines(rows).encode()), 'codex', 'saved', history_api) is None


@pytest.mark.parametrize(('sid', 'code', 'expected'), [('saved', 0, 'complete'), ('other', 0, None), ('saved', 1, None)])
def test_explicit_signal_requires_exact_identity_and_success(migration, history_api, sid, code, expected):
    command = f"python3 /t/libexec/t_session_appearance.py signal /tmp/tmux %1 {sid} 'owner start' complete"
    rows = [
        codex('session_meta', id='saved'),
        codex('event_msg', type='task_started'),
        codex('response_item', type='function_call', call_id='signal', name='exec_command',
              arguments=json.dumps(dict(cmd=command))),
        codex('response_item', type='function_call_output', call_id='signal', output=f'Exit code: {code}'),
    ]
    result = migration['_report'](io.BytesIO(as_lines(rows).encode()), 'codex', 'saved', history_api)
    assert result == ((dict(state=expected, at=history_api['timestamp'](STAMP)), 'signal') if expected else None)


def add_source(store, native, sid, *, status='completed', content=None):
    path = native / f'rollout-{sid}.jsonl'
    path.write_text(as_lines(content if content is not None else codex_plan(sid, status)))
    record = dict(repo='org/repo', agent='codex', sid=sid,
                  associations=[dict(workspace='/repo/4', host='local', slot='4')],
                  sources=[dict(path=str(path), host='local', updated=path.stat().st_mtime)])
    store.merge(record)
    return path, record


def test_commits_batches_preserves_reports_and_retries(migration, history_api, tmp_path, monkeypatch):
    native = tmp_path / '.codex/archived_sessions'
    native.mkdir(parents=True)
    monkeypatch.setenv('CODEX_HOME', str(native.parent))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    store = history_api['Store']()
    try:
        store.db.execute('create table data_migrations (migration text, key text, provenance text, primary key(migration,key))')
        add_source(store, native, 'complete')
        add_source(store, native, 'manual', status='pending')
        add_source(store, native, 'wrong', content=codex_plan('another'))
        store.report_work('codex', 'manual', 'complete')
        chosen = migration['MIGRATIONS'][0]
        def interrupted(db, key, row, result):
            if row['sid'] == 'wrong':
                raise OSError('interrupted batch')
            return chosen.write(db, key, row, result)
        incomplete = migration['Migration'](chosen.id, chosen.candidates, chosen.inspect, interrupted, chosen.projection)
        with pytest.raises(OSError, match='interrupted batch'):
            migration['_batch'](store.db, incomplete, list(chosen.candidates(store)), history_api, final=False)
        assert not store.db.execute('select 1 from data_migrations').fetchone()
        assert dict(store.db.execute('select sid,state from work_reports')) == {'manual': 'complete'}
        monkeypatch.setitem(migration['_pass'].__globals__, 'BATCH_SIZE', 1)
        migration['_pass'](store, chosen, history_api, {}, lambda _: None)
        changes = dict(store.db.execute('select key,provenance from data_migrations'))
        assert len(changes) == 2 and sorted(changes.values()) == ['', 'plan']
        assert dict(store.db.execute('select sid,state from work_reports')) == {'complete': 'complete', 'manual': 'complete'}
        migration['_pass'](store, chosen, history_api, {}, lambda _: pytest.fail('repeated work'))
        assert dict(store.db.execute('select key,provenance from data_migrations')) == changes
    finally:
        store.close()


def test_changed_source_and_live_report_win_at_commit(migration, history_api, tmp_path, monkeypatch):
    native = tmp_path / '.codex/sessions'
    native.mkdir(parents=True)
    monkeypatch.setenv('CODEX_HOME', str(native.parent))
    store = history_api['Store'](tmp_path / 'history')
    try:
        store.db.execute('create table data_migrations (migration text, key text, provenance text, primary key(migration,key))')
        changed, _ = add_source(store, native, 'changed')
        add_source(store, native, 'live')
        rows = list(migration['_work_candidates'](store))
        original = migration['MIGRATIONS'][0]
        def inspect(row, history):
            result = original.inspect(row, history)
            if row['sid'] == 'changed':
                with changed.open('a') as stream:
                    stream.write(as_lines([codex('event_msg', type='task_started')]))
            else:
                store.report_work('codex', 'live', 'unfinished')
            return result
        batch = migration['Migration'](original.id, original.candidates, inspect, original.write, original.projection)
        assert not migration['_batch'](store.db, batch, rows, history_api, final=False)
        assert dict(store.db.execute('select sid,state from work_reports')) == {'live': 'unfinished'}
        assert len(list(store.db.execute('select key from data_migrations'))) == 1
    finally:
        store.close()


def test_upgrade_worker_election_and_completion(migration, history_api, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    for name in ('XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR'):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / '.claude'))
    migration_root = migration['directory']()
    migration_root.mkdir(parents=True)
    config = tmp_path / 'local.zsh'
    config.touch()
    native = tmp_path / '.codex/sessions'
    native.mkdir(parents=True)
    store = history_api['Store']()
    try:
        add_source(store, native, 'saved')
        with (migration_root / 'worker.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            migration['queue'](ROOT, config)
            assert not store.db.execute("select 1 from sqlite_master where name='data_migrations'").fetchone()
        migration['queue'](ROOT, config)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if store.db.execute("select 1 from sqlite_master where name='data_migrations'").fetchone():
                if store.db.execute("select 1 from data_migrations where key=''").fetchone():
                    break
            time.sleep(.05)
        else:
            pytest.fail('detached migration did not finish')
        assert list(store.db.execute('select sid,state from work_reports')) == [('saved', 'complete')]
    finally:
        store.close()
