"""Shared lifecycle policy, durable operations, and a versioned shell adapter.

The engine owns ordering and receipts. Terminal/tmux/SSH effects remain in the
shell adapter so callers retain their login environment and terminal contract.
"""
from contextlib import contextmanager
from contextlib import nullcontext
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

PROTOCOL = 1
ROOT = Path(__file__).resolve().parent.parent
STORE = runpy.run_path(str(ROOT / 'libexec/t_codex_store.py'))
OWNERSHIP = runpy.run_path(str(ROOT / 'libexec/t_codex_ownership.py'))
SOURCE = runpy.run_path(str(ROOT / 'libexec/t_resume_source.py'))
TERMINAL = frozenset(('complete', 'cancelled'))


class LifecycleError(ValueError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def state_home():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/lifecycle'


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise LifecycleError('unsafe lifecycle record path')
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


@contextmanager
def claim(directory, identity, *, timeout=15):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise LifecycleError('unsafe lifecycle claim directory')
    path = directory / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LifecycleError('timed out waiting for conversation/workspace ownership claim')
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


@contextmanager
def launch_claims(workspace, sid):
    # Interoperate with released zsystem flock callers, including waiting peers.
    root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'claude-sessions'
    with claim(root / 'app-handoffs', os.path.realpath(workspace)):
        with claim(root / 'launch-sids', sid) if sid else _empty_claim():
            yield


@contextmanager
def _empty_claim():
    yield


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


@contextmanager
def native_writer_claim(sid):
    folder = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'thread-writer-locks'
    if not folder.is_dir() or folder.is_symlink():
        raise LifecycleError('cannot safely publish a Codex rollout without its native writer-lock directory')
    path = folder / (sid + '.lock')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LifecycleError('Codex claimed the selected writer before restore; no transcript changed') from error
        inode, current = os.fstat(descriptor), path.lstat()
        if (inode.st_dev, inode.st_ino) != (current.st_dev, current.st_ino):
            raise LifecycleError('Codex writer lock changed before restore')
        yield
    finally:
        os.close(descriptor)


def validate_identity(agent, sid, workspace):
    if agent not in ('claude', 'codex') or not OWNERSHIP['_UUID'].fullmatch(sid):
        raise LifecycleError('invalid conversation identity')
    if not isinstance(workspace, str) or not os.path.isabs(workspace):
        raise LifecycleError('invalid workspace identity')


def selected_rollout(sid):
    if not OWNERSHIP['_UUID'].fullmatch(sid):
        raise LifecycleError('invalid conversation identity')
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    for database in sorted(home.glob('state_*.sqlite'), reverse=True):
        matches = STORE['rows'](database, 'select rollout_path from threads where id=?', (sid,))
        if matches and matches[0][0]:
            path = Path(matches[0][0])
            path = path if path.is_absolute() else home / path
            if path.is_file():
                return path
    matches = [p for directory in ('sessions', 'archived_sessions')
               for p in (home / directory).glob('**/rollout-*' + sid + '.jsonl')]
    if len(matches) != 1:
        raise LifecycleError('selected Codex rollout is missing or ambiguous; retry t resume after restoring its history')
    return matches[0]


def native_preflight(agent, sid, workspace, rollout=None, *, retry='Retry: t session resume.'):
    if agent != 'codex':
        return
    if not sid:
        home = str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
        other = OWNERSHIP['_other_cwd_owner'](OWNERSHIP['_lock_dir'](home), '', workspace, home, run=run)
        if other:
            raise LifecycleError('another Codex conversation owns this workspace: ' + other)
        return
    path = Path(rollout) if rollout else selected_rollout(sid)
    home = str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    creation_cwd = OWNERSHIP['_rollout_cwd'](str(path), sid)
    STORE['rollout_integrity'](home, sid)
    OWNERSHIP['assert_released'](None, sid, creation_cwd, run, codex_home=home,
        rollout=str(path), ownership_cwd=workspace,
        archived=path.resolve().is_relative_to((Path(home) / 'archived_sessions').resolve()), retry=retry)


def wait_released(agent, sid, *, timeout=15):
    if agent != 'codex' or not sid:
        return
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    folder = OWNERSHIP['_lock_dir'](str(home))
    if folder is None:
        raise LifecycleError('cannot verify source writer release without the native lock contract')
    deadline = time.monotonic() + timeout
    while OWNERSHIP['_lock_held'](str(Path(folder) / (sid + '.lock'))):
        if time.monotonic() >= deadline:
            raise LifecycleError('client stopped but its conversation writer is still held; no destination started. Retry: t resume.')
        time.sleep(.1)


def verify_ready(agent, sid, *, timeout=15):
    if agent != 'codex' or not sid:
        return
    home = str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    deadline = time.monotonic() + timeout
    while True:
        folder = OWNERSHIP['_lock_dir'](home)
        if folder and OWNERSHIP['_lock_held'](str(Path(folder) / (sid + '.lock'))):
            return
        if time.monotonic() >= deadline:
            raise LifecycleError('Codex has not loaded the selected conversation; inspect its CLI before retrying t resume')
        time.sleep(.1)


class Operation:
    def __init__(self, verb, arguments, *, token=None, directory=None):
        self.token = token or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}', self.token):
            raise LifecycleError('invalid lifecycle operation ID')
        self.path = (Path(directory) if directory else state_home()) / 'operations' / (self.token + '.json')
        self.value = dict(version=PROTOCOL, id=self.token, verb=verb, arguments=arguments,
                          phase='created', receipts={}, updated=time.time())
        if self.path.is_symlink():
            raise LifecycleError('unsafe lifecycle record path')
        if self.path.exists():
            self.value = json.loads(self.path.read_text())
            if (self.value.get('version') != PROTOCOL or self.value.get('verb') != verb
                    or self.value.get('arguments') != arguments):
                raise LifecycleError('operation identity changed; no action repeated')

    def save(self, phase, **values):
        self.value.update(phase=phase, updated=time.time(), **values)
        write_private(self.path, self.value)

    def step(self, name, function, *, repeat=False):
        if not repeat and name in self.value['receipts']:
            return self.value['receipts'][name]
        self.save(name, pending=name)
        result = function()
        self.value['receipts'][name] = result
        self.save(name, pending=None)
        return result


class ShellAdapter:
    """Length-bounded JSON frames. User data is never executable shell code."""
    def call(self, action, *args):
        print(json.dumps(dict(call=action, args=list(args))), flush=True)
        line = sys.stdin.readline(8 * 1024 * 1024)
        if not line or not line.endswith('\n'):
            raise LifecycleError('lifecycle adapter disconnected; reconcile with t resume')
        result = json.loads(line)
        if result['code']:
            raise LifecycleError(f'{action} failed; work and recovery details retained', result['code'])
        return result['output'].rstrip('\n')

    def output(self, value):
        if value:
            print(json.dumps(dict(output=value)), flush=True)


def git(workspace, *args, optional=False):
    result = run(['git', '-C', workspace, *args], timeout=60)
    if result.returncode and not optional:
        raise LifecycleError('Git could not prepare saved work: ' + result.stderr.strip())
    return result.stdout.strip() if not result.returncode else ''


def source_preflight(workspace):
    if not git(workspace, 'branch', '--show-current'):
        raise LifecycleError('source is detached; select its work branch before moving')
    index = Path(git(workspace, 'rev-parse', '--git-path', 'index'))
    if not index.is_absolute():
        index = Path(workspace) / index
    if Path(str(index) + '.lock').exists():
        raise LifecycleError('Git is changing the source index; source was not stopped')
    paths = git(workspace, 'diff', '--name-only', '-z', 'HEAD') + '\0' + git(workspace, 'ls-files', '--others', '--exclude-standard', '-z')
    if any(SOURCE['private_path'](name) for name in paths.split('\0') if name):
        raise LifecycleError('source contains private credentials or agent artifacts; source was not stopped')


def checkpoint(workspace, sid, evidence=None):
    history = SimpleNamespace(error=LifecycleError, spinner=lambda _: nullcontext())
    def invoke(argv, *, optional=False, **kwargs):
        result = run(argv, timeout=60)
        if result.returncode and not optional:
            raise LifecycleError('source checkpoint failed: ' + result.stderr.strip())
        return result.stdout if not result.returncode else ''
    history.run = invoke
    association = dict(workspace=workspace, branch=git(workspace, 'branch', '--show-current'), commit=git(workspace, 'rev-parse', 'HEAD'))
    if evidence:
        current = workspace_evidence(workspace)
        if any(current[key] != evidence[key] for key in ('gitdir', 'generation', 'branch')):
            raise LifecycleError('source workspace generation changed; no checkpoint published')
        association.update(branch=evidence['branch'], commit=evidence['commit'])
    revision, _ = SOURCE['checkpoint'](history, {'sid': sid}, association)
    return revision


def workspace_evidence(workspace):
    marker = (Path(workspace) / '.git').stat()
    return dict(gitdir=git(workspace, 'rev-parse', '--absolute-git-dir'),
                generation=[marker.st_dev, marker.st_ino],
                branch=git(workspace, 'branch', '--show-current'),
                commit=git(workspace, 'rev-parse', 'HEAD'))


def transcript_manifest(agent, sid, workspace, filename):
    validate_identity(agent, sid, workspace)
    path = Path(filename)
    if not path.is_file() or path.is_symlink():
        raise LifecycleError('source transcript is missing or unsafe')
    data = path.read_bytes()
    verify_transcript(agent, sid, workspace, data)
    assets = []
    candidates = []
    origin = path.with_suffix('.origin')
    if origin.exists():
        candidates.append(('origin', origin))
    directory = path.parent / sid
    if directory.is_symlink():
        raise LifecycleError('unsafe conversation asset directory')
    if directory.is_dir():
        candidates.extend(('assets/' + item.relative_to(directory).as_posix(), item)
                          for item in sorted(directory.rglob('*')) if not item.is_dir() or item.is_symlink())
    total = len(data)
    for name, item in candidates:
        if item.is_symlink() or not item.is_file():
            raise LifecycleError('unsafe conversation asset')
        content = item.read_bytes()
        total += len(content)
        if total > SOURCE['MAX_BUNDLE']:
            raise LifecycleError('conversation assets exceed the supported transfer size')
        assets.append(dict(name=name, path=str(item), size=len(content), sha256=hashlib.sha256(content).hexdigest()))
    return dict(agent=agent, sid=sid, workspace=workspace, path=str(path),
                size=len(data), sha256=hashlib.sha256(data).hexdigest(), assets=assets)


def verify_transcript(agent, sid, workspace, data):
    if len(data) > SOURCE['MAX_BUNDLE']:
        raise LifecycleError('conversation exceeds the supported transfer size')
    if agent == 'codex':
        record = json.loads(data.splitlines()[0])
        metadata = record.get('payload', {})
        if record.get('type') != 'session_meta' or metadata.get('id') != sid:
            raise LifecycleError('transcript conversation identity changed')
        # Creation cwd can legitimately differ after an earlier relocation.
        if not isinstance(metadata.get('cwd'), str) or not os.path.isabs(metadata['cwd']):
            raise LifecycleError('transcript workspace identity is invalid')
    elif agent == 'claude':
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
        identities = {r.get('sessionId') for r in records if r.get('sessionId')}
        if identities != {sid}:
            raise LifecycleError('transcript conversation identity changed')
    else:
        raise LifecycleError('unsupported conversation agent')


def stage_import(token, manifest):
    if not re.fullmatch(r'[a-f0-9]{32}', token):
        raise LifecycleError('invalid transfer identity')
    validate_identity(manifest['agent'], manifest['sid'], manifest['workspace'])
    directory = state_home() / 'transfers' / token
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise LifecycleError('unsafe transfer staging directory')
    seen = set()
    for item in manifest.get('assets', []):
        name = item['name']
        if (name in seen or (name != 'origin' and not name.startswith('assets/'))
                or any(part in ('', '.', '..') for part in name.split('/'))):
            raise LifecycleError('unsafe conversation asset name')
        seen.add(name)
        target = directory / name
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise LifecycleError('unsafe conversation asset staging path')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(directory / 'rollout.jsonl')


def install_transfer(token, manifest, workspace):
    validate_identity(manifest['agent'], manifest['sid'], workspace)
    with launch_claims(workspace, manifest['sid']):
        native_preflight(manifest['agent'], manifest['sid'], workspace, stage_import(token, manifest))
        with native_writer_claim(manifest['sid']) if manifest['agent'] == 'codex' else nullcontext():
            return _install_transfer(token, manifest, workspace)


def _install_transfer(token, manifest, workspace):
    source = Path(stage_import(token, manifest))
    if source.is_symlink():
        raise LifecycleError('unsafe staged transcript')
    data = source.read_bytes()
    if len(data) != manifest['size'] or hashlib.sha256(data).hexdigest() != manifest['sha256']:
        raise LifecycleError('transferred transcript checksum differs; no destination started')
    agent, sid = manifest['agent'], manifest['sid']
    verify_transcript(agent, sid, manifest['workspace'], data)
    if agent == 'codex':
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        try:
            destination = selected_rollout(sid)
        except LifecycleError:
            destination = home / 'sessions/1970/01/01' / ('rollout-1970-01-01T00-00-00-' + sid + '.jsonl')
    else:
        encoded = re.sub(r'[^A-Za-z0-9]', '-', workspace)
        destination = Path.home() / '.claude/projects' / encoded / (sid + '.jsonl')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise LifecycleError('unsafe destination transcript')
    if any(parent.is_symlink() for parent in destination.parents):
        # Resolve an explicitly configured store root once; nested links are unsafe.
        root = (Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') if agent == 'codex' else Path.home() / '.claude').resolve()
        if not destination.resolve().is_relative_to(root):
            raise LifecycleError('destination transcript escapes its store')
    assets = []
    for item in manifest.get('assets', []):
        staged = source.parent / item['name']
        content = staged.read_bytes()
        if staged.is_symlink() or len(content) != item['size'] or hashlib.sha256(content).hexdigest() != item['sha256']:
            raise LifecycleError('conversation asset checksum differs; no destination started')
        target = destination.with_suffix('.origin') if item['name'] == 'origin' else destination.parent / sid / item['name'][7:]
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != Path(os.environ.get('CODEX_HOME', '/nonexistent'))):
            raise LifecycleError('unsafe destination conversation asset')
        if target.exists() and target.read_bytes() != content and not content.startswith(target.read_bytes()):
            raise LifecycleError('destination conversation asset diverges; both copies retained')
        assets.append((target, content))
    if destination.exists():
        old = destination.read_bytes()
        if old != data and not data.startswith(old):
            raise LifecycleError('destination history diverges from the source; both copies retained')
    for target, content in [(destination, data), *assets]:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            pending = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.replace(pending, target)
            finally:
                pending.unlink(missing_ok=True)
    return str(destination)


def transfer_copy(adapter, source, destination, manifest, staging):
    adapter.call('copy', source, manifest['path'], destination, staging)
    for item in manifest.get('assets', []):
        adapter.call('copy', source, item['path'], destination, str(Path(staging).parent / item['name']))
    return True


def peer_request(adapter, host, operation, stage, *args):
    request = dict(version=PROTOCOL, id=operation.token,
                   identity=operation.value['arguments'][2:5], stage=stage, args=list(args))
    return json.loads(adapter.call('peer', host, base64.b64encode(json.dumps(request).encode()).decode()))


def execute_peer(request, adapter):
    if (request.get('version') != PROTOCOL or request.get('stage') not in
        ('inspect', 'inspect-saved', 'reserve', 'prepare', 'stop', 'released', 'publish', 'manifest',
         'stage', 'preflight', 'install', 'launch', 'ready', 'finish', 'release-source', 'status')):
        raise LifecycleError('unsupported lifecycle peer protocol; update t before moving')
    stage, arguments = request['stage'], request['args']
    workspace, sid, agent = request['identity']
    validate_identity(agent, sid, workspace)
    operation = Operation('peer', request['identity'], token=request['id'], directory=state_home() / 'peers')
    # Host-local receipts make retries of lost SSH replies idempotent.
    with claim(operation.path.parent / 'claims', operation.token):
        operation = Operation('peer', request['identity'], token=request['id'], directory=state_home() / 'peers')
        if stage == 'status':
            return operation.value
        def effect():
            if stage in ('inspect', 'inspect-saved'):
                workspace, sid, agent = arguments
                if stage == 'inspect-saved':
                    native_preflight(agent, sid, workspace)
                origin = adapter.call('peer-' + stage, operation.token, *arguments)
                return dict(origin=origin, **workspace_evidence(workspace))
            if stage == 'released':
                wait_released(*arguments)
                workspace, sid, agent = request['identity']
                adapter.call('peer-released', operation.token, workspace, sid, agent)
                native_preflight(agent, sid, workspace)
                adapter.call('record', 'handoff', '', workspace, agent, sid)
                return True
            if stage == 'ready':
                result = adapter.call('peer-ready', operation.token, *arguments)
                verify_ready(arguments[3], arguments[2])
                return result
            if stage == 'manifest':
                agent, sid, workspace = arguments
                if agent == 'codex':
                    STORE['rollout_integrity'](os.environ.get('CODEX_HOME') or str(Path.home() / '.codex'), sid)
                filename = adapter.call('transcript', agent, sid, workspace)
                return transcript_manifest(agent, sid, workspace, filename)
            if stage == 'stage':
                return stage_import(operation.token, arguments[0])
            if stage == 'preflight':
                manifest, workspace = arguments
                native_preflight(manifest['agent'], manifest['sid'], workspace, stage_import(operation.token, manifest))
                return True
            if stage == 'install':
                manifest, workspace = arguments
                return install_transfer(operation.token, manifest, workspace)
            if stage == 'prepare':
                source_preflight(arguments[0])
                return adapter.call('peer-prepare', operation.token, *arguments)
            if stage == 'publish':
                workspace, sid, ref = arguments
                revision = operation.step('snapshot', lambda: checkpoint(workspace, sid, operation.value['receipts'].get('inspect') or operation.value['receipts'].get('inspect-saved')))
                existing = git(workspace, 'ls-remote', '--refs', 'origin', ref)
                if existing and existing.split()[0] != revision:
                    raise LifecycleError('transfer snapshot ref changed; source work retained')
                if not existing:
                    git(workspace, 'push', '--force-with-lease=' + ref + ':', 'origin', revision + ':' + ref)
                return revision
            return adapter.call('peer-' + stage, operation.token, *arguments)
        # Inspection and readiness are observations, never cached as current truth.
        return operation.step(stage, effect, repeat=stage in ('inspect', 'released', 'ready', 'preflight', 'manifest', 'stage'))


def move(operation, adapter):
    source, destination, workspace, sid, agent, repo, slot, detach = operation.value['arguments']
    receipts = operation.value['receipts']
    peer = lambda host, stage, *args: peer_request(adapter, host, operation, stage, *args)
    saved = operation.value['verb'] == 'restore'
    if 'stop' not in receipts:
        # A lost stop acknowledgement is retried at the peer, which reconciles
        # the exact selected owner before repeating any effect.
        if operation.value.get('pending') != 'stop':
            source_info = operation.step('inspect', lambda: peer(source, 'inspect-saved' if saved else 'inspect', workspace, sid, agent), repeat=True)
            operation.step('capability', lambda: adapter.call('capability', destination), repeat=True)
            reservation = operation.step('reserve', lambda: peer(destination, 'reserve', source_info['origin'], repo, slot, sid))
            operation.step('prepare', lambda: peer(source, 'prepare', workspace))
            preliminary = operation.step('preflight-manifest', lambda: peer(source, 'manifest', agent, sid, workspace), repeat=True)
            staging = operation.step('preflight-stage', lambda: peer(destination, 'stage', preliminary), repeat=True)
            operation.step('preflight-copy', lambda: transfer_copy(adapter, source, destination, preliminary, staging), repeat=True)
            operation.step('preflight', lambda: peer(destination, 'preflight', preliminary, reservation), repeat=True)
        operation.step('stop', lambda: True if saved else peer(source, 'stop', workspace, sid, agent))
    reservation, ref = receipts['reserve'], receipts['prepare']
    operation.step('released', lambda: peer(source, 'released', agent, sid), repeat=True)
    operation.step('publish', lambda: peer(source, 'publish', workspace, sid, ref))
    manifest = operation.step('manifest', lambda: peer(source, 'manifest', agent, sid, workspace))
    staging = operation.step('stage', lambda: peer(destination, 'stage', manifest))
    operation.step('copy', lambda: transfer_copy(adapter, source, destination, manifest, staging))
    landed = operation.step('install', lambda: peer(destination, 'install', manifest, reservation))
    session = operation.step('launch', lambda: peer(destination, 'launch', reservation, sid, agent, ref))
    operation.step('ready', lambda: peer(destination, 'ready', reservation, session, sid, agent), repeat=True)
    try:
        operation.step('cleanup', lambda: peer(destination, 'finish', reservation, ref))
        operation.step('release-source', lambda: peer(source, 'release-source', workspace))
    except (ValueError, OSError) as error:
        operation.save('cleanup-pending', error=str(error), result=session)
        adapter.output('Conversation ready; snapshot cleanup is pending. Retry: t resume ' + repo + ' ' + slot)
        return session
    operation.save('complete', result=session, pending=None, error=None)
    adapter.output('✓ Running ' + sid[:8] + ' on ' + (destination or 'local') + ' as ' + session)
    if detach != '1':
        adapter.call('attach', destination, session, detach)
    if saved:
        adapter.output(session)
    return session


def execute(verb, arguments, adapter, *, token=None, directory=None):
    operation = Operation(verb, arguments, token=token, directory=directory)
    with claim(operation.path.parent / 'claims', operation.token):
        operation = Operation(verb, arguments, token=operation.token, directory=directory)
        if operation.value['phase'] == 'complete':
            return operation.value.get('result', '')
        try:
            if verb in ('move', 'restore'):
                return move(operation, adapter)
            elif verb in ('new', 'resume', 'replace'):
                session, workspace = arguments[:2]
                agent = arguments[4] if verb == 'new' and len(arguments) > 4 else (
                    arguments[3] if verb in ('resume', 'replace') and len(arguments) > 3 else 'claude')
                sid = arguments[2] if verb in ('resume', 'replace') else ''
                if 'launch' not in operation.value['receipts'] and operation.value.get('pending') != 'launch':
                    operation.step('inspect', lambda: adapter.call('replace-preflight', *arguments) if verb == 'replace' else adapter.call('preflight', session, workspace, sid), repeat=True)
                    operation.step('ownership', lambda: native_preflight(agent, sid, workspace), repeat=True)
                    operation.step('launch', lambda: adapter.call(verb, *arguments))
                result = operation.step('ready', lambda: adapter.call('ready', session, workspace, agent, sid), repeat=True)
                operation.step('writer-ready', lambda: verify_ready(agent, sid), repeat=True)
                operation.step('record', lambda: adapter.call('record', verb, session, workspace, agent, sid))
            elif verb == 'close':
                session, workspace, sid, agent = arguments
                if 'stop' not in operation.value['receipts']:
                    owner = operation.step('inspect', lambda: adapter.call('close-check', *arguments), repeat=True)
                    operation.step('stop', lambda: adapter.call('stop', session, workspace, owner))
                operation.step('released', lambda: wait_released(agent, sid), repeat=True)
                operation.step('record', lambda: adapter.call('record', 'close', session, workspace, agent, sid))
                result = session
            else:
                raise LifecycleError('unsupported lifecycle operation')
            operation.save('complete', result=result, pending=None, error=None)
            return result
        except (ValueError, OSError, sqlite3.Error) as error:
            operation.save('failed', error=str(error))
            if verb in ('move', 'restore'):
                adapter.output('Transfer paused with work retained. Retry: t resume ' + arguments[5] + ' ' + arguments[6])
            raise


def main(argv):
    if argv and argv[0] == 'preflight':
        native_preflight(*argv[1:4])
        return 0
    if argv == ['protocol']:
        print(PROTOCOL)
        return 0
    if argv and argv[0] == 'background':
        token = uuid.uuid4().hex
        folder = state_home() / 'logs'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_') and k not in
               ('TMUX', 'TMUX_PANE', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_RESUME_ID',
                'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CURSOR_SESSION_ID', 'CURSOR_AGENT')}
        env['T_LIFECYCLE_ID'] = token
        command = 'source ' + shlex.quote(str(ROOT / 't.plugin.zsh')) + '; ' + shlex.join(['_t_lifecycle_run', *argv[1:]])
        logfile = folder / (token + '.log')
        with logfile.open('x') as stream:
            os.chmod(logfile, 0o600)
            subprocess.Popen(['zsh', '-lic', command], env=env, stdin=subprocess.DEVNULL,
                             stdout=stream, stderr=stream, start_new_session=True)
        print('Move started independently of this session. Progress: ' + str(logfile))
        return 0
    if argv and argv[0] == 'pending':
        repo, slot = argv[1:3]
        pending = []
        for path in (state_home() / 'operations').glob('*.json'):
            if path.is_symlink():
                continue
            value = json.loads(path.read_text())
            if value.get('verb') in ('move', 'restore') and value.get('phase') not in TERMINAL:
                args = value.get('arguments', [])
                if len(args) == 8 and args[5:7] == [repo, slot]:
                    pending.append(value)
        if not pending:
            return 3
        if len(pending) != 1:
            raise LifecycleError('multiple interrupted moves match this slot; inspect retained lifecycle records')
        value = pending[0]
        print(shlex.join(['env', 'T_LIFECYCLE_ID=' + value['id'], 'zsh', '-lic',
            'source ' + shlex.quote(str(ROOT / 't.plugin.zsh')) + '; ' +
            shlex.join(['_t_lifecycle_run', value['verb'], *value['arguments']])]))
        return 0
    if argv and argv[0] == 'decode':
        frame = json.loads(argv[1])
        if 'call' in frame:
            # The only executable is our fixed dispatcher; every argument is quoted.
            print(shlex.join(['_t_lifecycle_effect', frame['call'], *frame['args']]))
        elif 'output' in frame:
            print(shlex.join(['print', '-r', '--', frame['output']]))
        return 0
    if argv and argv[0] == 'reply':
        print(json.dumps(dict(code=int(argv[1]), output=Path(argv[2]).read_text(errors='replace'))))
        return 0
    if argv and argv[0] == 'bridge':
        adapter = ShellAdapter()
        code = 0
        try:
            if argv[1] == 'peer':
                result = execute_peer(json.loads(base64.b64decode(argv[2])), adapter)
                adapter.output(json.dumps(result))
            else:
                execute(argv[1], argv[2:], adapter, token=os.environ.get('T_LIFECYCLE_ID'))
        except (ValueError, OSError, sqlite3.Error) as error:
            print('t: ' + str(error), file=sys.stderr)
            code = getattr(error, 'code', 1)
        print(json.dumps(dict(done=code)), flush=True)
        return code
    raise LifecycleError('invalid lifecycle helper request')


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, OSError, sqlite3.Error) as error:
        print('t: ' + str(error), file=sys.stderr)
        sys.exit(getattr(error, 'code', 1))
