"""Seed one saved conversation in a disposable test container."""
import json
from pathlib import Path
import sqlite3
import sys

workspace, sid = sys.argv[1:]
root = Path.home() / '.codex'
path = root / 'sessions/2026/11/07' / ('rollout-2026-11-07T00-00-00-' + sid + '.jsonl')
path.parent.mkdir(parents=True, exist_ok=True)
payload = dict(id=sid, cwd=workspace, source='cli', cli_version='0.160.0')
timestamp = '2026-11-07T00:00:00Z'
records = [
    dict(type='session_meta', payload=payload),
    dict(type='turn_context', timestamp=timestamp, payload=dict(model='test-model')),
    dict(type='event_msg', timestamp=timestamp, payload=dict(type='task_started')),
    dict(type='event_msg', timestamp=timestamp, payload=dict(type='user_message', message='SSH test')),
]
path.write_text(''.join(json.dumps(record) + '\n' for record in records))
with sqlite3.connect(root / 'state_5.sqlite') as database:
    database.execute('create table threads (id text, rollout_path text, cwd text, name text, '
                     'title text, updated_at integer, archived integer, source text, first_user_message text, history_mode text)')
    database.execute('insert into threads values (?,?,?,?,?,?,?,?,?,?)',
                     (sid, str(path), workspace, 'SSH test', 'SSH test', 1, 0, 'cli', 'Move this work', 'legacy'))
