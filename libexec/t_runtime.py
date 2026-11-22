"""Capability-based reconciliation of t-owned live runtime components.

Receipts are retry evidence, never ownership. tmux, native process identity and
cooperative acknowledgements establish each target again on every update.
"""
import fcntl
import hashlib
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = 1
FILES = {
    'terminalhooks': ('libexec/t_terminal_close.py', 'libexec/t_runtime_dispatch.py'),
    'terminalobserver': ('libexec/t_terminal_close.py', 'libexec/t_runtime.py', 'libexec/t_runtime_dispatch.py'),
    'recoverywatcher': ('libexec/t_recovery.py', 'libexec/t_runtime.py', 'libexec/t_runtime_dispatch.py'),
    'discoverydaemon': ('libexec/t_session_cache.py', 'libexec/t_session_discovery.py'),
}


def private_root():
    root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/runtime'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe runtime state directory')
    return root


def write(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('unsafe runtime record')
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value if isinstance(value, str) else json.dumps(value))
    try:
        os.replace(stream.name, path)
    finally:
        Path(stream.name).unlink(missing_ok=True)


def read(path):
    try:
        path = Path(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def stable_source(source):
    source = Path(source).resolve()
    try:
        marker = json.loads((source / '.t-homebrew').read_text())
        opt = Path(marker['opt_libexec'])
        if marker.get('formula') == 'agenthangar/tap/t' and opt.is_absolute() and opt.resolve() == source:
            return str(opt)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return str(source)


def fingerprint(component, source=ROOT):
    source = Path(source)
    digest = hashlib.sha256()
    for relative in FILES[component]:
        value = (source / relative).read_bytes()
        if relative.endswith('.py'):
            compile(value, str(source / relative), 'exec')
        digest.update(relative.encode() + b'\0' + value + b'\0')
    return digest.hexdigest()


def validate_source(source, component, expected=None):
    source = Path(source)
    if (not source.is_absolute() or source.stat().st_uid != os.getuid()
            or (source / '.t-install-version').read_text().strip() != '1'):
        raise ValueError('unverified replacement source')
    value = fingerprint(component, source)
    # Protocol declarations are data, not code imported from a candidate source.
    import ast
    for relative in ('libexec/t_runtime.py', 'libexec/t_runtime_dispatch.py'):
        tree = ast.parse((source / relative).read_bytes())
        protocols = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'PROTOCOL' for target in node.targets)
                     and isinstance(node.value, ast.Constant)]
        if protocols != [PROTOCOL]:
            raise ValueError('incompatible replacement runtime protocol')
    if component in ('terminalhooks', 'terminalobserver', 'recoverywatcher'):
        helper = 'libexec/t_recovery.py' if component == 'recoverywatcher' else 'libexec/t_terminal_close.py'
        tree = ast.parse((source / helper).read_bytes())
        capability = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'RUNTIME_PROTOCOL' for target in node.targets)
                      and isinstance(node.value, ast.Constant)]
        if capability != [PROTOCOL]:
            raise ValueError('incompatible helper reload protocol')
    if expected is not None and value != expected:
        raise ValueError('replacement source changed')
    return value


def dispatcher(source=ROOT):
    installed = private_root() / 'dispatch.py'
    # Development helpers remain pinned and never replace a shared executable.
    return installed if installed.is_file() else Path(source) / 'libexec/t_runtime_dispatch.py'


def bootstrap(source=ROOT, previous_sources=()):
    source = Path(source).resolve()
    if (source / '.git').is_file():
        raise ValueError('development runtimes cannot replace the installed dispatcher')
    validate_source(source, 'terminalobserver')
    path = private_root() / 'installation.json'
    previous = read(path)
    roots = [str(source), *map(str, previous_sources)]
    if previous.get('stable_source') == stable_source(source):
        roots += previous.get('sources', [])
    write(private_root() / 'dispatch.py', (source / 'libexec/t_runtime_dispatch.py').read_text())
    write(path, dict(protocol=PROTOCOL, stable_source=stable_source(source), sources=list(dict.fromkeys(roots))))


def command(argv, timeout=3):
    try:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              cwd='/', timeout=timeout, env={**os.environ, 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 1, '', str(error))


def tmux(socket, *args):
    return command(['tmux', '-S', str(socket), *args])


def module(name):
    return runpy.run_path(str(ROOT / 'libexec' / name))


def native(pid, deadline=None):
    helper = module('t_terminal_close.py')
    def budget():
        left = 3 if deadline is None else min(3, deadline - time.monotonic())
        if left <= 0:
            raise ValueError('runtime inspection deadline reached')
        return left
    table = helper['process_table'](timeout=budget())
    item = table.get(int(pid))
    if not item or item['uid'] != os.getuid() or item['state'].startswith('Z'):
        raise ValueError('runtime process exited or changed user')
    detail = helper['process_detail'](int(pid), timeout=budget())
    return dict(pid=int(pid), birth=detail['birth'], argv=detail['argv'], executable=detail['executable'])


def fd_identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino, info.st_mode, info.st_rdev]


def publish(path, component, socket, token=None, **extra):
    identity = native(os.getpid())
    value = dict(protocol=PROTOCOL, component=component, socket=socket,
                 source=str(ROOT), fingerprint=fingerprint(component), token=token or uuid.uuid4().hex,
                 pid=identity['pid'], birth=identity['birth'], **extra)
    write(path, value)
    return value


def live_record(record, component, target=None, deadline=None):
    if record.get('protocol') != PROTOCOL or record.get('component') != component:
        return False
    try:
        identity = native(record['pid'], deadline)
        argv = identity['argv']
        if identity['birth'] != record['birth'] or len(argv) < 4:
            return False
        if not re.fullmatch(r'[Pp]ython(?:3(?:\.\d+)?)?', Path(identity['executable']).name):
            return False
        relative = 'libexec/t_terminal_close.py' if component == 'terminalobserver' else 'libexec/t_recovery.py'
        if argv[1].endswith('/' + relative):
            source, rest = str(Path(argv[1]).parents[1]), argv[2:]
        elif Path(argv[1]) == private_root() / 'dispatch.py' and len(argv) > 4 and argv[3] == relative:
            source, rest = argv[2], argv[4:]
        else:
            return False
        if os.path.realpath(source) != os.path.realpath(record['source']):
            return False
        socket = record['socket']
        if target and socket != target[1]:
            return False
        if component == 'terminalobserver':
            expected = [socket, record['tty'], record['client_pid'], str(record['descriptor']), str(record['lock_descriptor'])]
            if not ((rest[0] == 'watch' and rest[1:] == expected)
                    or (rest[0] == 'watch-resume' and len(rest) == 7 and rest[1:-1] == expected)):
                return False
            if target and target[2] and (record['tty'] != target[2]['tty'] or record['client_pid'] != target[2]['pid']):
                return False
            if target:
                base = str(target[0]).removesuffix('.runtime.json')
                lockpath = Path(base + '.lock')
                fd = os.open(lockpath, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    if fd_identity(fd) != record['lock_fd']:
                        return False
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        return False
                finally:
                    os.close(fd)
            if sys.platform == 'linux':
                for number, key in ((record['descriptor'], 'terminal_fd'), (record['lock_descriptor'], 'lock_fd')):
                    info = os.stat(f"/proc/{record['pid']}/fd/{number}")
                    if [info.st_dev, info.st_ino, info.st_mode, info.st_rdev] != record[key]:
                        return False
            return True
        return rest == ['watch', socket] or rest == ['watch-resume', socket, record['token']]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def request_reload(path, record, source, component):
    value = dict(protocol=PROTOCOL, source=stable_source(source), fingerprint=fingerprint(component, source),
                 token=record['token'], pid=record['pid'], birth=record['birth'])
    write(path, value)
    return value


def replacement(path, record, component):
    request = read(path)
    if not request:
        return None
    if any(request.get(key) != record.get(key) for key in ('protocol', 'token', 'pid', 'birth')):
        raise ValueError('runtime reload owner changed')
    installed = read(private_root() / 'installation.json')
    if request.get('source') != installed.get('stable_source'):
        raise ValueError('replacement is not the installed runtime source')
    # Worktree-pinned helpers never cross into the shared source.
    if (Path(record['source']) / '.git').is_file():
        raise ValueError('development runtime is pinned')
    validate_source(request['source'], component, request.get('fingerprint'))
    if request['fingerprint'] == record['fingerprint'] and os.path.realpath(request['source']) == record['source']:
        Path(path).unlink(missing_ok=True)
        return None
    return request


def owned_socket(path):
    try:
        path = Path(path)
        info, parent = path.lstat(), path.parent.lstat()
        return (path.is_absolute() and stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
                and not parent.st_mode & 0o022)
    except OSError:
        return False


def socket_hints():
    hints = set()
    if os.environ.get('TMUX'):
        hints.add(os.environ['TMUX'].split(',')[0])
    default = command(['tmux', 'display-message', '-p', '#{socket_path}'])
    if default.returncode == 0:
        hints.add(default.stdout.strip())
    for parent in {Path('/tmp'), Path(os.environ.get('TMUX_TMPDIR') or '/tmp')}:
        directory = parent / ('tmux-' + str(os.getuid()))
        if directory.is_dir() and not directory.is_symlink():
            hints.update(map(str, directory.iterdir()))
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/terminal-close'
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 't/recovery'
    for path in [*state.glob('*.runtime.json'), *cache.glob('*/watch.runtime.json')]:
        hint = read(path).get('socket')
        if isinstance(hint, str):
            hints.add(hint)
    return sorted(hint for hint in hints if owned_socket(hint))


def panes(socket):
    result = tmux(socket, 'list-panes', '-a', '-F',
                  '#{session_id}|#{session_name}|#{pane_id}|#{pane_pid}|#{session_path}|#{pane_dead}')
    if result.returncode:
        raise ValueError('tmux server changed')
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split('|')
        if (len(fields) == 6 and re.fullmatch(r'\$\d+', fields[0])
                and fields[1].startswith('dev-') and re.fullmatch(r'%\d+', fields[2])
                and fields[3].isdigit() and fields[5] == '0'):
            rows.append(dict(zip(('identity', 'session', 'pane', 'pane_pid', 'workspace', 'dead'), fields)))
    # Multi-pane sessions aren't a single verified t owner.
    return [row for row in rows if sum(other['identity'] == row['identity'] for other in rows) == 1]


def parsed_hook(line, socket, pane):
    """Recognize only t's exact hook grammar; filenames in user code are not ownership."""
    try:
        values = shlex.split(line)
        key, verb = values[0], values[1]
        options = ['-b'] if key == 'client-detached[50100]' else []
        if key not in ('client-detached[50100]', 'client-attached[50100]', 'client-session-changed[50100]'):
            return None
        if verb != 'run-shell' or values[2:-1] != options:
            return None
        suffix = ' #{q:hook_client} >/dev/null 2>&1 || :'
        if not values[-1].endswith(suffix):
            return None
        argv = shlex.split(values[-1][:-len(suffix)])
        if not argv or not re.fullmatch(r'[Pp]ython(?:3(?:\.\d+)?)?', Path(argv[0]).name):
            return None
        if len(argv) > 2 and argv[1].endswith('/libexec/t_terminal_close.py'):
            source, rest = str(Path(argv[1]).parents[1]), argv[2:]
        elif len(argv) > 4 and Path(argv[1]).name == 'dispatch.py' and argv[3] == 'libexec/t_terminal_close.py':
            if Path(argv[1]) != private_root() / 'dispatch.py':
                return None
            source, rest = argv[2], argv[4:]
        else:
            return None
        expected = (['detached', socket, pane['identity'], pane['pane'], pane['pane_pid']]
                    if options else ['attached', socket])
        return dict(key=key, source=source, argv=argv) if rest == expected else None
    except (ValueError, IndexError):
        return None


def hook_source(socket, pane):
    hooks = tmux(socket, 'show-hooks', '-t', pane['identity'] + ':')
    for line in hooks.stdout.splitlines():
        value = parsed_hook(line, socket, pane)
        if value and value['key'] == 'client-detached[50100]':
            return value['source']
    option = tmux(socket, 'show-options', '-qv', '-t', pane['identity'] + ':', '@t_runtime_source')
    return option.stdout.strip() if option.returncode == 0 else ''


def same_owner(socket, pane, source):
    if pane not in panes(socket):
        return False
    script = '''setopt no_monitor no_notify
    autoload -Uz add-zsh-hook
    zmodload zsh/datetime
    zmodload -F zsh/stat b:zstat
    source "$T_HOME/ui/fzf.sh" || exit
    for part in config agent worktree sessions; do source "$T_HOME/zsh/$part.zsh" || exit; done
    tmux() { command tmux -S "$1_socket" "$@"; }
    '''
    # Use an explicit socket variable, never an inherited/default server.
    script = script.replace('"$1_socket"', shlex.quote(socket))
    script += '\ns=' + shlex.quote(pane['session']) + '\np=$(_dev_session_claude_pid "$s")\na=$(_dev_agent_of_session "$s")\n[[ -n $p && $a == (codex|claude) ]] || exit 1\nprint -r -- "$p"\n'
    result = command(['zsh', '-f', '-c', script], timeout=5) if not source else command(
        ['env', 'T_HOME=' + str(source), 'zsh', '-f', '-c', script], timeout=5)
    if result.returncode or not result.stdout.strip().isdigit():
        return False
    try:
        native(result.stdout.strip())
    except (OSError, ValueError, TypeError, SyntaxError):
        return False
    return pane in panes(socket)


def source_owned(source, roots):
    if not source or (Path(source) / '.git').is_file():
        return False
    return os.path.realpath(source) in roots or source in roots


def outcome(status, reason='', **extra):
    return dict(status=status, reason=reason, **extra)


class Hooks:
    name = 'terminalhooks'

    def inspect(self, context, target):
        socket, pane = target
        helper = module('t_terminal_close.py')
        expected = helper['hook_commands'](socket, pane, stable_source(ROOT))
        current = tmux(socket, 'show-hooks', '-t', pane['identity'] + ':')
        if current.returncode:
            raise ValueError('unreadable session hooks')
        all_current = True
        for hook, wanted in expected.items():
            rows = [line for line in current.stdout.splitlines() if line.startswith(hook + ' ')]
            if rows and parsed_hook(rows[0], socket, pane) is None:
                return outcome('deferred', 'reserved hook index contains a user hook')
            if not rows or shlex.split(rows[0])[1:] != shlex.split(wanted):
                all_current = False
        if all_current:
            return outcome('current')
        return outcome('updated')

    def apply(self, context, target):
        socket, pane = target
        if not same_owner(socket, pane, ROOT):
            raise ValueError('session owner changed before hook refresh')
        helper = module('t_terminal_close.py')
        if helper['install'](pane['session'], socket=socket, expected=pane, source=stable_source(ROOT)):
            raise ValueError('session hook refresh failed')

    def verify(self, context, target, before):
        return self.inspect(context, target)['status'] == 'current'


class Worker:
    def __init__(self, name):
        self.name = name

    def inspect(self, context, target):
        path, socket, client = target
        record = read(path)
        if record and live_record(record, self.name, target):
            if not source_owned(record.get('source'), context['roots']):
                return outcome('deferred', 'runtime source is pinned or unrelated')
            if record.get('fingerprint') == fingerprint(self.name) and os.path.realpath(record['source']) == str(ROOT):
                return outcome('current', record=record)
            return outcome('updated', record=record)
        if self.name == 'terminalobserver':
            base = Path(str(path).removesuffix('.runtime.json'))
            lockpath = Path(str(base) + '.lock')
            if lockpath.exists():
                fd = os.open(lockpath, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return outcome('deferred', 'legacy observer retains its original terminal until normal attachment')
                finally:
                    os.close(fd)
            if not client:
                return outcome('deferred', 'original terminal descriptor is unavailable')
        else:
            legacy = legacy_workers(socket)
            if legacy:
                missing = any(not Path(arg).is_file() for item in legacy for arg in item['argv']
                              if arg.endswith('/libexec/t_recovery.py'))
                return outcome('restart-required' if missing else 'deferred',
                    'legacy recovery watcher source is unavailable; normal session lifecycle must replace it' if missing
                    else 'legacy recovery watcher retains its normal safe-boundary reload')
            if os.environ.get('T_RECOVERY_DISABLE'):
                return outcome('current', 'recovery is disabled')
        return outcome('updated', record={})

    def apply(self, context, target):
        path, socket, client = target
        record = read(path)
        if record and live_record(record, self.name, target):
            request_reload(Path(str(path).replace('.runtime.json', '.reload.json')), record, ROOT, self.name)
        elif self.name == 'terminalobserver':
            if not client:
                raise ValueError('original observer descriptor unavailable')
            helper = module('t_terminal_close.py')
            if helper['attached'](socket, client['tty'], client['pid']):
                raise ValueError('could not arm verified attached terminal')
        else:
            pane = context['panes'][socket][0]
            if not same_owner(socket, pane, ROOT):
                raise ValueError('recovery owner changed')
            if module('t_recovery.py')['start'](pane['session'], socket=socket):
                raise ValueError('could not start required recovery watcher')

    def verify(self, context, target, before):
        path, socket, client = target
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            record = read(path)
            if (live_record(record, self.name, target, deadline) and record.get('fingerprint') == fingerprint(self.name)
                    and os.path.realpath(record['source']) == str(ROOT)
                    and (not before.get('record') or all(record.get(key) == before['record'].get(key)
                         for key in ('pid', 'birth', 'token', 'terminal_fd', 'lock_fd')))):
                return True
            time.sleep(.05)
        return False


def legacy_workers(socket, component='recoverywatcher'):
    filename = 't_recovery.py' if component == 'recoverywatcher' else 't_terminal_close.py'
    result = command(['ps', '-axo', 'pid=,uid=,args='])
    found = []
    deadline = time.monotonic() + 3
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[1] == str(os.getuid()) and filename in fields[2] and socket in fields[2]:
            try:
                value = native(fields[0], deadline)
                args = value['argv']
                if len(args) < 4 or not args[1].endswith('/libexec/' + filename):
                    continue
                rest = args[2:]
                if component == 'recoverywatcher' and rest == ['watch', socket]:
                    found.append(value)
                elif (component == 'terminalobserver' and len(rest) == 6 and rest[:2] == ['watch', socket]
                      and re.fullmatch(r'/dev/(?:pts/\d+|tty[\w.-]+)', rest[2])
                      and all(item.isdecimal() for item in rest[3:])):
                    found.append(dict(value, source=str(Path(args[1]).parents[1]), tty=rest[2], client_pid=rest[3]))
            except (OSError, ValueError, TypeError, SyntaxError):
                continue
    return found


class Discovery:
    name = 'discoverydaemon'

    def inspect(self, context, target):
        return outcome('updated')

    def apply(self, context, target):
        helper = module('t_session_cache.py')
        context['discovery_result'] = helper['retire_obsolete'](context['previous_sources'], report=True)

    def verify(self, context, target, before):
        return context['discovery_result']['status'] not in ('failed', 'deferred')


ADAPTERS = (Hooks(), Worker('terminalobserver'), Worker('recoverywatcher'), Discovery())


def reconcile(previous_sources=(), bootstrap_install=False):
    if (ROOT / '.git').is_file():
        return dict(protocol=PROTOCOL, source=str(ROOT), results=[dict(component='source', status='deferred',
                    reason='development runtime remains pinned')], failed=False)
    bootstrap(ROOT, previous_sources)
    installed = read(private_root() / 'installation.json')
    roots = {str(ROOT), stable_source(ROOT), *installed.get('sources', []), *map(str, previous_sources)}
    context = dict(roots=roots, previous_sources=list(previous_sources), panes={})
    targets = {adapter.name: [] for adapter in ADAPTERS}
    results = []
    for socket in socket_hints():
        eligible = []
        try:
            observed = panes(socket)
        except ValueError:
            results.append(dict(component='inventory', target=socket, status='deferred',
                                reason='unreachable private tmux socket was preserved', protocol=PROTOCOL))
            continue
        for pane in observed:
            source = hook_source(socket, pane)
            if not source:
                results.append(dict(component='terminalhooks', target=pane['identity'], status='deferred',
                                    reason='runtime source cannot be verified; use normal attachment', protocol=PROTOCOL))
            if source_owned(source, roots) and same_owner(socket, pane, ROOT):
                eligible.append(pane)
                targets['terminalhooks'].append((socket, pane))
        if not eligible:
            continue
        context['panes'][socket] = eligible
        clients = tmux(socket, 'list-clients', '-F', '#{client_tty}|#{client_pid}|#{session_id}')
        seen = set()
        terminal = module('t_terminal_close.py')
        for line in clients.stdout.splitlines():
            parts = line.split('|')
            if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {pane['identity'] for pane in eligible}:
                continue
            try:
                native(parts[1])
            except (OSError, ValueError, TypeError, SyntaxError):
                continue
            base = terminal['event_path'](socket, parts[0])
            path = Path(str(base) + '.runtime.json')
            targets['terminalobserver'].append((path, socket, dict(tty=parts[0], pid=parts[1])))
            seen.add(path)
        state = terminal['event_path'](socket, '').parent
        for path in state.glob('*.runtime.json'):
            record = read(path)
            if record.get('socket') == socket and path not in seen and source_owned(record.get('source'), roots):
                targets['terminalobserver'].append((path, socket, None))
        # Detached legacy observers have no protocol record but may still own
        # the original terminal handle. Native exact watch argv is only a hint
        # to report deferral; it never authorizes takeover or a synthetic event.
        for legacy in legacy_workers(socket, 'terminalobserver'):
            if source_owned(legacy['source'], roots):
                base = terminal['event_path'](socket, legacy['tty'])
                path = Path(str(base) + '.runtime.json')
                if path not in seen:
                    targets['terminalobserver'].append((path, socket, None))
                    seen.add(path)
        enabled = []
        for pane in eligible:
            environment = tmux(socket, 'show-environment', '-t', pane['identity'] + ':')
            if environment.returncode:
                results.append(dict(component='recoverywatcher', target=socket, status='deferred',
                                    reason='session recovery setting could not be verified', protocol=PROTOCOL))
                continue
            values = dict(line.split('=', 1) for line in environment.stdout.splitlines() if '=' in line)
            if not values.get('T_RECOVERY_DISABLE'):
                enabled.append(pane)
        if not os.environ.get('T_RECOVERY_DISABLE') and enabled:
            context['panes'][socket] = enabled
            recovery = module('t_recovery.py')
            targets['recoverywatcher'].append((recovery['cache'](socket) / 'watch.runtime.json', socket, None))
    targets['discoverydaemon'].append(None)
    for adapter in ADAPTERS:
        for target in targets[adapter.name]:
            key = str(target[0]) if target else str(ROOT)
            try:
                before = adapter.inspect(context, target)
                status, reason = before['status'], before.get('reason', '')
                if status == 'updated':
                    adapter.apply(context, target)
                    verified = adapter.verify(context, target, before)
                    if adapter.name == 'discoverydaemon':
                        status = context['discovery_result']['status']
                        reason = context['discovery_result'].get('reason', '')
                    elif not verified:
                        status, reason = 'failed', 'runtime adoption was not acknowledged; retry t update'
                results.append(dict(component=adapter.name, target=key, status=status, reason=reason,
                                    fingerprint=fingerprint(adapter.name), protocol=PROTOCOL))
            except (OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError) as error:
                results.append(dict(component=adapter.name, target=key, status='failed', reason=str(error), protocol=PROTOCOL))
    value = dict(protocol=PROTOCOL, source=str(ROOT), results=results,
                 failed=any(result['status'] == 'failed' for result in results))
    write(private_root() / 'last-reconcile.json', value)
    return value
