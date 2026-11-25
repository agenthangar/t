"""Manage the current user's owned background services."""
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import re
import runpy
import shutil
import subprocess
import sys
from contextlib import contextmanager
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLEANUP_LABEL = 'com.agenthangar.t.cleanup'
NAMES = {'cleanup': 'Finished session cleanup', 'recovery': 'Automatic recovery'}


def state():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/background'


def recovery():
    return runpy.run_path(str(ROOT / 'libexec/t_auto_recovery.py'))


def specification(agent):
    if agent == 'cleanup':
        path = Path.home() / 'Library/LaunchAgents' / (CLEANUP_LABEL + '.plist')
        return CLEANUP_LABEL, path
    api = recovery()
    return api['LABEL'], api['service_path']()


def run(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True, timeout=8)


def domain():
    return f'gui/{os.getuid()}'


def read_service(agent):
    label, path = specification(agent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_size > 65536:
        raise ValueError('unverified LaunchAgent file')
    document = plistlib.loads(path.read_bytes())
    if not isinstance(document, dict) or document.get('Label') != label:
        raise ValueError('unverified LaunchAgent label')
    args = document.get('ProgramArguments')
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError('unverified LaunchAgent command')
    if agent == 'cleanup':
        if (len(args) != 3 or not Path(args[0]).is_absolute() or
                not Path(args[1]).is_absolute() or args[2] != 'watch' or
                Path(args[1]).parts[-2:] != ('libexec', 't_close_queue.py')):
            raise ValueError('unverified cleanup LaunchAgent command')
    else:
        expected = recovery()['root']() / 't Recovery.app/Contents/MacOS/t-recovery'
        if args != [str(expected)]:
            raise ValueError('unverified recovery LaunchAgent command')
    return document


def launch_state(agent):
    label, _ = specification(agent)
    value = run('print', domain() + '/' + label)
    if value.returncode:
        detail = value.stderr or value.stdout
        if re.search(r'could not find service|service not found|no such process', detail, re.I):
            return 'Stopped', False
        return 'Unverified', False
    running = (re.search(r'^\s*state = running\s*$', value.stdout, re.M)
               and re.search(r'^\s*pid = [1-9][0-9]*\s*$', value.stdout, re.M))
    return ('Running' if running else 'Stopped'), True


def login_state(agent, document):
    label, _ = specification(agent)
    if document.get('Disabled') is True or document.get('RunAtLoad') is not True:
        return 'Disabled'
    commands = document['ProgramArguments'][:2]
    if not all(Path(arg).is_file() for arg in commands) or not os.access(commands[0], os.X_OK):
        return 'Unverified'
    value = run('print-disabled', domain())
    if value.returncode or not re.search(r'disabled services\s*=\s*\{', value.stdout):
        return 'Unverified'
    override = re.search(r'^\s*"?'+ re.escape(label) + r'"?\s*=>\s*(\w+)\s*$', value.stdout, re.M)
    if not override or override[1] in ('enabled', 'false'):
        return 'Enabled'
    return 'Disabled' if override[1] in ('disabled', 'true') else 'Unverified'


def status(agent):
    if sys.platform != 'darwin':
        return ('Unavailable', 'Unavailable', 'Unavailable')
    try:
        document = read_service(agent)
        if document is None:
            return ('Not installed', 'Stopped', 'Disabled')
        return ('Installed', launch_state(agent)[0], login_state(agent, document))
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return ('Unverified', 'Unverified', 'Unverified')


def ensure(result):
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or 'launchctl failed')


def atomic_plist(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        plistlib.dump(document, stream)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def start(agent):
    label, path = specification(agent)
    runtime, loaded = launch_state(agent)
    if runtime == 'Unverified':
        raise ValueError('could not verify launchd state')
    if not loaded:
        ensure(run('bootstrap', domain(), str(path)))
    elif runtime != 'Running':
        ensure(run('kickstart', domain() + '/' + label))
    (state() / (agent + '.stopped')).unlink(missing_ok=True)


def stop(agent):
    label, _ = specification(agent)
    runtime, loaded = launch_state(agent)
    if runtime == 'Unverified':
        raise ValueError('could not verify launchd state')
    marker = state() / (agent + '.stopped')
    descriptor = os.open(marker, os.O_NOFOLLOW | os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        if loaded:
            ensure(run('bootout', domain() + '/' + label))
    except (OSError, ValueError, subprocess.SubprocessError):
        marker.unlink(missing_ok=True)
        raise


def install(agent, previous, reinstall):
    if previous is not None and not reinstall:
        return 'already installed'
    runtime, loaded = launch_state(agent)
    if previous is not None and runtime == 'Unverified':
        raise ValueError('could not verify previous runtime state')
    launch = previous is None or loaded
    if agent == 'recovery':
        api = recovery()
        api['configure'](True, api['enabled'](), enroll_live=False, launch=launch)
    else:
        label, path = specification(agent)
        env = {key: os.environ[key] for key in (
            'HOME', 'PATH', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
            'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'T_LOCAL_RC', 'TMPDIR') if key in os.environ}
        atomic_plist(path, dict(Label=label, RunAtLoad=True, KeepAlive=True, ThrottleInterval=30,
                               ProgramArguments=[sys.executable, str(ROOT / 'libexec/t_close_queue.py'), 'watch'],
                               EnvironmentVariables=env, WorkingDirectory=str(Path.home()), ExitTimeOut=60))
        try:
            if loaded:
                ensure(run('bootout', domain() + '/' + label))
            if launch:
                start(agent)
        except (OSError, ValueError, subprocess.SubprocessError):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_plist(path, previous)
                if loaded:
                    start(agent)
            raise
    return 'installed' if previous is None else 'reinstalled'


@contextmanager
def claim():
    folder = state()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(folder / 'management.lock', os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another background service action is running') from None
        yield


def command(action, target, reinstall, interval):
    agents = NAMES if target in (None, 'all') else (target,)
    if action == 'status':
        compact = sys.stdout.isatty() and shutil.get_terminal_size().columns < 86
        if not compact:
            print(f'{"BACKGROUND AGENT":<27} {"INSTALLATION":<14} {"STATUS":<12} {"STARTS AT LOGIN":<15} CHECK INTERVAL')
        for agent in agents:
            installed, live, login = status(agent)
            cadence = f'Every {interval} min' if agent == 'cleanup' else 'continuous'
            if compact:
                print(NAMES[agent])
                print(f'  {installed} · {live}')
                print(f'  Starts at login: {login.lower()} · {cadence}')
            else:
                print(f'{NAMES[agent]:<27} {installed:<14} {live:<12} {login:<15} {cadence}')
        return 0
    if sys.platform != 'darwin':
        raise ValueError('background agent installation and controls require macOS')
    failed = False
    with claim():
        for agent in agents:
            try:
                previous = read_service(agent)
                if action == 'install':
                    outcome = install(agent, previous, reinstall)
                elif previous is None:
                    raise ValueError(f'install first with t agent background install {agent}')
                elif action == 'stop':
                    stop(agent)
                    outcome = 'stopped for this login'
                else:
                    if action == 'restart':
                        stop(agent)
                    start(agent)
                    outcome = 'restarted' if action == 'restart' else 'started'
                print(f'{agent}: {outcome}')
            except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError) as error:
                print(f'{agent}: {error}', file=sys.stderr)
                failed = True
    return int(failed)


def upgrade_cleanup():
    if sys.platform != 'darwin' or (ROOT / '.git').is_file():
        return
    if read_service('cleanup') is None:
        return
    with claim():
        previous = read_service('cleanup')
        if previous is None:
            return
        worker = ROOT / 'libexec/t_close_queue.py'
        changed_source = previous['ProgramArguments'][1] != str(worker)
        if not changed_source and launch_state('cleanup')[0] == 'Running':
            try:
                current = (state() / 'cleanup.fingerprint').read_text().strip()
            except FileNotFoundError:
                current = ''
            changed_source = current != hashlib.sha256(worker.read_bytes()).hexdigest()
        if changed_source:
            install('cleanup', previous, True)
