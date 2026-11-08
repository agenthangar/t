"""Real SQLite stores preserve rollout completeness and fail closed on damage."""
from contextlib import closing
from pathlib import Path
import runpy
import sqlite3

import pytest

STORE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'libexec/t_codex_store.py'))
SID = '12345678-1234-1234-1234-123456789abc'


@pytest.mark.parametrize('mode,offset,error', [
    ('legacy', 99, None), ('paginated', 3, None),
    ('paginated', 4, 'behind its SQLite checkpoint'),
    ('paginated', -1, 'invalid SQLite rollout checkpoint'),
    ('paginated', None, 'invalid SQLite rollout checkpoint'),
])
def test_integrity_compares_durable_bytes_with_projection(tmp_path, mode, offset, error):
    rollout = tmp_path / 'rollout.jsonl'
    rollout.write_bytes(b'abc')
    with closing(sqlite3.connect(tmp_path / 'state_5.sqlite')) as db:
        db.execute('create table threads (id text, rollout_path text, history_mode text)')
        db.execute('insert into threads values (?, ?, ?)', (SID, rollout.name, mode))
        db.commit()
    with closing(sqlite3.connect(tmp_path / 'thread_history_1.sqlite')) as db:
        db.execute('create table thread_history_projection_state (thread_id text, next_rollout_byte_offset integer)')
        db.execute('insert into thread_history_projection_state values (?, ?)', (SID, offset))
        db.commit()
    if error:
        with pytest.raises(ValueError, match=error):
            STORE['rollout_integrity'](tmp_path, SID)
    else:
        STORE['rollout_integrity'](tmp_path, SID)
    assert rollout.read_bytes() == b'abc'


def test_legacy_schema_is_optional_but_corruption_is_not(tmp_path):
    STORE['rollout_integrity'](tmp_path, SID)
    with pytest.raises(ValueError, match='missing Codex thread'):
        STORE['rollout_integrity'](tmp_path, '')
    database = tmp_path / 'state_5.sqlite'
    with closing(sqlite3.connect(database)) as db:
        db.execute('create table threads (id text, rollout_path text)')
        db.commit()
    STORE['rollout_integrity'](tmp_path, SID)
    database.write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        STORE['rollout_integrity'](tmp_path, SID)


def test_shared_reader_cannot_write_or_create_a_store(tmp_path):
    database = tmp_path / 'state.sqlite'
    with closing(sqlite3.connect(database)) as db:
        db.execute('create table records (value text)')
        db.execute("insert into records values ('keep')")
        db.commit()
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        STORE['rows'](database, "update records set value='changed'")
    assert STORE['rows'](database, 'select value from records') == [('keep',)]
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises(sqlite3.OperationalError):
        STORE['rows'](missing, 'select 1')
    assert not missing.exists()
