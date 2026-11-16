"""Ownership metadata reuse never substitutes for current pane evidence."""
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from conftest import load_script, REPO_ROOT

SID = 'aaaaaaaa-0000-0000-0000-000000000001'
OTHER = 'aaaaaaaa-0000-0000-0000-000000000002'
START = 'Mon Sep 28 12:00:00 2026'


@pytest.fixture
def pane(tmp_path, monkeypatch):
    module = load_script(REPO_ROOT / 'libexec/t_pane_identity.py', 'pane_identity_test')
    monkeypatch.setenv('HOME', str(tmp_path))
    cwd = str(tmp_path / 'work')
    rollout = tmp_path / 'rollout.jsonl'
    rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(id=SID, cwd=cwd, source='cli'))) + '\n')
    db = tmp_path / 'state_5.sqlite'
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute('create table threads (id, rollout_path, updated_at, archived, first_user_message, cwd, name, source)')
        epoch = time.mktime(time.strptime(START, '%a %b %d %H:%M:%S %Y'))
        connection.execute('insert into threads values (?,?,?,?,?,?,?,?)',
                           (SID, str(rollout), epoch + 1, 0, 'private prompt', cwd, 'Task', 'cli'))
    values = {}
    calls = []
    def request(operation, payload, **options):
        assert operation == 'artifact'
        calls.append((payload, options))
        if 'value' in payload:
            values[payload['key']] = copy.deepcopy(payload['value'])
        return {'value': copy.deepcopy(values.get(payload['key']))}
    monkeypatch.setitem(module.CACHE, 'request', request)
    return module, db, cwd, rollout, values, calls


def resolve(pane, *, metadata=None, title='Task', footer=None):
    module, db, cwd, *_ = pane
    return module.resolve(metadata or module.Metadata(db), cwd, START, title + ' | work',
                          footer or 'GPT · ' + cwd + ' · ' + title)


def test_warm_metadata_reuses_index_and_header_but_reads_live_evidence(pane, monkeypatch):
    module, db, cwd, rollout, values, _ = pane
    metadata = module.Metadata(db)
    assert resolve(pane, metadata=metadata) == SID
    metadata.save()
    assert 'private prompt' not in json.dumps(values)
    monkeypatch.setitem(module.STORE, 'rows', lambda *a, **k: pytest.fail('warm index reopened'))
    monkeypatch.setattr('builtins.open', lambda *a, **k: pytest.fail('warm rollout reopened'))
    metadata = module.Metadata(db)
    assert resolve(pane, metadata=metadata) == SID
    assert resolve(pane, metadata=metadata, title='Changed') == ''
    assert resolve(pane, metadata=metadata, footer='GPT · /another · Task') == ''


@pytest.mark.parametrize('change', ['database', 'wal', 'replacement', 'truncate', 'parser'])
def test_changed_metadata_cannot_reuse_identity(pane, change):
    module, db, cwd, rollout, values, _ = pane
    metadata = module.Metadata(db)
    assert resolve(pane, metadata=metadata) == SID
    metadata.save()
    if change == 'database':
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute('update threads set archived=1')
    elif change == 'wal':
        connection = sqlite3.connect(db)
        connection.execute('pragma journal_mode=WAL')
        connection.execute('update threads set archived=1')
        connection.commit()
    elif change == 'replacement':
        replacement = rollout.with_suffix('.new')
        replacement.write_text(rollout.read_text().replace(SID, OTHER))
        replacement.replace(rollout)
    elif change == 'truncate':
        rollout.write_text('{}\n')
    else:
        values[metadata.key]['version'] = 0
        values[metadata.key]['headers'][str(rollout)]['payload']['id'] = OTHER
    try:
        if change == 'truncate':
            with pytest.raises(ValueError):
                resolve(pane)
        else:
            assert resolve(pane) == (SID if change == 'parser' else '')
    finally:
        if change == 'wal':
            connection.close()


@pytest.mark.parametrize('field,value', [('archived', 1), ('updated_at', 0), ('first_user_message', ''),
    ('first_user_message', '\n\t\u3000'), ('first_user_message', 3),
    ('id', 'invalid'), ('source', '{"subagent":{}}'), ('cwd', '/wrong'), ('name', 'Other')])
def test_index_guards(pane, field, value):
    _, db, *_ = pane
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute('update threads set ' + field + '=?', (value,))
    assert resolve(pane) == ''


def test_old_namesake_is_still_ambiguous(pane):
    _, db, *_ = pane
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute('insert into threads select ?,rollout_path,0,0,first_user_message,cwd,name,source from threads', (OTHER,))
    assert resolve(pane) == ''


@pytest.mark.parametrize('title,footer', [('Task | wrong', 'GPT · {cwd} · Task'),
    ('Task', 'GPT · {cwd} · Task'), ('Task | work', 'Welcome'),
    ('Task | work', 'GPT · {cwd} · Task\nGPT · {cwd}'),
    ('Task | work', 'GPT · {cwd} · Task\nGPT · {cwd} · Other')])
def test_latest_footer_and_title_must_agree(pane, title, footer):
    module, db, cwd, *_ = pane
    assert module.resolve(module.Metadata(db), cwd, START, title, footer.format(cwd=cwd)) == ''


@pytest.mark.parametrize('change', ['id', 'cwd', 'source', 'thread_source', 'bad_type', 'symlink'])
def test_rollout_guards(pane, change):
    _, _, _, rollout, *_ = pane
    row = json.loads(rollout.read_text())
    if change == 'bad_type':
        row['type'] = 'event_msg'
    elif change == 'symlink':
        target = rollout.with_suffix('.original')
        rollout.rename(target)
        rollout.symlink_to(target)
    else:
        row['payload'][change] = 'subagent' if change == 'thread_source' else 'wrong'
    if change != 'symlink':
        rollout.write_text(json.dumps(row) + '\n')
    if change in ('symlink', 'bad_type'):
        with pytest.raises(ValueError):
            resolve(pane)
    else:
        assert resolve(pane) == ''


def test_cache_outage_and_budget_fall_back_to_direct_reads(pane, monkeypatch):
    module, db, *_ = pane
    def unavailable(*a, **k):
        raise OSError('cache offline')
    metadata = module.Metadata(db, unavailable)
    assert resolve(pane, metadata=metadata) == SID
    metadata.budget = 0
    assert metadata.artifact({}) == {}
    metadata.save()


@pytest.mark.parametrize('damage', ['response', 'value', 'rows', 'header'])
def test_malformed_cache_falls_back_to_direct_metadata(pane, damage):
    module, db, _, rollout, values, _ = pane
    metadata = module.Metadata(db)
    assert resolve(pane, metadata=metadata) == SID
    metadata.save()
    if damage == 'response':
        metadata = module.Metadata(db, lambda *a, **k: [])
    else:
        if damage == 'value':
            values[metadata.key] = ['invalid']
        elif damage == 'rows':
            values[metadata.key]['rows'] = ['invalid']
        else:
            values[metadata.key]['headers'][str(rollout)] = ['invalid']
        metadata = module.Metadata(db)
    assert resolve(pane, metadata=metadata) == SID


def test_evidence_changes_during_reads_are_rejected(pane, monkeypatch):
    module, db, _, rollout, *_ = pane
    original = module.STORE['rows']
    def racing_index(*a, **k):
        result = original(*a, **k)
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute('update threads set archived=1')
        return result
    monkeypatch.setitem(module.STORE, 'rows', racing_index)
    with pytest.raises(ValueError, match='index changed'):
        resolve(pane)
    calls = 0
    real_stamp = module.stamp
    def racing_header(path):
        nonlocal calls
        value = real_stamp(path)
        calls += 1
        return value + [calls]
    monkeypatch.setattr(module, 'stamp', racing_header)
    with pytest.raises(ValueError, match='rollout changed'):
        module.Metadata(db).header(str(rollout))


def test_batch_rechecks_process_birth_and_database(pane, monkeypatch):
    module, db, cwd, *_ = pane
    entries = [dict(session='dev-repo-1', cwd=cwd, pid='42')]
    monkeypatch.setattr(module, 'observe', lambda e: ('Task | work', 'GPT · ' + cwd + ' · Task'))
    monkeypatch.setattr(module, 'process_starts', lambda e: {'42': START})
    assert module.batch(entries, module.Metadata(db)) == {'dev-repo-1': SID}
    starts = iter([{'42': START}, {'42': 'changed'}])
    monkeypatch.setattr(module, 'process_starts', lambda e: next(starts))
    with pytest.raises(ValueError, match='evidence changed'):
        module.batch(entries, module.Metadata(db))
    monkeypatch.setattr(module, 'process_starts', lambda e: {'42': START})
    metadata = module.Metadata(db)
    monkeypatch.setattr(metadata, 'stable', lambda: False)
    with pytest.raises(ValueError, match='evidence changed'):
        module.batch(entries, metadata)


def test_switch_conversation_with_same_process(pane, monkeypatch):
    module, db, cwd, rollout, *_ = pane
    other_rollout = rollout.with_name('other.jsonl')
    other_rollout.write_text(rollout.read_text().replace(SID, OTHER))
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute('insert into threads select ?,?,updated_at,archived,first_user_message,cwd,?,source from threads',
                           (OTHER, str(other_rollout), 'Second'))
    metadata = module.Metadata(db)
    assert resolve(pane, metadata=metadata) == SID
    metadata.save()
    assert resolve(pane, title='Second') == OTHER


def test_live_observation_rejects_replaced_pane(pane, monkeypatch):
    module = pane[0]
    output = iter(['$1|%2|42|Task | work', 'screen', '$1|%3|42|Task | work'])
    monkeypatch.setattr(module, 'command', lambda a: next(output))
    with pytest.raises(ValueError, match='pane changed'):
        module.observe(dict(session='dev-repo-1'))
    output = iter(['$1|%2|42|Task | work', 'screen', '$1|%2|42|Task | work'])
    assert module.observe(dict(session='dev-repo-1')) == ('Task | work', 'screen')
    monkeypatch.setattr(module, 'command', lambda a: 'invalid')
    with pytest.raises(ValueError, match='invalid pane'):
        module.observe(dict(session='dev-repo-1'))


def test_process_observation_is_batched(pane, monkeypatch):
    module = pane[0]
    seen = []
    monkeypatch.setattr(module, 'command', lambda a: seen.append(a) or ' 42 ' + START + '\n 43 ' + START)
    assert module.process_starts([]) == {}
    assert module.process_starts([dict(pid=42), dict(pid=43)]) == {'42': START, '43': START}
    assert len(seen) == 1 and seen[0][2] == '42,43'


def test_command_preserves_failure(pane, monkeypatch):
    module = pane[0]
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, 'ok\n'))
    assert module.command(['tmux']) == 'ok'
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1, ''))
    with pytest.raises(ValueError, match='observation failed'):
        module.command(['tmux'])
