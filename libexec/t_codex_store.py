"""Codex index reads and durable rollout validation shared by lifecycle callers."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys


def rows(path, query, parameters=(), *, initialize_sidecars=True, timeout=.5, row_factory=None):
    """Never create a database or modify records; preserve normal WAL locking.

    Action preflight can initialize missing sidecars. Metadata-only inspection
    opts out, reporting an unavailable index rather than repairing it.
    """
    uri = Path(path).resolve().as_uri()
    def read(mode):
        with closing(sqlite3.connect(uri + '?mode=' + mode, uri=True, timeout=timeout)) as db:
            db.execute('PRAGMA query_only=ON')
            if row_factory is not None:
                db.row_factory = row_factory
            return db.execute(query, parameters).fetchall()
    try:
        return read('ro')
    except sqlite3.OperationalError as error:
        code = getattr(error, 'sqlite_errorcode', None)
        cantopen = (code is not None and code & 255 == sqlite3.SQLITE_CANTOPEN
                    or code is None and str(error) == 'unable to open database file')
        if not initialize_sidecars or not cantopen:
            raise
    return read('rw')


def rollout_integrity(home, sid):
    """A legacy schema is optional; an unreadable store is never missing history."""
    home = Path(home)
    if not sid:
        raise ValueError('missing Codex thread id for rollout check')

    def one(name, query):
        path = home / name
        if not path.is_file():
            return None
        try:
            result = rows(path, query, (sid,))
            return result[0] if result else None
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if 'no such table' in message or (name == 'state_5.sqlite' and
                                              'no such column: history_mode' in message):
                return None
            raise

    thread = one('state_5.sqlite', 'select rollout_path, history_mode from threads where id=?')
    if thread is None or thread[1] != 'paginated':
        return
    checkpoint = one('thread_history_1.sqlite',
                     'select next_rollout_byte_offset from thread_history_projection_state where thread_id=?')
    if checkpoint is None:
        return
    offset = checkpoint[0]
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid SQLite rollout checkpoint')
    if not thread[0]:
        raise ValueError('missing paginated rollout path')
    rollout = Path(thread[0])
    if not rollout.is_absolute():
        rollout = home / rollout
    size = rollout.stat().st_size
    if size < offset:
        raise ValueError(f'Codex rollout is {size} bytes, behind its SQLite checkpoint at {offset}; '
                         'keep the source here and restore the matching rollout before moving')


def main(argv):
    try:
        if len(argv) != 3 or argv[0] != 'integrity':
            raise ValueError('expected integrity HOME THREAD')
        rollout_integrity(argv[1], argv[2])
        return 0
    except (OSError, TypeError, ValueError, sqlite3.Error) as error:
        print(f'tbeam: could not verify paginated Codex rollout: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
