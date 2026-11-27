"""Close one focused session and update only the live picker's cached snapshot."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import threading
import urllib.request

from t_picker_filters import render
from t_progress import spinner as _spinner
from t_session_details import clip_columns

KEYS = ('ctrl-n', 'enter', 'ctrl-k', 'space', 'tab', 'ctrl-a', 'ctrl-f', 'ctrl-l', 'ctrl-x', 'ctrl-t', 'ctrl-g', 'ctrl-d', 'ctrl-o', 'ctrl-r',
        'ctrl-u', 'ctrl-p', 'ctrl-b', 'ctrl-v', 'ctrl-e', 'ctrl-w', 'ctrl-s', 'ctrl-y', 'alt-s', 'alt-t', 'alt-r', 'alt-g', 'alt-o', 'alt-v')
HEADER = 'transform-header(cat "$T_PICKER_CURRENT_HEADER")'


def identity(row):
    fields = row.split('\t')
    return tuple(fields[:2]) + tuple(fields[5:7])


def prune(directory, row):
    """Remove one exact displayed identity from every view, never rediscover."""
    target = identity(row)
    for name in ('all', 'default', 'current', 'rows-0', 'rows-1'):
        path = directory / name
        if path.exists():
            path.write_text(''.join(line + '\n' for line in path.read_text().splitlines()
                                    if identity(line) != target))
    source = directory / 'source'
    if source.exists():
        source.write_text(''.join(line + '\n' for line in source.read_text().splitlines()
                                 if tuple(line.split('\t')[i] for i in (0, 3)) != target[:2]))
        render(directory, 'open', 'current', int(os.environ['T_PICKER_HEADER_WIDTH']),
               os.environ.get('T_PICKER_COLUMNS', ''))


def status(directory, message):
    path = directory / 'current-header'
    lines = path.read_text().splitlines()
    if lines and lines[0].startswith('Close: '):
        lines.pop(0)
    width = int(os.environ.get('T_PICKER_HEADER_WIDTH', '60'))
    message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', message)
    message = ''.join(c for c in message if c.isprintable())
    path.write_text(clip_columns('Close: ' + message, width) + '\n' + '\n'.join(lines) + '\n')


class Picker:
    def __init__(self, count):
        self.url = 'http://127.0.0.1:' + str(int(os.environ['FZF_PORT'])) + '/'
        self.key = os.environ['FZF_API_KEY']
        self.count = count
        self.deadline = time.monotonic() + 5
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, actions=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('picker update timed out')
        request = urllib.request.Request(
            self.url + (f'?limit={self.count}' if actions is None else ''),
            data=None if actions is None else actions.encode(),
            headers={'X-API-Key': self.key})
        with self.opener.open(request, timeout=min(remaining, 1)) as response:
            result = response.read()
        return json.loads(result) if actions is None else None

    def wait(self, query, total):
        while True:
            state = self.request()
            if (state['query'] == query and state['totalCount'] == total
                    and not state['reading'] and state['progress'] == 100):
                return state
            time.sleep(.01)


def lock_query(directory, query):
    path = directory / 'action-query-locked'
    temporary = directory / 'action-query-next'
    temporary.write_text(query)
    temporary.replace(path)


def update(directory, changed):
    """Run after fzf's execute returns: its HTTP API needs a running event loop."""
    count = len((directory / 'all').read_text().splitlines())
    picker = Picker(count + 1)
    try:
        before = picker.request()
        if changed:
            query = before['query']
            (directory / 'action-query').write_text(query)
            replaced_path = directory / 'replaced'
            replaced = json.loads(replaced_path.read_text()) if replaced_path.exists() else []
            replaced_path.unlink(missing_ok=True)
            marks = [identity(item['text']) for item in before['selected']
                     if list(identity(item['text'])[:2]) not in replaced]
            total = len((directory / 'current').read_text().splitlines())
            # 0.44 clears marks on reload. Restore exact surviving identities in
            # selection order, including marks hidden by the current query.
            lock_query(directory, '')
            picker.request('reload-sync(cat "$T_PICKER_CURRENT")+clear-query')
            state = picker.wait('', total)
            positions = {identity(item['text']): n for n, item in enumerate(state['matches'], 1)}
            actions = ['deselect-all']
            actions += [f'pos({positions[key]})+select' for key in marks if key in positions]
            lock_query(directory, query)
            actions.append('transform-query(cat "$T_PICKER_VIEWS/action-query")')
            picker.request('+'.join(actions))
            state = picker.wait(query, total)
            matches = before.get('matches', [])
            focused = matches[before['position']] if 0 <= before['position'] < len(matches) else {}
            positions = {identity(item['text']): n for n, item in enumerate(state['matches'], 1)}
            position = positions.get(identity(focused.get('text', '')),
                                     min(before['position'] + 1, max(1, state['matchCount'])))
            picker.request(f'pos({position})')
        picker.request(HEADER + '+unbind(change)+' + '+'.join(f'rebind({key})' for key in KEYS))
    except (OSError, ValueError, KeyError, TimeoutError):
        # Do not let Enter accidentally accept a different row after an update
        # failure. Esc remains available; no additional session action is run.
        if directory.exists():
            status(directory, 'update failed; Esc to close')
            try:
                picker.deadline = time.monotonic() + 1
                picker.request(HEADER)
            except (OSError, ValueError, TimeoutError):
                pass
    finally:
        (directory / 'update.pid').unlink(missing_ok=True)


def close(directory, row, query=''):
    lock_query(directory, query)
    row = next((line for line in (directory / 'current').read_text().splitlines()
                if identity(line) == identity(row)), '')
    if not row:
        status(directory, 'session unavailable; row kept')
        changed = False
    else:
        try:
            with _spinner('Closing session'):
                result = subprocess.run(['zsh', '-f', os.environ['T_PICKER_KILL_CALLBACK'], row],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            code, output = result.returncode, result.stdout
        except KeyboardInterrupt:
            code, output = 130, ''
        changed = code == 0
        if changed:
            prune(directory, row)
            message = row.split('\t')[1] + ' closed'
        elif code in (130, -2):
            message = 'canceled; row kept'
        else:
            message = next((line for line in reversed(output.splitlines()) if line.strip()),
                           'failed; row kept')
        status(directory, message)
    start_update(directory, changed)


def start_update(directory, changed):
    # No inherited terminal descriptors or coverage bootstrap in this short-lived
    # updater. Cleanup is owned by the picker, including cancellation mid-update.
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    worker = subprocess.Popen([sys.executable, __file__, 'update', str(int(changed))],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env)
    (directory / 'update.pid').write_text(str(worker.pid))


@contextmanager
def refreshing_header(directory, picker, profile, health):
    """Repaint the existing header while a bounded metadata probe is running."""
    stopped = threading.Event()
    status_file = directory / 'discovery-status'
    status_file.write_text(json.dumps({**health, 'refreshing': True}))
    def paint():
        while not stopped.wait(.12):
            try:
                render(directory, profile, 'current', int(os.environ['T_PICKER_HEADER_WIDTH']),
                       os.environ.get('T_PICKER_COLUMNS', ''))
                picker.deadline = time.monotonic() + 1
                picker.request(HEADER)
            except (OSError, ValueError, TimeoutError):
                return
    worker = threading.Thread(target=paint, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()


def completed_rows(cfg, source):
    """Ask each owner host for its existing close-eligibility proof."""
    groups = {}
    for line in source.splitlines():
        host, _, row = line.partition('\t')
        groups.setdefault(host, []).append(row)
    completed = []
    root = Path(__file__).resolve().parents[1]
    script = 'source ' + shlex.quote(str(root / 't.plugin.zsh')) + '; _dev_kill_completed_labels'
    env = {**os.environ, 'T_DISCOVERY_COLLECTOR': '1', 'T_NO_UPDATE_CHECK': '1'}
    for host, rows in groups.items():
        command = ['zsh', '-lic', script]
        if host != 'local':
            target = cfg.hosts.get(host)
            if not target:
                continue
            command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', target,
                       'zsh -lic ' + shlex.quote('_dev_kill_completed_labels')]
        try:
            result = subprocess.run(command, input='\n'.join(rows) + '\n',
                                    capture_output=True, text=True, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            completed.extend([host, label] for label in result.stdout.splitlines() if label)
    return completed


def observe_open(cfg, cache, parse, format_rows):
    directory = Path(os.environ['T_PICKER_VIEWS'])
    repo = os.environ.get('T_OPEN_REPO') or None
    profile = os.environ.get('T_PICKER_SESSION_FILTERS', 'open')
    roots = ()
    if repo in cfg.repos:
        root = cfg.repos[repo]
        roots = (root, str(Path(cfg.worktree_root) / Path(root).name))
    picker = Picker(1)
    last_revision = None
    hydrated = None
    hydrated_terminals = {}
    completion = None
    while directory.exists():
        time.sleep(.35)
        if (directory / 'update.pid').exists():
            continue
        refresh = directory / 'refresh-request'
        manual = refresh.exists() and completion is None
        refresh.unlink(missing_ok=True)
        snapshot = cache['discover'](cfg, local=not bool(os.environ.get('T_OPEN_REMOTE')),
                                     fields=['rows', 'scopes', 'catalog'], refresh=manual, live_wait=manual)
        if manual:
            completion = cache['requested_refresh'](snapshot, live=True)
        if snapshot.get('unavailable'):
            (directory / 'discovery-status').write_text(json.dumps({'unavailable': True}))
            render(directory, profile, 'current', int(os.environ['T_PICKER_HEADER_WIDTH']),
                   os.environ.get('T_PICKER_COLUMNS', ''))
            picker.deadline = time.monotonic() + 5
            picker.request(HEADER)
            return
        local = next((host for host in snapshot.get('hosts', []) if host['host'] == 'local'), None)
        revision = local.get('live_revision') if local else None
        if local and (manual or hydrated is None or revision != last_revision):
            with refreshing_header(directory, picker, profile, cache['resume_health'](snapshot)):
                snapshot = cache['hydrate_local'](cfg, snapshot, roots)
            local = next(host for host in snapshot['hosts'] if host['host'] == 'local')
            hydrated = local.get('value')
            hydrated_terminals = local.get('terminals', {})
            last_revision = revision
        elif local and hydrated is not None:
            local['value'] = hydrated
            local.setdefault('terminals', hydrated_terminals)
        if completion and completion(snapshot):
            completion = None
        health = cache['resume_health'](snapshot)
        if health.get('hosts') and all(host.get('status') == 'current' for host in health['hosts']):
            (directory / 'discovery-notice').write_text('')
        (directory / 'discovery-status').write_text(json.dumps(health))
        render(directory, profile, 'current', int(os.environ['T_PICKER_HEADER_WIDTH']),
               os.environ.get('T_PICKER_COLUMNS', ''))
        picker.deadline = time.monotonic() + 5
        picker.request(HEADER)
        source = cache['rows'](snapshot, repo=repo, local_roots=roots,
                               move_only=profile == 'move')
        previous = (directory / 'source').read_text()
        if source == previous and not manual:
            continue
        terminals = { (host['host'], label): terminal
                      for host in snapshot.get('hosts', [])
                      for label, terminal in host.get('terminals', {}).items() }
        rows = format_rows(parse(source, host_prefixed=True),
                           remote=True, scoped=bool(repo), compact=profile != 'kill',
                           header=True, last_turn=True)
        heading, *display = rows or ['']
        stamped_display = []
        for line, raw in zip(display, source.splitlines(), strict=True):
            key = tuple(line.split('\t')[:2])
            stamped = line + '\t' + terminals.get(key, '')
            encoded = base64.b64encode(raw.encode()).decode()
            stamped += '\t' + raw.split('\t')[1] + '\t' + encoded
            stamped_display.append(stamped)
        display = stamped_display
        old_ids = {tuple(f[i] for i in (0, 3)): f[1]
                   for line in previous.splitlines() if len(f := line.split('\t')) >= 5}
        new_ids = {tuple(f[i] for i in (0, 3)): f[1]
                   for line in source.splitlines() if len(f := line.split('\t')) >= 5}
        changed_owners = [list(key) for key in old_ids if old_ids[key] != new_ids.get(key)]
        completed = []
        if profile == 'kill':
            with refreshing_header(directory, picker, profile, health):
                completed = completed_rows(cfg, source)
        staged = directory / 'incoming.json'
        staged.write_text(json.dumps(dict(source=source.splitlines(), display=display,
            heading=heading, replaced=changed_owners, full=True,
            completed=completed)))
        actions = '+'.join(f'unbind({key})' for key in KEYS)
        picker.request(actions + '+rebind(change)+execute-silent(python3 "$T_HOME/libexec/t_picker_close.py" append {q})')
        while staged.exists() and directory.exists() and time.monotonic() < picker.deadline:
            time.sleep(.02)
        if staged.exists():
            break


def append(directory, query):
    lock_query(directory, query)
    staged = directory / 'incoming.json'
    data = json.loads(staged.read_text())
    if data.get('full'):
        (directory / 'replaced').write_text(json.dumps(data['replaced']))
    profile = os.environ.get('T_PICKER_SESSION_FILTERS', 'open')
    views = [('source', data['source']), ('all', data['display'])]
    detached = [row for row in data['display'] if row.split('\t')[2] == 'detached']
    if profile == 'kill':
        # Completion is recomputed using the owner host before publishing.
        safe = {tuple(row) for row in data.get('completed', [])}
        completed = [row for row in data['display'] if tuple(row.split('\t')[:2]) in safe
                     and list(row.split('\t')[:2]) not in data.get('replaced', [])]
        views += [('rows-3', data['display']), ('rows-2', detached),
                  ('rows-1', completed), ('rows-0', [row for row in completed if row in detached])]
    else:
        views += [('rows-1', data['display']), ('rows-0', detached)]
    for name, lines in views:
        path = directory / name
        if data.get('full'):
            path.write_text('')
        if data.get('removed'):
            index = 3 if name == 'source' else 1
            state = index + 1
            retained = []
            for row in path.read_text().splitlines():
                fields = row.split('\t')
                if fields[0] != 'local' or fields[index] not in data['removed'] or fields[state] not in ('attached', 'detached'):
                    retained.append(row)
            path.write_text(''.join(row + '\n' for row in retained))
        with path.open('a') as output:
            for line in lines:
                print(line, file=output)
    render(directory, os.environ.get('T_PICKER_SESSION_FILTERS', 'open'), 'current',
           int(os.environ['T_PICKER_HEADER_WIDTH']),
           os.environ.get('T_PICKER_COLUMNS') or data['heading'])
    start_update(directory, True)
    staged.unlink()


if __name__ == '__main__':
    root = Path(os.environ['T_PICKER_VIEWS'])
    if sys.argv[1] == 'close':
        close(root, sys.argv[2], sys.argv[3])
    elif sys.argv[1] == 'append':
        append(root, sys.argv[2])
    else:
        update(root, sys.argv[2] == '1')
