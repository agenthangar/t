"""Resumable, local data migrations dispatched by the installed updater."""
from contextlib import closing
from collections import deque
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import stat
import subprocess
import sys
import tempfile
import time


BATCH_SIZE = 16
MAX_LINE = 16 * 1024 * 1024
RECENT_SECONDS = 30 * 86400


def directory():
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    return state / 't/data-migrations'


def queue(source, config):
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(root / 'worker.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600), 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('COV_CORE_') and key not in ('T_DEV_CONTEXT', 'T_DEV_CONTEXT_OWNER')}
        env.update(T_LOCAL_RC=str(Path(config).absolute()), T_NO_AUTORELOAD='1', T_NO_UPDATE_CHECK='1')
        subprocess.Popen([sys.executable, str(Path(source) / 'bin/t'), '__data-migrate', str(lock.fileno())],
                         cwd='/', env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock.fileno(),))


def prioritize(aliases, slot):
    root = directory()
    if not (root / 'worker.lock').is_file():
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(dict(aliases=aliases, slot=slot, at=time.time()), stream)
        temporary.replace(root / 'priority.json')
    except OSError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def revision():
    try:
        return (directory() / 'revision').stat().st_mtime_ns
    except OSError:
        return 0


def _priority():
    try:
        data = json.loads((directory() / 'priority.json').read_text())
        if 0 <= time.time() - data['at'] < 300:
            return set(data['aliases']) - {'--all'}, data['slot']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return set(), None


def _plan(args, *, claude):
    rows = args.get('todos' if claude else 'plan')
    if not isinstance(rows, list) or not rows:
        return None
    done = 'completed'
    statuses = ('pending', 'in_progress', done)
    label = 'content' if claude else 'step'
    if all(isinstance(row, dict) and row.get('status') in statuses
           and isinstance(row.get(label), str) and row[label].strip() for row in rows):
        return 'complete' if all(row['status'] == done for row in rows) else 'unfinished'
    return None


def _signal_state(args, sid):
    command = args.get('cmd') or args.get('command')
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if (len(words) not in (8, 10) or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(words[0]).name)
            or Path(words[1]).parts[-2:] != ('libexec', 't_session_appearance.py')
            or words[2] != 'signal' or not re.fullmatch(r'%[0-9]+', words[4]) or words[5] != sid):
        return None
    event = words[7]
    if len(words) == 10:
        return 'unfinished' if event == 'enter' and words[8] == 'workflow' else None
    return {'complete': 'complete', 'resume': 'unfinished', 'waiting': 'unfinished'}.get(event)


def _receipt(call, kind, claude, history):
    if call.get('is_error'):
        return False
    output = call.get('content' if claude else 'output')
    if isinstance(output, list):
        output = '\n'.join(block.get('text', '') for block in output
                           if isinstance(block, dict) and isinstance(block.get('text'), str))
    if not isinstance(output, str):
        return False
    if kind == 'signal':
        _, statuses, running = history['process_output'](output)
        return statuses == [0] and not running
    if claude:
        return output.strip().startswith('Todos have been modified successfully.')
    return output.strip() in ('Plan updated', 'Plan updated.')


def _call_state(call, sid, claude):
    args = call.get('input' if claude else 'arguments')
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        return None, None
    if not isinstance(args, dict):
        return None, None
    name = call.get('name')
    if name == ('TodoWrite' if claude else 'update_plan'):
        return 'plan', _plan(args, claude=claude)
    if name == ('Bash' if claude else 'exec_command'):
        state = _signal_state(args, sid)
        if state:
            return 'signal', state
    return None, None


def _codex_event(row):
    body = row.get('payload')
    if not isinstance(body, dict):
        return False, (), None
    kind = row.get('type')
    if kind == 'session_meta':
        source = body.get('source')
        if body.get('thread_source') == 'subagent' or isinstance(source, dict) and 'subagent' in source:
            return False, (), ''
        return False, (), body.get('id', '')
    if kind == 'event_msg':
        return body.get('type') in ('task_started', 'user_message'), (), None
    # Older rollouts can have a user message without an event_msg.
    if kind == 'response_item':
        reset = body.get('type') == 'message' and body.get('role') == 'user'
        return reset, (body,), None
    return False, (), None


def _claude_event(row):
    if any(row.get(key) for key in ('isSidechain', 'isMeta', 'isCompactSummary')):
        return False, (), None
    kind = row.get('type')
    if kind not in ('user', 'assistant'):
        return False, (), None
    message = row.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    reset = kind == 'user' and not any(block.get('type') == 'tool_result' for block in blocks)
    return reset, blocks, row.get('sessionId', '')


def _report(stream, agent, sid, history):
    claude = agent == 'claude'
    event = _claude_event if claude else _codex_event
    pending, latest, report, verified, task = {}, None, None, False, False
    seen = set()
    while raw := stream.readline(MAX_LINE + 1):
        if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
            return None
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(row, dict):
            return None
        reset, calls, identity = event(row)
        if identity is not None:
            if identity != sid:
                return None
            verified = True
        if reset:
            pending.clear()
            seen.clear()
            latest, report, task = None, None, True
        if not (task and verified):
            continue
        for call in calls:
            kind = call.get('type')
            if kind == ('tool_use' if claude else 'function_call'):
                call_id = call.get('id' if claude else 'call_id')
                tool, state = _call_state(call, sid, claude)
                if not tool or not isinstance(call_id, str) or not call_id:
                    continue
                pending[call_id] = (tool, state, row.get('uuid')) if call_id not in seen else (tool, None, None)
                seen.add(call_id)
                latest, report = call_id, None
            elif kind == ('tool_result' if claude else 'function_call_output'):
                call_id = call.get('tool_use_id' if claude else 'call_id')
                if not isinstance(call_id, str) or call_id not in pending:
                    continue
                tool, state, parent = pending.pop(call_id)
                if call_id != latest:
                    continue
                at = history['timestamp'](row.get('timestamp'))
                if (state and 0 < at <= time.time() and _receipt(call, tool, claude, history)
                        and (not claude or parent and row.get('parentUuid') == parent)):
                    report = dict(state=state, at=at), tool
                else:
                    report = None
    return report


def _stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _native_source(row):
    local = [source for source in row.get('sources', []) if source['host'] == 'local']
    if not local or not any(a.get('host') == 'local' for a in row.get('associations', [])):
        return None
    updated = max(source.get('updated', 0) for source in local)
    paths = {source['path'] for source in local if source.get('updated', 0) == updated}
    if len(paths) != 1:
        return None
    path = Path(paths.pop())
    codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').resolve()
    roots = (codex / 'sessions', codex / 'archived_sessions') if row['agent'] == 'codex' else (claude / 'projects',)
    if path.suffix != '.jsonl' or path.is_symlink():
        return None
    path = path.resolve()
    return path if any(path.is_relative_to(root) for root in roots) else None


def _work_status(row, history):
    try:
        path = _native_source(row)
        if path is None:
            return None, None
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None, None
            stamp = _stamp(info)
            report = _report(stream, row['agent'], row['sid'], history)
        if _stamp(path.stat()) != stamp:
            return None, None
        return (path, stamp), report
    except (OSError, RuntimeError):
        return None, None


def _save_work_status(db, key, row, inspected):
    source, report = inspected
    if source is None:
        return None
    current = db.execute('select record from conversations where key=?', (key,)).fetchone()
    if not current:
        return None
    current = json.loads(current[0])
    if current.get('work_report'):
        return ''
    path, stamp = source
    try:
        stable = _stamp(path.stat()) == stamp and _native_source(current) == path
    except (OSError, RuntimeError):
        return None
    if not stable:
        return None
    if report is None:
        return ''
    value, source = report
    cursor = db.execute('insert into work_reports values (?,?,?,?) on conflict(agent,sid) do nothing',
                        (row['agent'], row['sid'], value['state'], value['at']))
    return source if cursor.rowcount else ''


@dataclass(frozen=True)
class Migration:
    id: str
    candidates: object
    inspect: object
    write: object
    projection: str


def _work_candidates(store):
    rows = store.records()
    identities = {}
    for row in rows:
        identity = (row['agent'], row['sid'])
        identities[identity] = identities.get(identity, 0) + 1
    for row in rows:
        identity = (row['agent'], row['sid'])
        if not row.get('work_report') and identities[identity] == 1:
            yield json.dumps([row['repo'], *identity]), row


MIGRATIONS = (Migration('work-reports-v1', _work_candidates, _work_status, _save_work_status, 'catalog-changed'),)


def _batch(db, migration, rows, history, *, final):
    inspected = [(key, row, migration.inspect(row, history)) for key, row in rows]
    changed = False
    with db:
        db.execute('begin immediate')
        for key, row, result in inspected:
            provenance = migration.write(db, key, row, result)
            if provenance is not None or final:
                db.execute('insert into data_migrations values (?,?,?) on conflict do nothing',
                           (migration.id, key, provenance or ''))
                changed |= bool(provenance)
    return changed


def _pass(store, migration, history, aliases, publish, *, final=False):
    checked = {key for key, in store.db.execute('select key from data_migrations where migration=?', (migration.id,))}
    pending = {key: row for key, row in migration.candidates(store) if key not in checked}
    cutoff = time.time() - RECENT_SECONDS
    priority, ordered = None, deque()
    while pending:
        requested = _priority()
        if requested != priority:
            priority = requested
            selected, slot = priority
            repos = selected | {aliases.get(alias, alias) for alias in selected}
            def order(key):
                row = pending[key]
                scoped = (not repos or row.get('repo') in repos) and (not slot or any(
                    a.get('host') == 'local' and a.get('slot') == slot for a in row.get('associations', [])))
                at = row.get('updated', 0)
                return not scoped, at < cutoff, -at, key
            ordered = deque(sorted(pending, key=order))
        keys = [ordered.popleft() for _ in range(min(BATCH_SIZE, len(ordered)))]
        if _batch(store.db, migration, [(key, pending.pop(key)) for key in keys], history, final=final):
            publish(migration.projection)


def run(api, fd):
    source = Path(api['_t_source_root']())
    with os.fdopen(fd, 'r+') as lock:
        expected = (directory() / 'worker.lock').stat()
        actual = os.fstat(lock.fileno())
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            return 2
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        history = runpy.run_path(str(source / 'libexec/t_history.py'))
        cache = api['_session_discovery']()
        with closing(history['Store']()) as store:
            store.db.execute('create table if not exists data_migrations '
                             '(migration text, key text, provenance text, primary key (migration, key))')
            pending = [migration for migration in MIGRATIONS if not store.db.execute(
                "select 1 from data_migrations where migration=? and key=''", (migration.id,)).fetchone()]
            if not pending:
                return 0
            app = history['HistoryCommand'](api['Config'](), api, store=store)
            aliases = {alias: app.canonical_identity(app.identity(alias)) for alias in app.cfg.repos}
            def publish(operation):
                # Other tmux and development cache namespaces share the history DB.
                (directory() / 'revision').touch(mode=0o600)
                cache['request'](operation, {'refresh_active': True}, deadline=time.monotonic() + 1)
            for migration in pending:
                # Repair notification if a previous worker committed then exited.
                if store.db.execute("select 1 from data_migrations where migration=? and provenance<>'' limit 1",
                                    (migration.id,)).fetchone():
                    publish(migration.projection)
            # Resolve newly archived or relocated sources before accepting older evidence.
            app.incremental_catalog()
            for migration in pending:
                _pass(store, migration, history, aliases, publish)
                _pass(store, migration, history, aliases, publish, final=True)
                with store.db:
                    store.db.execute("insert into data_migrations values (?,'','')", (migration.id,))
    return 0
