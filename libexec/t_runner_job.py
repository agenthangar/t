"""Portable, credential-free runner snapshot and bounded job helper (Python 3.9+).

A clean environment and disposable workspace are hygiene, not an OS sandbox:
only run trusted code on a dedicated runner account or machine.
"""
import argparse
import fnmatch
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 10000
MAX_LOG = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_BLOCKED = {'.git', '.hg', '.svn', '.ssh', '.aws', '.azure', '.gnupg',
            '.codex', '.claude', '.cursor', '.gemini', '.t', '.config',
            '.local', '.cache', '.npmrc', '.pypirc', '.netrc', '.docker',
            'credentials', 'credentials.json', 'secrets', 'secrets.json',
            'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'auth.json', 'oauth.json',
            '.git-credentials', '.gitconfig', '.agent', '.agents', '.bash_history',
            '.zsh_history', '.python_history'}


def safe_path(value):
    """Require portable relative names, with no hidden normalization."""
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('unsafe relative path')
    if len(os.fsencode(value)) > 4096:
        raise ValueError('relative path exceeds portable limit')
    parts = value.split('/')
    if any(p in ('', '.', '..') or ':' in p for p in parts) or PurePosixPath(value).is_absolute():
        raise ValueError('unsafe relative path: ' + value)
    return value


def excluded(value):
    return any(p.lower() in _BLOCKED or p.lower().startswith(('.env', 'credentials.', 'secrets.'))
               or p.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.keychain'))
               for p in value.split('/'))


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def _version(info):
    return _identity(info) + (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_root(root):
    # Resolve the trusted root once (macOS /tmp and /var are system aliases),
    # then hold every ancestor and verify its name still identifies that inode.
    root = Path(root).resolve(strict=True)
    descriptors = [os.open('/', os.O_RDONLY | os.O_DIRECTORY)]
    links = []
    try:
        for part in root.parts[1:]:
            parent = descriptors[-1]
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            links.append((parent, part, child))
        return descriptors, links
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def read_regular(root, relative, limit=MAX_BYTES):
    """Read a stable ordinary file through held, no-follow directory descriptors."""
    safe_path(relative)
    descriptors, links = _open_root(root)
    try:
        parts = relative.split('/')
        for part in parts[:-1]:
            parent = descriptors[-1]
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            links.append((parent, part, child))
        parent = descriptors[-1]
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('only regular files are supported: ' + relative)
        if before.st_size > limit:
            raise ValueError('file exceeds snapshot limit')
        chunks, length = [], 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit - length + 1))
            if not chunk:
                break
            length += len(chunk)
            if length > limit:
                raise ValueError('file exceeds snapshot limit')
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if _version(before) != _version(after) or _version(after) != _version(named) or length != before.st_size:
            raise ValueError('file changed during snapshot: ' + relative)
        for ancestor, name, child in links:
            if _identity(os.stat(name, dir_fd=ancestor, follow_symlinks=False)) != _identity(os.fstat(child)):
                raise ValueError('directory changed during snapshot')
        return b''.join(chunks), (0o755 if before.st_mode & 0o111 else 0o644)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _git(repo, *args):
    return subprocess.check_output(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-C', str(repo), *args],
        stderr=subprocess.DEVNULL, timeout=30,
        env={'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
             'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})


def _record(path, data, mode):
    return {'path': path, 'mode': mode, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _tar_add(archive, record, data):
    member = tarfile.TarInfo(record['path'])
    member.size, member.mode, member.mtime = len(data), record['mode'], 0
    archive.addfile(member, io.BytesIO(data))


def create_snapshot(repo, archive, manifest_path, include=(), max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Snapshot current tracked bytes and explicitly selected untracked files.

    Git is queried read-only: neither HEAD nor index is modified. Missing tracked
    files represent deletions and are absent from the transferred tree.
    """
    repo, archive, manifest_path = Path(repo), Path(archive), Path(manifest_path)
    if _git(repo, 'rev-parse', '--show-prefix').strip():
        raise ValueError('snapshot source must be the repository root')
    patterns = [safe_path(str(value)) for value in include]
    indexed = _git(repo, 'ls-files', '--stage', '-z').split(b'\0')
    names = set()
    for entry in filter(None, indexed):
        metadata, raw_name = entry.split(b'\t', 1)
        mode, _object, stage = metadata.split()
        name = safe_path(os.fsdecode(raw_name))
        if excluded(name):
            continue
        if mode not in (b'100644', b'100755') or stage != b'0':
            raise ValueError('unsupported tracked file or unresolved merge: ' + name)
        names.add(name)
    if patterns:
        for raw_name in _git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if not raw_name:
                continue
            name = safe_path(os.fsdecode(raw_name))
            if not excluded(name) and any(name == p or name.startswith(p + '/') or fnmatch.fnmatchcase(name, p) for p in patterns):
                names.add(name)
    if len(names) > max_files:
        raise ValueError('snapshot file count exceeds limit')
    try:
        head = _git(repo, 'rev-parse', '--verify', 'HEAD').decode('ascii').strip()
    except subprocess.CalledProcessError:
        head = None
    manifest = {'version': 1, 'id': str(uuid.uuid4()), 'source_head': head, 'files': [], 'total_bytes': 0}
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=str(archive.parent))
    os.close(fd)
    try:
        with tarfile.open(temporary, 'w', format=tarfile.PAX_FORMAT) as tar:
            for name in sorted(names):
                try:
                    data, mode = read_regular(repo, name, max_bytes - manifest['total_bytes'])
                except FileNotFoundError:
                    continue
                record = _record(name, data, mode)
                _tar_add(tar, record, data)
                manifest['files'].append(record)
                manifest['total_bytes'] += len(data)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + '\n')
        os.replace(temporary, archive)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest


def validate_archive(archive, manifest, max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Validate every member and digest before creating any destination files."""
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        raise ValueError('invalid manifest version')
    uuid.UUID(manifest['id'])
    records = manifest['files']
    if not isinstance(records, list) or len(records) > max_files:
        raise ValueError('invalid manifest file count')
    expected, total = {}, 0
    for record in records:
        name = safe_path(record['path'])
        if excluded(name) or name in expected or type(record['size']) is not int or record['size'] < 0 or record['mode'] not in (0o644, 0o755):
            raise ValueError('invalid manifest member')
        if not isinstance(record['sha256'], str) or len(record['sha256']) != 64:
            raise ValueError('invalid manifest digest')
        total += record['size']
        if total > max_bytes:
            raise ValueError('archive exceeds size limit')
        expected[name] = record
    if total != manifest['total_bytes']:
        raise ValueError('manifest size mismatch')
    if Path(archive).stat().st_size > max_bytes + max_files * 16384 + 10240:
        raise ValueError('archive exceeds transfer size limit')
    payloads = {}
    with tarfile.open(archive, 'r:') as tar:
        for member in tar:
            name = safe_path(member.name)
            record = expected.get(name)
            if not member.isfile() or member.linkname or name in payloads or record is None or member.size != record['size'] or member.mode != record['mode']:
                raise ValueError('unexpected archive member')
            stream = tar.extractfile(member)
            data = stream.read(record['size'] + 1)
            if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                raise ValueError('archive digest mismatch')
            payloads[name] = data
    if payloads.keys() != expected.keys():
        raise ValueError('archive is missing manifest members')
    return [(expected[name], data) for name, data in payloads.items()]


def extract_snapshot(archive, manifest, destination):
    payloads = validate_archive(archive, manifest)
    destination = Path(destination)
    if destination.exists():
        raise ValueError('extraction destination must be new')
    destination.mkdir(mode=0o700)
    for record, data in payloads:
        path = destination / record['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(record['mode'])


def probe():
    return {'os': platform.system().lower(), 'arch': platform.machine().lower()}


def _normal_os(value):
    return {'macos': 'darwin'}.get(value.lower(), value.lower())


def _normal_arch(value):
    return {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64'}.get(value.lower(), value.lower())


def clean_environment(home):
    home = Path(home)
    values = {'HOME': str(home), 'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
              'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TMPDIR': str(home / 'tmp'),
              'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache'),
              'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_STATE_HOME': str(home / '.local/state')}
    for key in ('HOME', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        Path(values[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return values


def cancel_job(job_dir, token):
    uuid.UUID(token)
    job_dir = Path(job_dir)
    if (job_dir / 'token').read_text().strip() != token:
        raise ValueError('job token does not match')
    (job_dir / 'cancel').write_text(token)


def _cancelled(job_dir, token):
    try:
        return (job_dir / 'cancel').read_text().strip() == token
    except FileNotFoundError:
        return False


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute(argv, workspace, home, job_dir, token, timeout, max_log=MAX_LOG):
    if not argv or not math.isfinite(timeout) or timeout <= 0 or timeout > 86400:
        raise ValueError('command and timeout between 0 and 86400 seconds required')
    process = subprocess.Popen(argv, cwd=str(workspace), env=clean_environment(home), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline, written, truncated, reason = time.monotonic() + timeout, 0, False, None
    ended_at = None
    try:
        with (job_dir / 'output.log').open('wb') as log:
            while process.poll() is None or selector.get_map():
                if reason is None and _cancelled(job_dir, token):
                    reason = 'cancelled'
                    _kill_group(process)
                if reason is None and time.monotonic() >= deadline:
                    reason = 'timed_out'
                    _kill_group(process)
                if process.poll() is not None:
                    _kill_group(process)  # Descendants cannot outlive the owned job.
                    ended_at = ended_at or time.monotonic()
                    if time.monotonic() - ended_at > 1:
                        break  # Even an escaped descendant cannot hold the log pipe forever.
                for key, _events in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, max_log - written)
                    log.write(chunk[:available])
                    log.flush()
                    written += min(available, len(chunk))
                    truncated = truncated or len(chunk) > available
        code = process.wait()
        return {'status': reason or ('succeeded' if code == 0 else 'failed'), 'exit_code': code,
                'log_bytes': written, 'log_truncated': truncated}
    finally:
        _kill_group(process)
        process.wait()
        selector.close()
        process.stdout.close()


def _artifact_names(workspace, name, max_files):
    descriptors, links = _open_root(workspace)
    try:
        parts = name.split('/')
        for index, part in enumerate(parts):
            parent = descriptors[-1]
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=parent)
            descriptors.append(child)
            links.append((parent, part, child))
        selected = descriptors[-1]
        mode = os.fstat(selected).st_mode
        names = []
        if stat.S_ISDIR(mode):
            visited = 0
            for current, directories, files, directory_fd in os.fwalk('.', dir_fd=selected, follow_symlinks=False):
                visited += len(directories) + len(files)
                if visited > max_files:
                    raise ValueError('artifact file count exceeds limit')
                for entry in directories + files:
                    kind = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False).st_mode
                    if not (stat.S_ISDIR(kind) or stat.S_ISREG(kind)):
                        raise ValueError('symlink or special artifact is unsupported')
                prefix = '' if current == '.' else current[2:] + '/'
                names.extend(name + '/' + prefix + entry for entry in files)
        elif stat.S_ISREG(mode):
            names = [name]
        else:
            raise ValueError('symlink or special artifact is unsupported')
        for parent, entry, child in links:
            if _identity(os.stat(entry, dir_fd=parent, follow_symlinks=False)) != _identity(os.fstat(child)):
                raise ValueError('artifact directory changed during collection')
        return names
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def collect_artifacts(workspace, paths, archive, max_bytes=MAX_ARTIFACT_BYTES, max_files=MAX_FILES):
    names = set()
    for value in paths:
        name = safe_path(value)
        if excluded(name):
            raise ValueError('credential or state artifact is excluded')
        names.update(_artifact_names(workspace, name, max_files))
        if len(names) > max_files:
            raise ValueError('artifact file count exceeds limit')
    records, total = [], 0
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(names):
            if excluded(name):
                raise ValueError('credential or state artifact is excluded')
            data, mode = read_regular(workspace, name, max_bytes - total)
            record = _record(name, data, mode)
            _tar_add(tar, record, data)
            total += len(data)
            records.append(record)
    return records


def run_job(job_dir, archive, manifest_path, argv, timeout, token, expected_os=None, expected_arch=None, artifacts=()):
    uuid.UUID(token)
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path = job_dir / 'token'
    if token_path.exists() and token_path.read_text().strip() != token:
        raise ValueError('job token does not match')
    token_path.write_text(token)
    result = {'version': 1, 'id': token, 'status': 'error', 'exit_code': None, 'artifacts': [], **probe()}
    sandbox = Path(tempfile.mkdtemp(prefix='runtime-', dir=str(job_dir)))
    try:
        if expected_os and _normal_os(expected_os) != _normal_os(result['os']):
            raise ValueError('runner OS does not match profile')
        if expected_arch and _normal_arch(expected_arch) != _normal_arch(result['arch']):
            raise ValueError('runner architecture does not match profile')
        manifest = json.loads(Path(manifest_path).read_text())
        workspace = sandbox / 'workspace'
        extract_snapshot(archive, manifest, workspace)
        result['snapshot_id'] = manifest['id']
        result['source_head'] = manifest['source_head']
        result.update(execute(argv, workspace, sandbox / 'home', job_dir, token, timeout))
        if artifacts:
            try:
                result['artifacts'] = collect_artifacts(workspace, artifacts, job_dir / 'artifacts.tar')
            except (OSError, ValueError, tarfile.TarError) as error:
                result['artifact_error'] = str(error)[:1000]
                (job_dir / 'artifacts.tar').unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        result['status'] = 'error'
        result['error'] = str(error)[:1000]
        (job_dir / 'artifacts.tar').unlink(missing_ok=True)
    finally:
        shutil.rmtree(sandbox)
        # Input source is sensitive; keep only bounded results after completion.
        for path in (Path(archive), Path(manifest_path)):
            if path.parent == job_dir:
                path.unlink(missing_ok=True)
        (job_dir / 'result.json').write_text(json.dumps(result, sort_keys=True) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('probe')
    cancel = sub.add_parser('cancel')
    cancel.add_argument('--job-dir', required=True)
    cancel.add_argument('--token', required=True)
    run = sub.add_parser('run')
    for name in ('job-dir', 'archive', 'manifest', 'token'):
        run.add_argument('--' + name, required=True)
    run.add_argument('--timeout', type=float, required=True)
    run.add_argument('--os')
    run.add_argument('--arch')
    run.add_argument('--artifact', action='append', default=[])
    run.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == 'probe':
        print(json.dumps(probe()))
        return 0
    if args.action == 'cancel':
        cancel_job(args.job_dir, args.token)
        return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    result = run_job(args.job_dir, args.archive, args.manifest, command, args.timeout, args.token,
                     args.os, args.arch, args.artifact)
    return 0 if result['status'] == 'succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
