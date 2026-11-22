"""Conservative, event-driven cleanup after a managed terminal is lost.

No keyboard input is read. A detached client with a usable terminal is deliberately
ignored. Unknown agent/composer/Git/process evidence always retains the owner.
"""
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import select
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PROTOCOL = 1
HOOK = 'client-detached[50100]'
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PROCESS_FIELDS = ('argv', 'executable', 'cwd', 'birth', 'import_safe')


def run(args, **kwargs):
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=kwargs.pop('timeout', 15), cwd='/', **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 1, '', str(error))


def tmux(socket, *args):
    return run(['tmux', '-S', socket, *args])


def hung_up(fd):
    poll = select.poll()
    poll.register(fd, select.POLLHUP | select.POLLERR)
    return any(events & select.POLLHUP for _, events in poll.poll(0))


def runtime():
    return runpy.run_path(str(ROOT / 'libexec/t_runtime.py'))


def hook_commands(socket, pane, source=None):
    source = source or str(ROOT)
    bridge = runtime()['dispatcher'](source)
    if bridge == Path(source) / 'libexec/t_runtime_dispatch.py':
        prefix = [sys.executable, str(Path(source) / 'libexec/t_terminal_close.py')]
    else:
        prefix = [sys.executable, str(bridge), source, 'libexec/t_terminal_close.py']
    worker = shlex.join([*prefix, 'detached', socket, pane['identity'], pane['pane'], pane['pane_pid']])
    worker += ' #{q:hook_client} >/dev/null 2>&1 || :'
    attached = shlex.join([*prefix, 'attached', socket]) + ' #{q:hook_client} >/dev/null 2>&1 || :'
    return {HOOK: shlex.join(['run-shell', '-b', worker]),
            'client-attached[50100]': shlex.join(['run-shell', attached]),
            'client-session-changed[50100]': shlex.join(['run-shell', attached])}


def install(session, socket=None, expected=None, source=None):
    result = (tmux(socket, 'display-message', '-p', '-t', '=' + session + ':',
                   '#{session_name}\t#{session_id}\t#{socket_path}\t#{pane_id}\t#{pane_pid}')
              if socket else run(['tmux', 'display-message', '-p', '-t', '=' + session + ':',
                  '#{session_name}\t#{session_id}\t#{socket_path}\t#{pane_id}\t#{pane_pid}']))
    fields = result.stdout.strip().split('\t')
    if result.returncode or len(fields) != 5 or not fields[0].startswith('dev-'):
        return 1
    _, identity, socket, pane, pane_pid = fields
    binding = dict(identity=identity, pane=pane, pane_pid=pane_pid)
    if expected and any(binding[key] != expected[key] for key in binding):
        return 1
    for hook, command in hook_commands(socket, binding, source).items():
        if expected:
            # The updater revalidates the exact session/pane incarnation before
            # every owned hook write; it never targets a recycled session name.
            utilities = runtime()
            if not utilities['same_owner'](socket, expected, ROOT):
                return 1
            existing = tmux(socket, 'show-hooks', '-t', identity + ':')
            rows = [line for line in existing.stdout.splitlines() if line.startswith(hook + ' ')]
            if existing.returncode or (rows and utilities['parsed_hook'](rows[0], socket, expected) is None):
                return 1
        result = tmux(socket, 'set-hook', '-t', identity + ':', hook, command)
        if result.returncode:
            return result.returncode
    return tmux(socket, 'set-option', '-t', identity + ':', '@t_runtime_source', source or str(ROOT)).returncode


def event_path(socket, tty):
    root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/terminal-close'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError('unsafe terminal observer state')
    return root / hashlib.sha256((socket + tty).encode()).hexdigest()


def attached(socket, tty, pid=None):
    """Hold the original PTY open, so a recycled /dev/tty name is never evidence."""
    if not re.fullmatch(r'/dev/(?:pts/\d+|tty[\w.-]+)', tty):
        return 1
    if pid is None:
        result = tmux(socket, 'list-clients', '-F', '#{client_tty}|#{client_pid}')
        matches = [line.split('|')[1] for line in result.stdout.splitlines()
                   if line.startswith(tty + '|') and line.count('|') == 1]
        if result.returncode or len(matches) != 1:
            return 1
        pid = matches[0]
    if not pid.isdigit():
        return 1
    base = event_path(socket, tty)
    lock = os.open(str(base) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fd = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # the same client switched managed sessions
        try:
            fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError:
            return 1  # Linux can remove the slave path before we open it.
        if hung_up(fd):
            return 1  # missed attachment: preserve, never guess
        Path(str(base) + '.event').unlink(missing_ok=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watch', socket, tty, pid,
                          str(fd), str(lock)], pass_fds=(fd, lock), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd='/', env=env)
        atomic_write(Path(str(base) + '.ready'), pid)
        return 0
    finally:
        if fd is not None:
            os.close(fd)
        os.close(lock)


def notify_detached(socket, identity, pane, pane_pid, tty):
    base = event_path(socket, tty)
    atomic_write(Path(str(base) + '.event'), json.dumps([identity, pane, pane_pid]))
    return 0


def watch(socket, tty, pid, descriptor, lock_descriptor, checkpoint=None):
    fd, lock = int(descriptor), int(lock_descriptor)
    event = Path(str(event_path(socket, tty)) + '.event')
    gone_at = None
    helper, record = None, None
    base = event_path(socket, tty)
    runtime_path = Path(str(base) + '.runtime.json')
    reload_path = Path(str(base) + '.reload.json')
    try:
        helper = runtime()
        token = None
        if checkpoint:
            saved = json.loads(checkpoint)
            if (saved['terminal_fd'] != helper['fd_identity'](fd)
                    or saved['lock_fd'] != helper['fd_identity'](lock)
                    or saved['socket'] != socket or saved['tty'] != tty or saved['client_pid'] != pid):
                raise ValueError('observer descriptor handoff changed')
            helper['validate_source'](ROOT, 'terminalobserver', saved['fingerprint'])
            gone_at, token = saved['gone_at'], saved['token']
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, False)
        os.set_inheritable(lock, False)
        record = helper['publish'](runtime_path, 'terminalobserver', socket, token=token,
                                  tty=tty, client_pid=pid, terminal_fd=helper['fd_identity'](fd),
                                  lock_fd=helper['fd_identity'](lock), descriptor=fd, lock_descriptor=lock, gone_at=gone_at)
        if checkpoint:
            reload_path.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        if checkpoint:
            raise  # Never acknowledge a successor that lost its original PTY.
        helper, record = None, None  # Preserve ordinary observation if metadata is unavailable.
    try:
        while True:
            # A reload is cooperative and precedes event consumption. The same
            # open PTY and flock descriptions, pending event and elapsed loss
            # deadline survive exec. The updater never generates detach events.
            if helper and record and reload_path.exists():
                try:
                    request = helper['replacement'](reload_path, record, 'terminalobserver')
                    if request:
                        saved = dict(socket=socket, tty=tty, client_pid=pid, terminal_fd=record['terminal_fd'],
                                     lock_fd=record['lock_fd'], token=record['token'], gone_at=gone_at,
                                     fingerprint=request['fingerprint'])
                        bridge = helper['dispatcher']()
                        os.set_inheritable(fd, True)
                        os.set_inheritable(lock, True)
                        os.execv(sys.executable, [sys.executable, str(bridge), request['source'],
                                 'libexec/t_terminal_close.py', 'watch-resume', socket, tty, pid,
                                 str(fd), str(lock), json.dumps(saved)])
                except (OSError, ValueError, KeyError, TypeError, SyntaxError):
                    os.set_inheritable(fd, False)
                    os.set_inheritable(lock, False)
                    reload_path.unlink(missing_ok=True)
            if event.exists():
                identity, pane, pane_pid = json.loads(event.read_text())
                event.unlink(missing_ok=True)
                return detached(socket, identity, pane, pane_pid, tty, fd)
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                gone_at = gone_at or time.monotonic()
            if hung_up(fd):
                gone_at = gone_at or time.monotonic()
            if gone_at is not None and time.monotonic() - gone_at > 5:
                return 0  # callback unavailable: no cleanup, even after PID reuse
            time.sleep(.1)
    finally:
        Path(str(event_path(socket, tty)) + '.ready').unlink(missing_ok=True)
        if record and helper['read'](runtime_path).get('token') == record['token']:
            runtime_path.unlink(missing_ok=True)
        os.close(fd)
        os.close(lock)


def owner(socket, identity, pane, pane_pid):
    result = tmux(socket, 'list-panes', '-s', '-t', identity + ':', '-F',
                  '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_pid}\t'
                  '#{session_path}\t#{session_attached}\t#{pane_dead}\t#{cursor_y}')
    rows = result.stdout.splitlines()
    if result.returncode or len(rows) != 1:
        return None
    fields = rows[0].split('\t')
    if (len(fields) != 8 or fields[0] != identity or fields[2:4] != [pane, pane_pid]
            or not fields[1].startswith('dev-') or fields[5:7] != ['0', '0']):
        return None
    return dict(zip(('identity', 'session', 'pane', 'pid', 'workspace', 'attached',
                     'dead', 'cursor'), fields))


def composer_empty(agent, screen, cursor):
    """Recognize a blank current composer, never a prompt quoted in scrollback."""
    if agent not in ('codex', 'claude') or not cursor.isdigit():
        return None
    lines = screen.splitlines()
    row = int(cursor)
    if row >= len(lines):
        return None
    marker = '›' if agent == 'codex' else '❯'
    line = ANSI.sub('', lines[row]).strip()
    if not line.startswith(marker):
        return None
    if line[len(marker):].strip():
        # Codex paints its startup suggestion dim; it is not submitted input.
        # Require the actual style and known placeholder, not just its text.
        raw_input = lines[row].split(marker, 1)[1]
        suggestion = line[len(marker):].strip()
        placeholder = (agent == 'codex' and re.match(r'^\s*\x1b\[(?:\d+;)*2(?:;\d+)*m', raw_input)
                       and re.fullmatch(r'(?:Implement \{feature\}|Write tests for @filename|'
                                        r'Explain this codebase|Find and fix a bug in @filename|'
                                        r'Improve documentation in @filename|Summarize recent commits)', suggestion))
        if not placeholder:
            return False
    # Multiline drafts/dialogs below the cursor must not be mistaken for a
    # blank prompt. Only recognized CLI footer lines and borders are accepted.
    for raw in lines[row + 1:]:
        text = ANSI.sub('', raw).strip()
        if not text:
            continue
        # Draft text can imitate footer wording or a border. Only actual dim
        # chrome is eligible; unstyled multiline content must be retained.
        dim = re.match(r'^\s*\x1b\[(?:\d+;)*2(?:;\d+)*m', raw)
        if dim and re.fullmatch(r'[─━═\s]+', text):
            continue
        if dim and re.fullmatch(r'\? for shortcuts(?:\s+.*)?', text):
            continue
        if dim and agent == 'codex' and re.fullmatch(r'\d+% context left(?:\s+.*)?', text):
            continue
        return None
    return True


def transcript_state(agent, path):
    """Return idle/empty/busy/unknown and associated PR URLs from real records."""
    if not path:
        return 'unknown', set()
    try:
        with open(path, 'rb') as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024 or (data and not data.endswith(b'\n')):
            return 'unknown', set()
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
    except (OSError, ValueError):
        return 'unknown', set()
    state, prs = 'empty', set()
    for record in records:
        if not isinstance(record, dict):
            return 'unknown', prs
        prs.update('https://' + value for value in re.findall(r'github\.com/[\w.-]+/[\w.-]+/pull/\d+', json.dumps(record)))
        if agent == 'codex' and record.get('type') == 'response_item':
            payload = record.get('payload') or {}
            if not isinstance(payload, dict):
                return 'unknown', prs
            if payload.get('role') in ('user', 'assistant') or payload.get('type') in ('function_call', 'function_call_output'):
                state = 'busy'
        elif agent == 'codex' and record.get('type') == 'event_msg':
            payload = record.get('payload') or {}
            if not isinstance(payload, dict):
                return 'unknown', prs
            kind = payload.get('type')
            if kind in ('task_started', 'user_message'):
                state = 'busy'
            elif kind == 'task_complete':
                state = 'idle'
            elif kind == 'turn_aborted':
                state = 'unknown'  # an interrupted task is not completed work
            elif kind in ('agent_message', 'agent_reasoning') and state == 'empty':
                state = 'unknown'
        elif agent == 'claude':
            message = record.get('message') or {}
            if not isinstance(message, dict):
                return 'unknown', prs
            if record.get('type') == 'user' and not any(record.get(key) for key in
                    ('isMeta', 'isSidechain', 'isCompactSummary')):
                state = 'busy'
            elif record.get('type') == 'assistant' and not record.get('isSidechain'):
                state = 'idle' if message.get('stop_reason') == 'end_turn' else 'busy'
    return (state if agent in ('codex', 'claude') else 'unknown'), prs


def history_prs(agent, sid):
    """Retained associations survive transcript compaction and branch changes."""
    if not sid:
        return set()
    path = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/history/v1/history.sqlite3'
    try:
        path.stat()
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)
        try:
            rows = db.execute("select record from conversations where json_extract(record,'$.agent')=? and json_extract(record,'$.sid')=?", (agent, sid)).fetchall()
        finally:
            db.close()
        urls = set()
        for row in rows:
            for pr in json.loads(row[0]).get('prs', []):
                if (not isinstance(pr, dict) or not re.fullmatch(r'[\w.-]+/[\w.-]+', pr.get('repo', ''))
                        or type(pr.get('number')) is not int or pr['number'] < 1):
                    return None
                urls.add(f'https://github.com/{pr["repo"]}/pull/{pr["number"]}')
        return urls
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        return None


def prs_complete(workspace, prs):
    if len(prs) > 8:
        return None
    origin = run(['git', '-C', workspace, 'remote', 'get-url', 'origin'])
    if origin.returncode:
        return None
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?', origin.stdout.strip())
    if not match:
        return None  # other hosts have no verified PR reader yet
    branch = run(['git', '-C', workspace, 'symbolic-ref', '--quiet', '--short', 'HEAD'])
    if branch.returncode or not branch.stdout.strip():
        return None
    deadline = time.monotonic() + 10
    result = run(['gh', 'pr', 'list', '--repo', match[1], '--head', branch.stdout.strip(),
                  '--state', 'open', '--limit', '1', '--json', 'number'], timeout=10)
    try:
        rows = json.loads(result.stdout)
        if result.returncode or not isinstance(rows, list):
            return None
        if rows:
            return False
        for url in sorted(prs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            result = run(['gh', 'pr', 'view', url, '--json', 'state'], timeout=remaining)
            value = json.loads(result.stdout)
            if result.returncode or value.get('state') not in ('MERGED', 'CLOSED'):
                return False if value.get('state') == 'OPEN' else None
    except (ValueError, AttributeError):
        return None
    return True


def startup_screen(agent, screen, cursor):
    lines = [ANSI.sub('', line).strip(' │') for line in screen.splitlines()[:int(cursor)]]
    brand = 'OpenAI Codex' if agent == 'codex' else 'Welcome back'
    if not any(brand in line for line in lines):
        return False
    # No-transcript startup is only accepted while the visible screen is still
    # the recognized welcome panel. Ordinary output cannot stand in for history.
    return all(not line or re.fullmatch(r'[╭╮╰╯┌┐└┘─━═\s]+', line)
               or brand in line or re.match(r'^(?:model:|directory:|Tip:)', line)
               for line in lines)


def revision(workspace, transcript):
    """A changed observation during slow PR checks revokes termination."""
    head = run(['git', '-C', workspace, 'rev-parse', 'HEAD'])
    dirty = run(['git', '-C', workspace, 'status', '--porcelain', '--untracked-files=all'])
    if head.returncode or dirty.returncode or dirty.stdout:
        return None
    try:
        stat = os.stat(transcript) if transcript else None
        return (head.stdout, (stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat else None)
    except OSError:
        return None


def process_table(timeout=15):
    result = run(['ps', '-axo', 'pid=,ppid=,uid=,stat=,lstart='],
                 env={**os.environ, 'LC_ALL': 'C'}, timeout=timeout)
    if result.returncode:
        raise ValueError('unreadable process table')
    table = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5:
            raise ValueError('incomplete process table')
        pid, parent, uid = map(int, fields[:3])
        table[pid] = dict(parent=parent, uid=uid, state=fields[3], start=fields[4])
    return table


def import_environment_safe(entries):
    # Keep only a verdict. Native environment values may contain secrets and
    # never enter process plans, diagnostics, receipts or output.
    unsafe = {b'PYTHONPATH', b'PYTHONHOME', b'PYTHONUSERBASE', b'PYTHONPLATLIBDIR',
              b'PYTHONSTARTUP', b'PYTHONINSPECT'}
    return not any(key in unsafe and value for entry in entries
                   for key, _, value in [entry.partition(b'=')])


def process_detail(pid, timeout=15):
    """Read exact native argv, executable and cwd; never parse ps's display argv."""
    if sys.platform == 'linux':
        root = Path('/proc') / str(pid)
        stat = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        return dict(argv=os.fsdecode((root / 'cmdline').read_bytes()).rstrip('\0').split('\0'),
                    executable=os.readlink(root / 'exe'), cwd=os.readlink(root / 'cwd'),
                    import_safe=import_environment_safe((root / 'environ').read_bytes().split(b'\0')),
                    birth=stat[19])  # starttime (field 22), not second-resolution ps
    if sys.platform != 'darwin':
        raise ValueError('unsupported process inspection')
    libc = ctypes.CDLL(None, use_errno=True)
    size = ctypes.c_size_t(1024 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise ValueError('unreadable native process arguments')
    raw = buffer.raw[:size.value]
    argc = ctypes.c_int.from_buffer_copy(raw[:4]).value
    executable, _, tail = raw[4:].partition(b'\0')
    entries = tail.lstrip(b'\0').split(b'\0')
    argv = entries[:argc]
    if argc < 1 or len(argv) != argc:
        raise ValueError('incomplete native process arguments')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    path = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, path, len(path)) <= 0:
        raise ValueError('unreadable process executable')
    # Public proc_bsdinfo layout, also used by t_offline.py.
    class BSDInfo(ctypes.Structure):
        _fields_ = [('header', ctypes.c_uint32*12), ('names', ctypes.c_char*48),
                    ('tail', ctypes.c_uint32*6), ('seconds', ctypes.c_uint64),
                    ('microseconds', ctypes.c_uint64)]
    info = BSDInfo()
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise ValueError('unreadable process birth')
    listing = run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], timeout=timeout)
    paths = [line[1:] for line in listing.stdout.splitlines() if line.startswith('n')]
    if listing.returncode or len(paths) != 1:
        raise ValueError('unreadable process cwd')
    return dict(argv=[os.fsdecode(arg) for arg in argv], executable=os.fsdecode(path.value),
                cwd=paths[0], birth=[info.seconds, info.microseconds],
                import_safe=import_environment_safe(entries[argc:]))


def rooted_processes(workspace):
    root = os.path.realpath(workspace)
    if root == '/':
        raise ValueError('unsafe workspace')
    if sys.platform == 'linux':
        rows = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdecimal():
                try:
                    rows.append((int(entry.name), os.readlink(entry / 'cwd')))
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue  # Same existing rooted-process visibility contract.
    elif sys.platform == 'darwin':
        listing = run(['lsof', '-a', '-d', 'cwd', '-Fpn'])
        if listing.returncode:
            raise ValueError('unreadable rooted processes')
        rows, pid = [], None
        for line in listing.stdout.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('n') and pid is not None:
                rows.append((pid, line[1:]))
    else:
        raise ValueError('unsupported rooted-process inspection')
    return {pid for pid, cwd in rows if cwd == root or cwd.startswith(root + '/')}


def static_preview(detail):
    argv = detail['argv']
    if (detail.get('import_safe') is not True or len(argv) < 3 or not re.fullmatch(r'[Pp]ython(?:3(?:\.\d+)?)?', Path(detail['executable']).name)
            or not re.fullmatch(r'[Pp]ython(?:3(?:\.\d+)?)?', Path(argv[0]).name)
            or argv[1:3] != ['-m', 'http.server'] or not os.path.isabs(detail['cwd'])):
        return False
    # Only Python's static server with an explicit literal loopback bind. No CGI,
    # TLS, custom modules, interpreter options, abbreviations or unknown flags.
    options, positional, seen = argv[3:], [], set()
    directory = detail['cwd']
    index = 0
    while index < len(options):
        token = options[index]
        if token in ('-b', '--bind', '-d', '--directory', '-p', '--protocol'):
            key = {'-b': 'bind', '--bind': 'bind', '-d': 'directory', '--directory': 'directory',
                   '-p': 'protocol', '--protocol': 'protocol'}[token]
            index += 1
            if index >= len(options) or key in seen:
                return False
            value = options[index]
            seen.add(key)
            if key == 'directory':
                directory = os.path.join(detail['cwd'], value)
            if ((key == 'bind' and value not in ('127.0.0.1', '::1', 'localhost'))
                    or (key == 'directory' and (not value or value.startswith('-')))
                    or (key == 'protocol' and value not in ('HTTP/1.0', 'HTTP/1.1'))):
                return False
        elif token.startswith('-'):
            return False
        else:
            positional.append(token)
        index += 1
    return (os.path.isdir(directory) and not (Path(detail['cwd']) / 'http.py').exists()
            and not (Path(detail['cwd']) / 'http').exists()
            and 'bind' in seen and len(positional) <= 1 and
            (not positional or positional[0].isascii() and positional[0].isdecimal()
             and 0 <= int(positional[0]) <= 65535))


def preview_listener(pid, detail):
    """Positive socket ownership and actual loopback bind, independent of argv."""
    if sys.platform == 'linux':
        root = Path('/proc') / str(pid)
        owned = set()
        for fd in (root / 'fd').iterdir():
            try:
                link = os.readlink(fd)
                if link.startswith('socket:['):
                    owned.add(link[8:-1])
            except FileNotFoundError:
                continue
        listeners = []
        for name, family in (('tcp', socket.AF_INET), ('tcp6', socket.AF_INET6)):
            for line in (root / 'net' / name).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] != '0A' or fields[9] not in owned:
                    continue
                address, port = fields[1].split(':')
                packed = b''.join(int(address[i:i+8], 16).to_bytes(4, sys.byteorder)
                                  for i in range(0, len(address), 8))
                listeners.append((socket.inet_ntop(family, packed), int(port, 16)))
    elif sys.platform == 'darwin':
        result = run(['lsof', '-nP', '-a', '-p', str(pid), '-iTCP', '-sTCP:LISTEN', '-Fn'])
        if result.returncode:
            return False
        listeners = []
        for line in result.stdout.splitlines():
            if line.startswith('n'):
                address, _, port = line[1:].rpartition(':')
                listeners.append((address.strip('[]'), int(port)))
    else:
        return False
    return bool(listeners) and all(address in ('127.0.0.1', '::1') and port > 0
                                   for address, port in listeners)


def process_candidates(table, agent_pid, workspace, inspector_pid):
    if (agent_pid not in table or table[agent_pid]['uid'] != os.geteuid()
            or table[agent_pid]['state'].startswith('Z')):
        raise ValueError('agent process changed')
    allowed = set()
    for root in (agent_pid, inspector_pid, os.getpid()):
        chain = set()
        while root > 1:
            if root in chain or root not in table:
                raise ValueError('unverified process ancestry')
            chain.add(root)
            allowed.add(root)
            root = table[root]['parent']
    descendants = {agent_pid}
    while True:
        expanded = descendants | {pid for pid, item in table.items() if item['parent'] in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    return {pid for pid in (descendants | rooted_processes(workspace)) - allowed
            if pid not in table or not table[pid]['state'].startswith('Z')}


def process_plan(agent_pid, workspace, inspector_pid):
    """All blockers are checked before any preview receives a signal."""
    table = process_table()
    agent_pid, inspector_pid = int(agent_pid), int(inspector_pid)
    candidates = process_candidates(table, agent_pid, workspace, inspector_pid)
    previews = {}
    for pid in candidates:
        item = table.get(pid)
        if (not item or item['uid'] != os.geteuid() or item['parent'] != agent_pid
                or any(child['parent'] == pid for child in table.values())):
            raise ValueError('background task or unknown process ancestry')
        detail = process_detail(pid)
        if not static_preview(detail) or not preview_listener(pid, detail):
            raise ValueError('background task is not a verified static preview')
        previews[pid] = dict(item, **detail)
    # Exact native birth stamps also freeze the agent, never just a reusable PID.
    agent = dict(table[agent_pid], **process_detail(agent_pid))
    after = process_table()
    if (process_candidates(after, agent_pid, workspace, inspector_pid) != candidates
            or any(after.get(pid) != table.get(pid) for pid in candidates | {agent_pid})):
        raise ValueError('process identity changed during inspection')
    for pid, item in [(agent_pid, agent), *previews.items()]:
        if process_detail(pid) != {key: item[key] for key in PROCESS_FIELDS}:
            raise ValueError('native process identity changed during inspection')
    return dict(agent=agent, previews=previews)


def git_completed(workspace):
    """Same read-only ancestry/squash/cache proof as _dev_kill_no_pending_work."""
    def git(*args):
        value = run(['git', '-C', workspace, *args])
        if value.returncode:
            return None
        return value.stdout.strip()
    if git('status', '--porcelain', '--untracked-files=all') != '':
        return False
    head = git('rev-parse', '--verify', 'HEAD')
    if not head:
        return False
    target = git('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')
    main = None
    if target and target.startswith('refs/remotes/origin/') and target != 'refs/remotes/origin/HEAD':
        main = git('rev-parse', '--verify', target + '^{commit}')
    if not main:
        for target in ('refs/remotes/origin/main', 'refs/remotes/origin/master'):
            main = git('rev-parse', '--verify', target + '^{commit}')
            if main:
                break
    if main:
        if git('merge-base', '--is-ancestor', head, main) is not None:
            return True
        base, tree = git('merge-base', head, main), git('rev-parse', 'HEAD^{tree}')
        trees = git('log', '--first-parent', '--format=%T', f'{base}..{main}') if base else None
        if tree and trees and tree in trees.splitlines():
            return True
    origin = git('remote', 'get-url', 'origin') or ''
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?', origin)
    if match:
        cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'claude-sessions/pr'
        for file in cache.glob(match[1].replace('/', '#') + '#*.head'):
            try:
                if file.read_text().strip() == head and file.with_suffix('').read_text().strip() == 'MERGED':
                    return True
            except OSError:
                continue
    return False


def preview_running(pid, birth):
    """Nonblocking native exit check; subprocess timeouts cannot extend TERM wait."""
    if sys.platform == 'linux':
        try:
            fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            return False
        if fields[19] != birth:
            raise ValueError('preview PID reused after TERM')
        return fields[0] != 'Z'
    if sys.platform == 'darwin':
        class BSDInfo(ctypes.Structure):
            _fields_ = [('header', ctypes.c_uint32*12), ('names', ctypes.c_char*48),
                        ('tail', ctypes.c_uint32*6), ('seconds', ctypes.c_uint64),
                        ('microseconds', ctypes.c_uint64)]
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        info = BSDInfo()
        result = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if not result and ctypes.get_errno() in (2, 3):  # ENOENT, ESRCH
            return False
        if result != ctypes.sizeof(info):
            raise ValueError('unknown preview exit state')
        if [info.seconds, info.microseconds] != birth:
            raise ValueError('preview PID reused after TERM')
        return info.header[1] != 5  # SZOMB
    raise ValueError('unsupported preview exit inspection')


def conversation_mark(socket, identity, pane, agent_pid):
    """Freeze existing owner evidence; no new agent status or completion record."""
    title = tmux(socket, 'display-message', '-p', '-t', pane, '#{pane_title}')
    stamp = tmux(socket, 'show-environment', '-t', identity + ':', 'CLAUDE_RESUME_ID')
    if title.returncode:
        raise ValueError('unreadable conversation title')
    registry = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'claude-sessions' / str(agent_pid)
    try:
        before = registry.stat()
        with registry.open('rb') as stream:
            data = stream.read(8193)
        after = registry.stat()
        stable = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(data) > 8192 or stable(before) != stable(after):
            raise ValueError('owner registry changed during inspection')
        registered = (after.st_ino, after.st_size, after.st_mtime_ns, hashlib.sha256(data).hexdigest())
    except FileNotFoundError:
        registered = None
    return title.stdout, stamp.returncode, stamp.stdout, registered


def stop_previews(check_args, agent_pid, inspector_pid):
    socket, identity, pane, pane_pid, agent, transcript = check_args[:6]
    stopped = []
    try:
        current = owner(socket, identity, pane, pane_pid)
        if not current:
            raise ValueError('owner changed before preview cleanup')
        workspace = current['workspace']
        baseline = revision(workspace, transcript)
        conversation = conversation_mark(socket, identity, pane, agent_pid)
        screen = tmux(socket, 'capture-pane', '-p', '-e', '-t', pane)
        if baseline is None or screen.returncode:
            raise ValueError('unknown task before preview cleanup')
        planned = process_plan(agent_pid, workspace, inspector_pid)
        def recheck(expected):
            if not git_completed(workspace):
                raise ValueError('pending or unverified Git work')
            decision = eligibility(*check_args)
            if decision['decision'] != 'terminate':
                raise ValueError(decision['reason'])
            live = process_plan(agent_pid, workspace, inspector_pid)
            visible = tmux(socket, 'capture-pane', '-p', '-e', '-t', pane)
            if (live != expected or owner(socket, identity, pane, pane_pid) != current
                    or revision(workspace, transcript) != baseline or visible.returncode
                    or visible.stdout != screen.stdout
                    or conversation_mark(socket, identity, pane, agent_pid) != conversation):
                raise ValueError('owner, task or process changed before preview cleanup')
        for pid in list(planned['previews']):
            recheck(planned)
            # A final exact native read narrows the PID-reuse window after slow PR checks.
            if process_detail(pid) != {key: planned['previews'][pid][key] for key in
                                      PROCESS_FIELDS}:
                raise ValueError('preview PID changed before TERM')
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
            deadline = time.monotonic() + 5
            while preview_running(pid, planned['previews'][pid]['birth']):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('preview did not exit after TERM')
                time.sleep(min(.05, remaining))
            del planned['previews'][pid]
        recheck(planned)
        if planned['previews']:
            raise ValueError('preview cleanup incomplete')
        print(json.dumps(dict(reason='verified previews exited', preview_term_sent=stopped)), file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        print(json.dumps(dict(reason=str(error), preview_term_sent=stopped)), file=sys.stderr)
        return 1


def eligibility(socket, identity, pane, pane_pid, agent, transcript, fresh, git_clean, processes_clear, sid=''):
    current = owner(socket, identity, pane, pane_pid)
    if not current:
        return {'decision': 'preserve', 'reason': 'owner changed or attached'}
    if git_clean != '1' or processes_clear != '1':
        return {'decision': 'preserve', 'reason': 'pending work or unknown process/Git state'}
    before = revision(current['workspace'], transcript)
    if before is None:
        return {'decision': 'unknown', 'reason': 'unverified revision'}
    screen = tmux(socket, 'capture-pane', '-p', '-e', '-t', pane)
    empty = None if screen.returncode else composer_empty(agent, screen.stdout, current['cursor'])
    if empty is not True:
        return {'decision': 'preserve' if empty is False else 'unknown', 'reason': 'draft or unrecognized composer'}
    state, prs = transcript_state(agent, transcript)
    # Only a fresh lifecycle launch can prove a no-transcript startup. A missing
    # resumed transcript or a missing session ID never constitutes that proof.
    if (state == 'unknown' and not transcript and fresh == '1'
            and startup_screen(agent, screen.stdout, current['cursor'])):
        state = 'empty'
    if state not in ('idle', 'empty'):
        return {'decision': 'preserve' if state == 'busy' else 'unknown', 'reason': 'unfinished or unknown turn'}
    retained = history_prs(agent, sid)
    if retained is None:
        return {'decision': 'unknown', 'reason': 'unreadable PR associations'}
    complete = prs_complete(current['workspace'], prs | retained)
    if complete is not True:
        return {'decision': 'preserve' if complete is False else 'unknown', 'reason': 'open or unverified PR'}
    # Include the screen and transcript identity in the final observation. The
    # shell performs this check again inside lifecycle stop before any signal.
    after_screen = tmux(socket, 'capture-pane', '-p', '-e', '-t', pane)
    if (owner(socket, identity, pane, pane_pid) != current or after_screen.returncode
            or after_screen.stdout != screen.stdout or revision(current['workspace'], transcript) != before):
        return {'decision': 'preserve', 'reason': 'owner changed during inspection'}
    return {'decision': 'terminate', 'reason': 'empty or completed session'}


def detached(socket, identity, pane, pane_pid, tty, terminal_fd):
    if not (socket.startswith('/') and re.fullmatch(r'\$\d+', identity)
            and re.fullmatch(r'%\d+', pane) and pane_pid.isdigit()):
        return 1
    # Use the original descriptor, never a device name which may be recycled.
    lost = hung_up(terminal_fd)
    current = owner(socket, identity, pane, pane_pid)
    if not lost or not current:
        receipt(socket, identity, {'decision': 'preserve', 'reason': 'terminal still usable' if not lost else 'owner changed or attached'})
        return 0
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env['T_HOME'] = str(ROOT)
    # Source repository helpers explicitly: no personal dotfiles, no source
    # selector which could redirect this event into another development tree.
    script = '''
setopt no_monitor no_notify
autoload -Uz add-zsh-hook
zmodload zsh/datetime
zmodload -F zsh/stat b:zstat
source "$T_HOME/ui/fzf.sh"
for module in config agent worktree sessions remote resume lifecycle; do
  source "$T_HOME/zsh/$module.zsh" || exit
done
_t_terminal_close_event "$@"
'''
    result = run(['zsh', '-f', '-c', script, 't-terminal-close', socket, identity,
                  pane, pane_pid], env=env, timeout=90)
    receipt(socket, identity, {'decision': 'terminate' if result.returncode == 0 else 'preserve',
                              'reason': result.stderr[-2000:], 'code': result.returncode})
    return result.returncode


def receipt(socket, identity, value):
    root = event_path(socket, identity).parent
    key = hashlib.sha256((socket + identity).encode()).hexdigest()
    atomic_write(root / (key + '.json'), json.dumps(value))


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
    try:
        os.replace(stream.name, path)
    finally:
        Path(stream.name).unlink(missing_ok=True)


def main(args):
    if len(args) == 2 and args[0] == 'install':
        return install(args[1])
    if len(args) == 6 and args[0] == 'detached':
        return notify_detached(*args[1:])
    if len(args) in (3, 4) and args[0] == 'attached':
        return attached(*args[1:])
    if len(args) == 6 and args[0] == 'watch':
        return watch(*args[1:])
    if len(args) == 7 and args[0] == 'watch-resume':
        return watch(*args[1:])
    if len(args) == 4 and args[0] in ('processes', 'processes-empty'):
        try:
            planned = process_plan(*args[1:])
            return int(args[0] == 'processes-empty' and bool(planned['previews']))
        except (OSError, ValueError, KeyError, TypeError, SyntaxError):
            return 1
    if len(args) == 13 and args[0] == 'stop-previews':
        return stop_previews(args[1:11], *args[11:])
    if len(args) == 11 and args[0] == 'check':
        result = eligibility(*args[1:])
        print(json.dumps(result))
        return 0 if result['decision'] == 'terminate' else 1
    return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
