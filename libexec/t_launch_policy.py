"""Private fresh-launch policy cache; source files remain authoritative.

The warm path imports only the standard library and the lightweight MCP dependency
reader. It never starts a client, resolves a provider, or loads the workflow engine.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'libexec'
VERSION = 1
IDLE_TTL = 24 * 60 * 60
MAX_CACHE_BYTES = 256 * 1024
ENV_KEYS = (
    'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
    'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'CURSOR_CONFIG_DIR', 'PATH', 'T_WORKFLOW_DEFAULT_MODEL',
    'T_LAUNCH_DEFAULT_MODEL', 'T_LAUNCH_DEFAULT_EFFORT', 'T_LAUNCH_DEFAULT_SPEED',
)


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def fingerprint(path):
    """Include replacements, missing/created transitions and symlink targets."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    value = {'entry': _identity(info)}
    if stat.S_ISLNK(info.st_mode):
        value['link'] = os.readlink(path)
        try:
            value['target'] = _identity(path.stat())
        except FileNotFoundError:
            value['target'] = None
    return value


def _context_scope(cwd):
    """Accept prepared common metadata only for its unchanged workspace."""
    raw = os.environ.get('T_LAUNCH_CONTEXT', '')
    if not raw or len(raw) > 64 * 1024:
        return None
    try:
        value = json.loads(raw)
        workspace = Path(value['workspace']).resolve()
        cwd.relative_to(workspace)
        info, gitfile = workspace.stat(), (workspace / '.git').stat()
        marker = value['gitfile_identity']
        if (value.get('version') != 1 or value['workspace_identity'] !=
                f'{info.st_dev}:{info.st_ino}' or
                any(marker.get(key) != getattr(gitfile, attr) for key, attr in
                    (('dev', 'st_dev'), ('ino', 'st_ino'), ('ctime_ns', 'st_ctime_ns')))):
            return None
        common = Path(value['common_dir']).resolve(strict=True)
        marker_path = workspace / '.git'
        if marker_path.is_dir():
            actual_common = marker_path.resolve(strict=True)
        else:
            text = marker_path.read_text(encoding='utf-8')
            if len(text) > 8192 or not text.startswith('gitdir: '):
                return None
            gitdir = (workspace / text[8:].strip()).resolve(strict=True)
            pointer = gitdir / 'commondir'
            relative = pointer.read_text(encoding='utf-8').strip() if pointer.exists() else '.'
            if not relative or len(relative) > 8192:
                return None
            actual_common = (gitdir / relative).resolve(strict=True)
        return ((workspace, common) if common.is_dir() and actual_common == common and
                _identity(marker_path.stat()) == _identity(gitfile) else None)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def repository_scope(cwd, common_dir=None):
    """Read Git's directory pointers locally, without a git subprocess."""
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('launch directory is not a directory')
    prepared = _context_scope(cwd)
    if prepared and (common_dir is None or prepared[1] == Path(common_dir).resolve()):
        return prepared
    for workspace in (cwd, *cwd.parents):
        marker = workspace / '.git'
        if marker.is_dir():
            common = marker.resolve(strict=True)
        elif marker.is_file():
            text = marker.read_text(encoding='utf-8')
            if len(text) > 8192 or not text.startswith('gitdir: '):
                raise ValueError('invalid linked worktree Git directory')
            gitdir = (workspace / text[8:].strip()).resolve(strict=True)
            pointer = gitdir / 'commondir'
            relative = pointer.read_text(encoding='utf-8').strip() if pointer.exists() else '.'
            if not relative or len(relative) > 8192:
                raise ValueError('invalid linked worktree common directory')
            common = (gitdir / relative).resolve(strict=True)
        elif marker.is_symlink():
            raise ValueError('invalid workspace Git marker')
        else:
            continue
        if not common.is_dir() or (common_dir is not None and common != Path(common_dir).resolve()):
            raise ValueError('launch common Git directory does not match workspace')
        return workspace, common
    if common_dir is not None:
        raise ValueError('launch common Git directory cannot be verified')
    return cwd, None


def _mcp():
    return runpy.run_path(str(LIB / 't_mcp_policy.py'))


def source_paths():
    return [ROOT / 'bin/t', *LIB.glob('t_*.py'), *LIB.glob('*.json'),
            LIB / 't_issue_policy.txt']


def dependency_paths(agent, cwd):
    home = Path.home()
    config = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex')
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    # All local Python policy helpers are cheap to stat and share one release.
    paths = [*source_paths(), config / 't/workflow.json',
             config / 't/local-worker.json', codex / 'config.toml',
             codex / 'AGENTS.md', codex / 'AGENTS.override.md',
             codex / 'auth.json', codex / 'models_cache.json',
             claude / 'CLAUDE.md', claude / 'settings.json', home / '.claude.json']
    paths.extend(_mcp()['dependency_paths'](agent, str(cwd)))
    executable = shutil.which('cursor-agent' if agent == 'cursor' else agent)
    if executable:
        paths.append(Path(executable))
    return sorted(set(Path(path).absolute() for path in paths), key=str)


def dependency_fingerprints(agent, cwd, workspace):
    """Equivalent project inputs can share policy across newly created slots."""
    result = {}
    sources = set(source_paths())
    for path in dependency_paths(agent, cwd):
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            relative = None
        if relative is not None and path not in sources:
            result['workspace/' + str(relative)] = project_fingerprint(path)
        else:
            result[str(path)] = fingerprint(path)
    return result


def project_fingerprint(path):
    """Compare bounded project configuration by content rather than slot inode."""
    before = fingerprint(path)
    if before is None or not stat.S_ISREG(before['entry'][2]) or before['entry'][3] > 2 * 1024 * 1024:
        return before
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = _identity(os.fstat(stream.fileno()))
        value = stream.read(2 * 1024 * 1024 + 1)
    if opened != before['entry'] or fingerprint(path) != before:
        raise ValueError('project launch configuration changed while reading; retry')
    return {'content': hashlib.sha256(value).hexdigest(), 'mode': stat.S_IMODE(opened[2])}


def cache_key(agent, workspace, common):
    info = common.stat() if common else workspace.stat()
    identity = {'version': VERSION, 'agent': agent, 'source': str(ROOT),
                'scope': str(common or workspace), 'identity': [info.st_dev, info.st_ino],
                'environment': {key: os.environ.get(key) for key in ENV_KEYS}}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def cache_path(key):
    root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return root / 't/launch-policy' / (key + '.json')


@contextmanager
def _locked(path):
    """A failed cache store falls back to fresh resolution, never stale policy."""
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path.with_suffix('.lock'),
                     os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _read(path):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > MAX_CACHE_BYTES):
            return None
        with path.open(encoding='utf-8') as stream:
            opened = os.fstat(stream.fileno())
            value = json.load(stream)
        if _identity(opened) != _identity(info) or fingerprint(path) != {'entry': _identity(info)}:
            return None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _valid_bundle(value):
    if not (isinstance(value, dict) and
            ('cacheable' not in value or type(value['cacheable']) is bool) and
            all(isinstance(value.get(key), list) and
                all(isinstance(word, str) and '\0' not in word for word in value[key])
                for key in ('args', 'prefix'))):
        return False
    prefix = value['prefix']
    if not prefix:
        return value.get('routing') is None
    if len(prefix) != 4 or prefix[:3] != ['python3', str(LIB / 't_local_inference.py'), '--lead-policy']:
        return False
    try:
        return isinstance(value.get('routing'), dict) and json.loads(prefix[3]) == value['routing']
    except ValueError:
        return False


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.launch-policy-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True, separators=(',', ':'))
            stream.write('\n')
        os.replace(temporary, path)
    except OSError:
        pass  # Optional cache persistence must not invalidate resolved launch settings.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def resolve_uncached(agent, cwd, common_dir=None):
    api = runpy.run_path(str(LIB / 't_workflow_sync.py'))
    return api['launch_bundle'](agent, str(cwd), common_dir=str(common_dir) if common_dir else None)


def _fresh(agent, cwd, workspace, common):
    # A save racing the resolution must not mix permissions and provider revisions.
    for _ in range(2):
        before = dependency_fingerprints(agent, cwd, workspace)
        bundle = resolve_uncached(agent, cwd, common)
        after = dependency_fingerprints(agent, cwd, workspace)
        if before == after:
            if not _valid_bundle(bundle):
                raise ValueError('invalid resolved launch policy')
            return bundle, after
    raise ValueError('launch settings changed while resolving; retry the launch')


def resolve(agent, cwd=None, *, common_dir=None, now=None):
    if agent not in ('codex', 'claude', 'cursor'):
        raise ValueError('unsupported launch tool')
    cwd = Path(cwd or os.getcwd()).resolve(strict=True)
    workspace, common = repository_scope(cwd, common_dir)
    key = cache_key(agent, workspace, common)
    path = cache_path(key)
    now = time.time() if now is None else now
    if not _number(now):
        raise ValueError('invalid cache clock')
    with _locked(path) as available:
        current = dependency_fingerprints(agent, cwd, workspace)
        record = _read(path) if available else None
        hit = (record is not None and record.get('version') == VERSION and
               record.get('key') == key and record.get('dependencies') == current and
               _number(record.get('expires')) and record['expires'] > now and
               _number(record.get('last_used')) and record['last_used'] <= now + 300 and
               _valid_bundle(record.get('bundle')) and record['bundle'].get('cacheable', True))
        if hit:
            bundle = record['bundle']
            observed = max(now, record['last_used'])
        else:
            bundle, current = _fresh(agent, cwd, workspace, common)
            observed = now
        if available and bundle.get('cacheable', True):
            _write(path, {'version': VERSION, 'key': key, 'dependencies': current,
                          'last_used': observed, 'expires': observed + IDLE_TTL,
                          'bundle': bundle})
        return {**bundle, 'cached': bool(hit)}


def frame(bundle):
    if not _valid_bundle(bundle):
        raise ValueError('invalid launch frame')
    words = [str(len(bundle['prefix'])), *bundle['prefix'], *bundle['args']]
    return '\0'.join(words).encode()


def main(argv):
    if len(argv) not in (3, 5) or argv[0] != 'launch' or (len(argv) == 5 and argv[3] != '--common-dir'):
        raise ValueError('use launch TOOL DIRECTORY [--common-dir DIRECTORY]')
    bundle = resolve(argv[1], argv[2], common_dir=argv[4] if len(argv) == 5 else None)
    sys.stdout.buffer.write(frame(bundle))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print('t: cannot resolve launch settings: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
