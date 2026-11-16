"""Local fresh-workspace allocation and independent origin-ref maintenance.

The foreground never contacts a host. Counters are durable Git-private metadata;
the caller holds the existing workspace launch claim across prepare and launch.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

INTERVAL = 300
RETRY = 60
COLLISION = 3


class Collision(ValueError):
    pass


class GitFailure(ValueError):
    def __init__(self, result):
        super().__init__('could not inspect or prepare the local repository; existing work was retained')
        self.diagnostic = (result.stderr or result.stdout).strip()


def git(repository, *args, optional=False):
    result = subprocess.run(['git', '-C', str(repository), *args], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, timeout=30)
    if result.returncode and not optional:
        raise GitFailure(result)
    return result.stdout.strip() if result.returncode == 0 else ''


def common_directory(repository):
    value = git(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError('repository common directory is unavailable')
    return path


def folder(repository):
    path = common_directory(repository) / 't-fresh'
    path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError('unsafe fresh-workspace state directory')
    return path


@contextmanager
def locked(path, *, blocking=True):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('unsafe fresh-workspace lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def read(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 65536:
            raise ValueError('unsafe fresh-workspace state file')
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def seed(repository, root):
    """One local inventory on cache loss; ordinary reservations read one integer."""
    base = Path(repository).name
    highest = 0
    expressions = (re.compile(r'(?:refs/heads/|refs/remotes/origin/)dev/' + re.escape(base) + r'-(\d+)$'),
                   re.compile(r'(?:' + re.escape(base) + r'-)?(\d+)$'))
    for ref in git(repository, 'for-each-ref', '--format=%(refname)',
                   'refs/heads/dev/', 'refs/remotes/origin/dev/').splitlines():
        match = expressions[0].fullmatch(ref)
        if match:
            highest = max(highest, int(match[1]))
    directory = Path(root) / base
    if directory.is_dir():
        for entry in directory.iterdir():
            match = expressions[1].fullmatch(entry.name)
            if match:
                highest = max(highest, int(match[1]))
    try:
        result = subprocess.run(['tmux', 'list-sessions', '-F', '#{session_name}|#{session_path}'],
                                capture_output=True, text=True, timeout=5)
        for row in result.stdout.splitlines():
            name, _, cwd = row.partition('|')
            if Path(cwd).parent == directory or Path(cwd).resolve() == Path(repository).resolve():
                match = re.search(r'-(\d+)$', name)
                if match:
                    highest = max(highest, int(match[1]))
    except (OSError, subprocess.TimeoutExpired):
        pass  # Final workspace ownership preflight remains authoritative.
    return highest + 1


def reserve(repository, root):
    state = folder(repository)
    with locked(state / 'counter.lock'):
        value = read(state / 'counter.json')
        number = value.get('next') if isinstance(value, dict) and value.get('version') == 1 else None
        if type(number) is not int or number < 1:
            number = seed(repository, root)
        write(state / 'counter.json', {'version': 1, 'next': number + 1})
    return str(number)


def default_revision(repository):
    origin = git(repository, 'symbolic-ref', '-q', 'refs/remotes/origin/HEAD', optional=True)
    candidates = [origin] if origin.startswith('refs/remotes/origin/') and origin != 'refs/remotes/origin/HEAD' else []
    if candidates:
        candidates.append('refs/heads/' + origin.removeprefix('refs/remotes/origin/'))
    candidates += ['refs/remotes/origin/main', 'refs/remotes/origin/master', 'refs/heads/main', 'refs/heads/master']
    for ref in candidates:
        revision = git(repository, 'rev-parse', '--verify', '-q', ref + '^{commit}', optional=True)
        if re.fullmatch(r'[0-9a-f]{40,64}', revision):
            return revision
    raise ValueError('no cached origin or local default branch; fetch the repository before opening a fresh session')


def prepare(repository, root, slot):
    if not str(slot).isdigit() or int(slot) < 1:
        raise ValueError('invalid fresh slot')
    base = Path(repository).name
    repository = Path(repository).resolve()
    common = common_directory(repository)
    directory = Path(root).absolute() / base
    workspace = directory / f'{base}-{slot}'
    branch = f'dev/{base}-{slot}'
    for candidate in (workspace, directory / str(slot), directory / f'.{base}-{slot}.history-reservation',
                      directory / f'.{slot}.history-reservation'):
        if os.path.lexists(candidate):
            raise Collision('fresh slot was claimed; choosing another number')
    for ref in ('refs/heads/' + branch, 'refs/remotes/origin/' + branch):
        if git(repository, 'rev-parse', '--verify', '-q', ref, optional=True):
            raise Collision('fresh branch was claimed')
    revision = default_revision(repository)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        git(repository, 'worktree', 'add', '--quiet', '--no-track', '-b', branch, str(workspace), revision)
    except ValueError as error:
        # Git may create the branch and checkout before a post-checkout hook
        # fails. Their existence cannot prove that another caller won a race.
        # Preserve the evidence and stop; never allocate indefinitely on a
        # persistent checkout/hook failure.
        message = f'could not create fresh worktree {workspace}; any created branch or files were retained'
        diagnostic = getattr(error, 'diagnostic', '')
        if diagnostic:
            message += '\n' + diagnostic[-4096:]
        raise ValueError(message) from error
    info, gitfile = workspace.stat(), (workspace / '.git').stat()
    gitdir = git(workspace, 'rev-parse', '--absolute-git-dir')
    remote = git(repository, 'remote', 'get-url', 'origin', optional=True)
    match = re.fullmatch(r'(?:https://github.com/|git@github.com:|ssh://git@github.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote)
    generation = hashlib.sha256(json.dumps([gitdir, gitfile.st_dev, gitfile.st_ino, gitfile.st_ctime_ns]).encode()).hexdigest()
    return dict(version=1, observed=time.time(), repo=match[1].lower() if match else 'local:' + str(common.parent),
                common_dir=str(common), workspace=str(workspace), slot=str(slot), branch=branch,
                commit=revision, gitdir=gitdir, generation=generation,
                workspace_identity=f'{info.st_dev}:{info.st_ino}',
                gitfile_identity=dict(dev=gitfile.st_dev, ino=gitfile.st_ino, ctime_ns=gitfile.st_ctime_ns))


def schedule(repository, *, force=False):
    """Start one coalesced worker per repository; no foreground network I/O."""
    if os.environ.get('T_FRESH_REFRESH_DISABLE'):
        return
    common = common_directory(repository)
    repository = common.parent if common.name == '.git' else Path(repository).resolve()
    state = folder(repository)
    if force:
        (state / 'refresh-request').touch()
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watch', str(Path(repository).resolve())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True, cwd='/', env=env)


def refresh(repository):
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GIT_ASKPASS='true', SSH_ASKPASS='true')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=5')
    try:
        result = subprocess.run(['git', '-C', str(repository), 'fetch', '--quiet', '--prune', '--no-tags', 'origin'],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def watch(repository):
    state = folder(repository)
    with locked(state / 'refresh.lock', blocking=False) as acquired:
        if not acquired:
            return
        deadline, observed = 0, 0
        source_version = Path(__file__).stat().st_mtime_ns
        while Path(repository).is_dir():
            if Path(__file__).stat().st_mtime_ns != source_version:
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), 'watch', str(repository)])
            try:
                requested = (state / 'refresh-request').stat().st_mtime_ns
            except FileNotFoundError:
                requested = 0
            if time.monotonic() >= deadline or requested != observed:
                observed = requested
                success = refresh(repository)
                write(state / 'refresh.json', {'version': 1, 'checked': time.time(), 'success': success})
                deadline = time.monotonic() + (INTERVAL if success else RETRY)
            time.sleep(1)


def main(argv):
    action, repository = argv[:2]
    if action == 'reserve':
        print(reserve(repository, argv[2]))
    elif action in ('prepare', 'prepare-shell'):
        value = prepare(repository, argv[2], argv[3])
        print(json.dumps(value))
        if action == 'prepare-shell':
            print(value['workspace'])
            print(value['workspace_identity'])
    elif action == 'schedule':
        schedule(repository, force='--force' in argv[2:])
    elif action == 'watch':
        watch(repository)
    else:
        raise ValueError('unknown fresh workspace operation')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Collision:
        sys.exit(COLLISION)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('t: ' + str(error), file=sys.stderr)
        sys.exit(1)
