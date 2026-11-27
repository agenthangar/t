"""Close one focused session and update only the live picker's cached snapshot."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

from t_picker_filters import render
from t_progress import spinner as _spinner
from t_session_details import clip_columns

KEYS = ('ctrl-n', 'enter', 'ctrl-k', 'space', 'tab', 'ctrl-a', 'ctrl-f', 'ctrl-l', 'ctrl-x', 'ctrl-t', 'ctrl-g', 'ctrl-d', 'ctrl-o', 'ctrl-r',
        'ctrl-p', 'ctrl-b', 'ctrl-v', 'ctrl-e', 'ctrl-w', 'ctrl-s', 'ctrl-y', 'alt-s', 'alt-t', 'alt-r', 'alt-g', 'alt-o', 'alt-v')
HEADER = 'transform-header(cat "$T_PICKER_CURRENT_HEADER")'


def identity(row):
    fields = row.split('\t')
    return tuple(fields[:3]) + tuple(fields[5:6])


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
                                 if tuple(line.split('\t')[i] for i in (0, 3, 4)) != target[:3]))
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
            marks = [identity(item['text']) for item in before['selected']]
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


def observe_open(cfg, cache, parse, format_rows):
    directory = Path(os.environ['T_PICKER_VIEWS'])
    repo = os.environ.get('T_OPEN_REPO') or None
    roots = ()
    if repo in cfg.repos:
        root = cfg.repos[repo]
        roots = (root, str(Path(cfg.worktree_root) / Path(root).name))
    picker = Picker(1)
    while directory.exists():
        time.sleep(.35)
        if (directory / 'update.pid').exists():
            continue
        view = cache['request']('live-view')
        if 'terminals' not in view:
            continue
        terminals = view['terminals']
        existing = {}
        for line in (directory / 'all').read_text().splitlines():
            fields = line.split('\t')
            if len(fields) >= 6 and fields[0] == 'local' and fields[2] in ('attached', 'detached'):
                existing[fields[1]] = (fields[5], fields[2])
        scoped = cache['rows']({'hosts': [dict(view, host='local')]}, repo=repo, local_roots=roots)
        additions = []
        current = {}
        for line in scoped.splitlines():
            fields = line.split('\t')
            label = fields[3]
            if label in terminals:
                current[label] = (terminals[label], fields[4])
                if existing.get(label) != current[label]:
                    additions.append(line)
        removed = [label for label in existing if existing[label] != current.get(label)]
        if not additions and not removed:
            continue
        rows = format_rows(parse('\n'.join(additions), host_prefixed=True),
                           remote=bool(os.environ.get('T_OPEN_REMOTE')), scoped=bool(repo),
                           compact=True, header=True, last_turn=True)
        heading, *display = rows or ['']
        display = [line + '\t' + terminals[line.split('\t')[1]] for line in display]
        staged = directory / 'incoming.json'
        staged.write_text(json.dumps(dict(source=additions, display=display, heading=heading, removed=removed)))
        picker.deadline = time.monotonic() + 5
        # Serialize adoption with ordinary filter, sort and close actions in fzf.
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
    for name, lines in [('source', data['source']), ('all', data['display']),
                        ('rows-1', data['display']),
                        ('rows-0', [row for row in data['display'] if row.split('\t')[2] == 'detached'])]:
        path = directory / name
        if data['removed']:
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
    render(directory, 'open', 'current', int(os.environ['T_PICKER_HEADER_WIDTH']),
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
