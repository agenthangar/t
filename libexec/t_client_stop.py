"""Stop one verified terminal client, preserving its pane until it has exited.

Private lifecycle/recovery helper. Tokens bind process birth and pane identity;
no process groups, service processes or replacement owners are signalled.
"""
import ctypes
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP = runpy.run_path(str(ROOT / 'libexec/t_codex_ownership.py'))
NATIVE = runpy.run_path(str(ROOT / 'libexec/t_terminal_close.py'))


class ClientStopPending(ValueError):
    code = 75


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def process(pid):
    value = run(['ps', '-p', str(pid), '-o', 'ppid=,stat=,comm='], timeout=5)
    if value.returncode or not value.stdout.strip():
        if value.returncode not in (0, 1) or (value.stderr or '').strip():
            raise ValueError('could not inspect selected client process')
        return None
    fields = value.stdout.strip().split(None, 2)
    if len(fields) != 3 or not fields[0].isdecimal():
        raise ValueError('could not verify client process identity')
    if fields[1].startswith('Z'):
        return None
    try:
        detail = NATIVE['process_detail'](int(pid), timeout=5)
    except (OSError, ValueError):
        if run(['ps', '-p', str(pid), '-o', 'pid='], timeout=5).returncode:
            return None
        raise
    return dict(pid=int(pid), birth=detail['birth'], parent=int(fields[0]),
                comm=fields[2], executable=detail['executable'], argv=detail['argv'], cwd=os.path.realpath(detail['cwd']),
                started=birth_epoch(detail['birth']))


def birth_epoch(birth):
    if sys.platform == 'darwin':
        return birth[0] + birth[1] / 1000000
    for line in Path('/proc/stat').read_text().splitlines():
        if line.startswith('btime '):
            return int(line.split()[1]) + int(birth) / os.sysconf('SC_CLK_TCK')
    raise ValueError('could not verify native client start time')


def process_socket(pid):
    """Read only the inherited TMUX server address from native argv/environment."""
    if sys.platform == 'linux':
        entries = (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
    elif sys.platform == 'darwin':
        libc = ctypes.CDLL(None, use_errno=True)
        size = ctypes.c_size_t(1024 * 1024)
        buffer = ctypes.create_string_buffer(size.value)
        mib = (ctypes.c_int * 3)(1, 49, int(pid))
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
            raise ValueError('could not verify inherited tmux address')
        raw = buffer.raw[:size.value]
        argc = ctypes.c_int.from_buffer_copy(raw[:4]).value
        _, _, tail = raw[4:].partition(b'\0')
        entries = tail.lstrip(b'\0').split(b'\0')[argc:]
    else:
        return None
    addresses = [os.fsdecode(entry[5:]).rsplit(',', 2)[0] for entry in entries if entry.startswith(b'TMUX=')]
    return addresses[0] if len(addresses) == 1 and os.path.isabs(addresses[0]) else None


def cwd(pid):
    if Path('/proc').is_dir():
        try:
            return os.path.realpath(os.readlink(f'/proc/{pid}/cwd'))
        except OSError:
            return None
    result = run(['lsof', '-nP', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], timeout=5)
    names = [line[1:] for line in result.stdout.splitlines() if line.startswith('n')]
    return os.path.realpath(names[0]) if not result.returncode and len(names) == 1 else None


def client(value):
    name = os.path.basename(value['comm'])
    if sys.platform == 'linux':
        name = name.removesuffix(' (deleted)')
    agent = 'claude' if name == 'claude' else 'codex' if re.fullmatch(r'codex(?:-(?:aarch64|x86_64)-.*)?', name) else None
    return None if 'app-server' in value['argv'] else agent


def tmux_absent(message, *, selected=False):
    message = (message or '').lower()
    return any(text in message for text in ('no server running on ', 'no sessions',
        '(no such file or directory)', '(connection refused)')) or (selected and "can't find session:" in message)


def pane(session, immutable, socket=None):
    if socket is None:
        result = run(['tmux', 'display-message', '-p', '-t', immutable + ':', '#{socket_path}'], timeout=5)
        socket = result.stdout.strip() if not result.returncode else ''
        if not socket or not os.path.isabs(socket):
            raise ValueError('could not verify tmux socket')
    result = run(['tmux', '-S', socket, 'list-panes', '-s', '-t', immutable + ':', '-F',
                  '#{session_id}|#{session_name}|#{session_path}|#{pane_id}|#{pane_pid}'], timeout=5)
    if result.returncode:
        if result.returncode == 1 and tmux_absent(result.stderr, selected=True):
            return None
        raise ValueError('could not inspect selected tmux pane')
    rows = result.stdout.splitlines()
    if len(rows) != 1:
        raise ValueError('selected slot has multiple panes; nothing was stopped')
    parts = rows[0].split('|')
    fields = [*parts[:2], '|'.join(parts[2:-2]), *parts[-2:]] if len(parts) >= 5 else []
    if len(fields) != 5 or fields[0] != immutable or fields[1] != session or not fields[4].isdecimal():
        raise ValueError('selected pane identity changed; nothing was stopped')
    return dict(socket=socket, session=fields[0], name=fields[1], workspace=os.path.realpath(fields[2]),
                pane=fields[3], pid=int(fields[4]))


def pane_client(owner):
    result = run(['ps', '-A', '-o', 'pid=,ppid='], timeout=5)
    if result.returncode:
        raise ValueError('could not inspect pane descendants')
    parents = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(v.isdecimal() for v in fields):
            raise ValueError('could not inspect pane descendants')
        parents[int(fields[0])] = int(fields[1])
    descendants = {owner['pid']}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    found = []
    for pid in sorted(descendants):
        value = process(pid)
        if value and client(value):
            found.append(value)
    if len(found) > 1:
        raise ValueError('multiple pane clients found; nothing was stopped')
    return found[0] if found else None


def capture(session, workspace, immutable, pid, agent, socket=None, generation="", proof=""):
    if not re.fullmatch(r'\$[0-9]+', immutable) or agent not in ('codex', 'claude'):
        raise ValueError('invalid selected owner')
    owner = pane(session, immutable, socket)
    if not owner or owner['workspace'] != os.path.realpath(workspace):
        raise ValueError('selected pane identity changed; nothing was stopped')
    value = pane_client(owner)
    if (value is None) != (not pid) or (value and (str(value['pid']) != str(pid) or client(value) != agent)):
        raise ValueError('selected client changed; nothing was stopped')
    directory = cwd(value['pid']) if value else None
    if value and directory != os.path.realpath(workspace):
        raise ValueError('selected client workspace changed; nothing was stopped')
    root = process(owner['pid'])
    token = dict(owner=owner, process=value, cwd=directory, agent=agent, pane_birth=root['birth'] if root else None)
    if socket:
        mark = NATIVE['conversation_mark'](socket, immutable, owner['pane'], int(pid)) if proof else None
        token['automatic'] = dict(generation=generation, proof=proof,
                                  conversation=json.dumps(mark) if mark else None)
    verify(token)
    return token


def verify_automatic(token):
    automatic = token.get('automatic')
    if automatic is None:
        return
    owner = token['owner']
    target = (owner['socket'], owner['session'], owner['pane'], str(owner['pid']))
    generation = automatic['generation']
    if not (NATIVE['attachment_matches'](*target, generation) if generation else NATIVE['owner'](*target)):
        raise ValueError('automatic close lost its detached owner')
    if automatic['proof']:
        api = runpy.run_path(str(ROOT / 'libexec/t_close_queue.py'))
        proof = json.loads(automatic['proof'])
        if not api['validate'](proof):
            raise ValueError('completed shipping evidence changed before stop')
        ticket = proof['ticket']
        observed = NATIVE['owner'](*target)
        screen = NATIVE['tmux'](owner['socket'], 'capture-pane', '-p', '-e', '-t', owner['pane'])
        if (not observed or screen.returncode or
                NATIVE['composer_empty'](ticket['agent'], screen.stdout, observed['cursor'], observed.get('column')) is not True or
                json.dumps(NATIVE['conversation_mark'](owner['socket'], owner['session'], owner['pane'],
                                                     int(ticket['pid']))) != automatic['conversation'] or
                not NATIVE['attachment_matches'](*target, generation)):
            raise ValueError('automatic close lost its empty composer or detached owner')


def verify(token):
    expected = token['process']
    owner = pane(token['owner']['name'], token['owner']['session'], token['owner']['socket'])
    current = process(expected['pid']) if expected else None
    root = process(token['owner']['pid'])
    if root and root['birth'] != token['pane_birth']:
        raise ValueError('pane process changed; nothing was stopped')
    if expected and current is None:
        if owner is not None and (owner != token['owner'] or pane_client(owner) is not None):
            raise ValueError('pane owner changed after client exit')
        return False
    if owner != token['owner'] or (expected and (current != expected or cwd(expected['pid']) != token['cwd'])):
        raise ValueError('selected owner changed; nothing was stopped')
    if pane_client(owner) != expected:
        raise ValueError('selected pane client changed; nothing was stopped')
    return bool(expected)


def stop(token, *, force=False, timeout=3):
    if not verify(token):
        return
    verify_automatic(token)
    os.kill(token['process']['pid'], signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process(token['process']['pid']) is None:
            verify(token)
            return
        time.sleep(.05)
    verify(token)
    raise ClientStopPending('agent has not exited; pane retained and no release recorded')


def finalize(token):
    """Remove only the exited client's original pane in one tmux server action."""
    if verify(token):
        raise ValueError('client is still running; pane retained')
    owner = token['owner']
    if pane(owner['name'], owner['session'], owner['socket']) is None:
        return  # Natural exit already removed this immutable session.
    condition = '#{&&:#{==:#{session_id},' + owner['session'] + '},#{&&:#{==:#{pane_id},' + owner['pane'] + '},#{==:#{pane_pid},' + str(owner['pid']) + '}}}'
    if 'automatic' in token:
        condition = '#{&&:#{==:#{session_attached},0},' + condition + '}'
        generation = token['automatic']['generation']
        if generation:
            if not re.fullmatch(r'[0-9]+:[0-9a-f]{32}', generation):
                raise ValueError('invalid attachment generation')
            condition = '#{&&:#{==:#{@t_terminal_attachment},' + generation + '},' + condition + '}'
    result = run(['tmux', '-S', owner['socket'], 'if-shell', '-F', '-t', owner['session'] + ':', condition,
                  'kill-session -t ' + shlex.quote(owner['session'] + ':'),
                  'display-message -p client-stop-owner-changed'], timeout=5)
    if result.returncode or result.stdout.strip():
        raise ValueError('pane changed before close; pane retained')


def failed_close(workspace, sid, *, directory=None, now=None):
    """Only a recent interrupted managed close can authorize orphan repair."""
    folder = Path(directory) if directory else Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/lifecycle/operations'
    matches = []
    for path in folder.glob('*.json'):
        try:
            info = path.lstat()
            if path.is_symlink() or not path.is_file() or info.st_size > 1024 * 1024 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            value = json.loads(path.read_text())
            args, receipts = value.get('arguments', []), value.get('receipts', {})
            age = (time.time() if now is None else now) - float(value.get('updated', 0))
            if (value.get('verb') != 'close' or value.get('phase') != 'failed' or value.get('pending') != 'stop'
                    or len(args) != 4 or args[2:] != [sid, 'codex'] or os.path.realpath(args[1]) != os.path.realpath(workspace)
                    or not 0 <= age <= 86400 or any(key in receipts for key in ('stop', 'released', 'record'))
                    or not re.fullmatch(r'\$[0-9]+', receipts.get('inspect', ''))
                    or not str(value.get('error', '')).endswith('agent has not exited; no release recorded')):
                continue
            matches.append(dict(path=str(path), identity=[info.st_dev, info.st_ino, info.st_mtime_ns],
                                session=args[0], immutable=receipts['inspect'],
                                started=float(value.get('started', value['updated']))))
        except (OSError, ValueError, TypeError):
            continue
    if len(matches) != 1:
        return None
    return matches[0]


def missing_session(receipt):
    if not receipt.get('socket') or not os.path.isabs(receipt['socket']):
        return False
    result = run(['tmux', '-S', receipt['socket'], 'list-sessions', '-F', '#{session_id}|#{session_name}'], timeout=5)
    if result.returncode:
        return result.returncode == 1 and tmux_absent(result.stderr)
    rows = [line.split('|') for line in result.stdout.splitlines()]
    return all(len(row) == 2 for row in rows) and not any(
        row[0] == receipt['immutable'] or row[1] == receipt['session'] for row in rows)


def stranded(workspace, sid, *, directory=None):
    if not os.path.isabs(workspace) or not OWNERSHIP['_UUID'].fullmatch(sid):
        return None
    receipt = failed_close(workspace, sid, directory=directory)
    if receipt is None:
        return None
    home = str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    folder = OWNERSHIP['_lock_dir'](home)
    if folder is None:
        return None
    path = str(Path(folder) / (sid + '.lock'))
    if not OWNERSHIP['_lock_held'](path):
        return None
    cwds, pids = OWNERSHIP['_open_writer_cwds'](path, sid, run)
    if len(pids) != 1 or {os.path.realpath(p) for p in cwds} != {os.path.realpath(workspace)}:
        return None
    pid = pids[0]
    if OWNERSHIP['_standalone_writer_cwd'](path, pid, run) != os.path.realpath(workspace):
        return None
    value = process(pid)
    receipt['socket'] = process_socket(pid)
    if (not value or value.get('parent') != 1 or value.get('started', float('inf')) > receipt['started']
            or not missing_session(receipt)):
        return None
    tty = run(['ps', '-p', str(pid), '-o', 'tty='], timeout=5)
    fds = OWNERSHIP['_writer_fds'](pid, run)
    if (not value or client(value) != 'codex' or cwd(pid) != os.path.realpath(workspace)
            or tty.returncode or tty.stdout.strip() not in ('?', '??', '-') or fds is None
            or any(fd.get('f', '').rstrip('rwu') in ('0', '1', '2') and
                   fd.get('t') == 'CHR' and fd.get('n') != '/dev/null' for fd in fds)):
        return None
    info = os.stat(path)
    token = dict(receipt=receipt, process=value, workspace=os.path.realpath(workspace), sid=sid,
                 directory=str(directory) if directory else None, lock=[info.st_dev, info.st_ino])
    if not missing_session(receipt) or process(pid) != value:
        return None
    return token


def stop_stranded(token, *, force=False, timeout=3):
    current = stranded(token['workspace'], token['sid'], directory=token['directory'])
    if current != token:
        raise ValueError('stranded writer identity changed; nothing was stopped')
    os.kill(token['process']['pid'], signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process(token['process']['pid']) is None:
            return
        time.sleep(.05)
    if stranded(token['workspace'], token['sid'], directory=token['directory']) != token:
        raise ValueError('stranded writer identity changed; repair stopped')
    raise ClientStopPending('agent has not exited; no conversation resumed')


def confirm(title, note, action, *, api=None):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    api = api or runpy.run_path(str(ROOT / 'bin/t'), run_name='client_stop_confirmation')
    ui_type = runpy.run_path(str(ROOT / 'libexec/t_share_ui.py'))['ShareUI']
    with ui_type(api) as ui:
        ui.intro(title, note)
        return ui.choose('Recovery', [dict(key='cancel', label='Cancel'),
                                      dict(key='go', label=action)]) == 'go'


def main(argv):
    try:
        if argv[0] == 'capture':
            print(json.dumps(capture(*argv[1:])))
        elif argv[0] == 'verify-live':
            return 0 if verify(json.loads(argv[1])) else 1
        elif argv[0] == 'stop':
            stop(json.loads(argv[1]), force=len(argv) > 2 and argv[2] == 'force')
        elif argv[0] == 'finalize':
            finalize(json.loads(argv[1]))
        elif argv[0] == 'confirm':
            return 0 if confirm(*argv[1:]) else 130
        else:
            return 2
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('t: ' + str(error), file=sys.stderr)
        return getattr(error, 'code', 1)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
