"""Selected-source resume checkpoints and private, versioned SSH protocol."""
import base64
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

LIFECYCLE = ('launch', 'resume', 'close', 'handoff', 'checkpoint')
MAX_BUNDLE = 64 * 1024 * 1024


def selected(row):
    durable = [a for a in row.get('associations', []) if a.get('evidence') in LIFECYCLE]
    return max(durable, key=lambda a: a.get('observed', 0), default=None)


def private_path(name):
    parts = Path(name).parts
    leaf = parts[-1].lower()
    return (any(p in ('.codex', '.claude', '.ssh', '.aws', '.gnupg') for p in parts)
            or leaf in ('credentials', 'credentials.json', 'auth.json', '.netrc', '.npmrc', '.pypirc')
            or leaf == '.env' or leaf.startswith('.env.') and leaf not in ('.env.example', '.env.sample', '.env.template')
            or leaf.endswith(('.pem', '.key', '.p12', '.pfx')) or leaf.startswith(('id_rsa', 'id_ed25519')))


@contextmanager
def index_claim(index, error):
    lock = Path(str(index) + '.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as cause:
        raise error('source index is being changed by Git; retry when it is idle') from cause
    os.close(descriptor)
    try:
        yield lock
    finally:
        lock.unlink(missing_ok=True)


def checkpoint(history, row, association):
    """Checkpoint through a separate index, retaining the original on any failure."""
    error = history.error
    _spinner = history.spinner
    workspace = association['workspace']
    git = lambda *args, **kwargs: history.run(['git', '-C', workspace, *args], **kwargs).strip()
    head = git('rev-parse', 'HEAD')
    branch = git('branch', '--show-current')
    if not branch or branch != association.get('branch'):
        raise error('source branch changed; no checkpoint transferred')
    recorded = association.get('commit', '')
    if not re.fullmatch(r'[a-fA-F0-9]{40,64}', recorded):
        raise error('source revision is unrelated to the recorded conversation')
    if git('merge-base', recorded, head, optional=True) != recorded:
        raise error('source revision is unrelated to the recorded conversation')
    gitdir = Path(git('rev-parse', '--absolute-git-dir'))
    if any((gitdir / name).exists() for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer')) or git('ls-files', '-u'):
        raise error('source has an unfinished merge, rebase or conflict')
    status = history.run(['git', '-C', workspace, 'status', '--porcelain=v1', '-z', '--untracked-files=all'])
    if not status:
        return head, False
    paths = history.run(['git', '-C', workspace, 'diff', '--name-only', '-z', 'HEAD']) + history.run(['git', '-C', workspace, 'ls-files', '--others', '--exclude-standard', '-z'])
    if any(private_path(name) for name in paths.split('\0') if name):
        raise error('source contains private credentials or agent artifacts; commit or remove them explicitly before resuming')
    index = Path(git('rev-parse', '--git-path', 'index'))
    if not index.is_absolute():
        index = Path(workspace) / index
    original = index.read_bytes() if index.exists() else None
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    with index_claim(index, error) as index_lock, tempfile.TemporaryDirectory(prefix='t-checkpoint-', dir=gitdir) as directory:
        os.chmod(directory, 0o700)
        temporary = Path(directory) / 'index'
        if original is not None:
            temporary.write_bytes(original)
        env['GIT_INDEX_FILE'] = str(temporary)
        def indexed(*args):
            with _spinner('Checkpointing source work'):
                result = subprocess.run(['git', '-C', workspace, *args], env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise error('source checkpoint failed (Git identity or hooks); original index and work retained')
            return result.stdout.strip()
        indexed('read-tree', head)
        indexed('add', '-A', '--', '.')
        tree = indexed('write-tree')
        # Repeat staging to detect changes while files were being collected.
        indexed('add', '-A', '--', '.')
        if indexed('write-tree') != tree or git('rev-parse', 'HEAD') != head or git('branch', '--show-current') != branch or (index.read_bytes() if index.exists() else None) != original:
            raise error('source changed during checkpoint; original index and work retained')
        staged_paths = indexed('diff', '--cached', '--name-only', '-z', head)
        if any(private_path(name) for name in staged_paths.split('\0') if name):
            raise error('source contains private credentials or agent artifacts; no checkpoint transferred')
        indexed('commit', '-m', 't: checkpoint work before resume (' + row['sid'] + ')')
        committed = git('rev-parse', 'HEAD')
        if git('rev-parse', 'HEAD^{tree}') != tree or git('branch', '--show-current') != branch or (index.read_bytes() if index.exists() else None) != original:
            raise error('source changed during checkpoint; checkpoint retained locally, no transfer')
        # Successful checkpoints include all changes, so publish their clean index.
        os.replace(temporary, index_lock)
        os.replace(index_lock, index)
        if git('status', '--porcelain=v1', '--untracked-files=all'):
            raise error('source changed after checkpoint; checkpoint retained locally, no transfer')
        return committed, True


@contextmanager
def prepared_source(history, request, *, historical_fallback=False):
    """Validate and checkpoint a selected source while holding its ownership claims."""
    error = history.error
    if not isinstance(request, dict) or request.get('version') != 1:
        raise error('unsupported source reconciliation request')
    row = request.get('identity', {})
    history.validate(row)
    expected = request.get('association', {})
    matches = [r for r in history.store.records() if r['sid'] == row['sid'] and r['agent'] == row['agent'] and history.same_repository(r['repo'], row['repo'])]
    if len(matches) != 1:
        raise error('source conversation identity is missing or ambiguous')
    saved = matches[0]
    association = selected(saved)
    if not association or association.get('host') != 'local' or any(association.get(k) != expected.get(k) for k in ('workspace', 'generation', 'branch', 'commit', 'observed')):
        raise error('source conversation association changed; select history again')
    workspace = association['workspace']
    with history.launch_claims(workspace, row['sid']):
        if not association.get('generation') or association['generation'] != history.generation(workspace):
            if historical_fallback:
                yield None
                return
            raise error('source workspace generation changed')
        latest = []
        for record in history.store.records():
            for a in record.get('associations', []):
                if a.get('host') == 'local' and a.get('evidence') in LIFECYCLE and os.path.realpath(a['workspace']) == os.path.realpath(workspace):
                    latest.append((a.get('observed', 0), record['sid'], record['agent']))
        if max(latest, default=(0, '', ''))[1:] != (row['sid'], row['agent']):
            if historical_fallback:
                yield None
                return
            raise error('source workspace belongs to another conversation')
        actual = history.run(['git', '-C', workspace, 'rev-parse', '--show-toplevel']).strip()
        if os.path.realpath(actual) != os.path.realpath(workspace):
            raise error('source workspace is not the recorded Git root')
        identity = history.slug(history.run(['git', '-C', workspace, 'remote', 'get-url', 'origin']).strip())
        if not identity or not history.same_repository(identity, row['repo']):
            raise error('source repository identity changed')
        def released():
            if any(o['sid'] == row['sid'] or os.path.realpath(o['cwd']) == os.path.realpath(workspace) for o in history.owners()):
                raise error('source conversation or workspace has a live owner')
            local_saved = {**saved, 'sources': [s for s in saved.get('sources', []) if s.get('host') == 'local']}
            history.codex_released(local_saved, history.source(local_saved), workspace=workspace)
        released()
        native = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'thread-writer-locks'
        with history.native_writer_claim(row['sid']) if row['agent'] == 'codex' and native.exists() else nullcontext():
            revision, changed = checkpoint(history, row, association)
        released()
        yield workspace, revision, changed, released


def export(history, request):
    """Bundle a validated checkpoint for the private cross-host protocol."""
    error = history.error
    with prepared_source(history, request) as prepared:
        workspace, revision, changed, released = prepared
        with tempfile.TemporaryDirectory(prefix='t-resume-') as directory:
            os.chmod(directory, 0o700)
            bundle = Path(directory) / 'source.bundle'
            # HEAD is advertised without creating or modifying a public branch.
            history.run(['git', '-C', workspace, 'bundle', 'create', str(bundle), 'HEAD'])
            if bundle.stat().st_size > MAX_BUNDLE:
                raise error('source bundle exceeds the transfer limit; checkpoint retained locally')
            advertised = history.run(['git', 'bundle', 'list-heads', str(bundle)]).strip()
            if advertised != revision + ' HEAD' or history.run(['git', '-C', workspace, 'rev-parse', 'HEAD']).strip() != revision or history.run(['git', '-C', workspace, 'status', '--porcelain=v1', '--untracked-files=all']):
                raise error('source changed during bundle creation; checkpoint retained locally, no transfer')
            data = bundle.read_bytes()
        released()
        return dict(version=1, identity=request['identity'], association=request['association'], revision=revision, checkpoint=changed,
                    token=uuid.uuid4().hex, sha256=hashlib.sha256(data).hexdigest(), bundle=base64.b64encode(data).decode())


def reconcile(history, alias, row):
    _spinner = history.spinner
    association = selected(row)
    if not association:
        return ''
    if association.get('host') == 'local':
        # Missing/replaced workspaces have no current work attributable to this
        # conversation. Keep historical fallback without touching replacement files.
        workspace = association.get('workspace', '')
        if not association.get('generation') or association['generation'] != history.generation(workspace):
            return ''
        request = dict(version=1, identity={k: row[k] for k in ('repo', 'agent', 'sid')}, association=association)
        with prepared_source(history, request, historical_fallback=True) as prepared:
            if prepared is None:
                return ''
            workspace, revision, changed, released = prepared
            repository = history.cfg.repos[alias]
            if history.run(['git', '-C', repository, 'cat-file', '-t', revision], optional=True).strip() != 'commit':
                # A verified local source can be a separate clone. Carry its
                # objects before allocating a destination, without a bundle.
                reference = 'refs/t/resume/' + uuid.uuid4().hex
                try:
                    history.run(['git', '-C', repository, 'fetch', '--no-tags', workspace, revision + ':' + reference])
                    if history.run(['git', '-C', repository, 'rev-parse', reference + '^{commit}']).strip() != revision:
                        raise history.error('imported local source revision mismatch; no session started')
                finally:
                    history.run(['git', '-C', repository, 'update-ref', '-d', reference], optional=True)
            if history.run(['git', '-C', workspace, 'rev-parse', 'HEAD']).strip() != revision or history.run(['git', '-C', workspace, 'branch', '--show-current']).strip() != association.get('branch') or history.run(['git', '-C', workspace, 'status', '--porcelain=v1', '--untracked-files=all']):
                raise history.error('source changed during local import; no session started')
            released()
            if changed:
                print('t resume: carried checkpoint ' + revision, file=sys.stderr)
            return revision
    host = association.get('host', '')
    def unavailable():
        print('t resume: ' + host + ' unavailable; continuing with locally available code and history (a source checkpoint may have been retained)', file=sys.stderr)
        return ''
    if host not in history.cfg.hosts:
        return unavailable()
    request = dict(version=1, identity={k: row[k] for k in ('repo', 'agent', 'sid')}, association=association)
    import shlex
    command = 'command t __resume-source ' + shlex.quote(json.dumps(request, separators=(',', ':')))
    with _spinner('Reconciling source branch and code'):
        try:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', history.cfg.hosts[host],
                                     'zsh -lic ' + shlex.quote(command)], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=90,
                                    env={k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')})
        except (OSError, subprocess.TimeoutExpired):
            return unavailable()
    if result.returncode == 255 or result.returncode in (2, 127) and ('__resume-source' in result.stderr and ('invalid choice' in result.stderr or 'unknown' in result.stderr) or 'command not found: t' in result.stderr):
        return unavailable()
    if result.returncode or len(result.stdout) > MAX_BUNDLE * 2:
        raise history.error('source reconciliation failed; no session started')
    try:
        value = json.loads(result.stdout)
        if value.get('error'):
            raise history.error('source refused resume: ' + str(value['error']))
        if value.get('version') != 1 or value.get('identity') != request['identity'] or value.get('association') != association or not re.fullmatch(r'[a-f0-9]{32}', value.get('token', '')) or not re.fullmatch(r'[a-f0-9]{40,64}', value.get('revision', '')):
            raise ValueError('manifest identity mismatch')
        data = base64.b64decode(value['bundle'], validate=True)
        if len(data) > MAX_BUNDLE or hashlib.sha256(data).hexdigest() != value['sha256']:
            raise ValueError('bundle hash mismatch')
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        # HistoryError derives from ValueError; retain its actionable refusal.
        if isinstance(error, history.error):
            raise
        raise history.error('invalid source checkpoint artifact; no session started') from error
    repository = history.cfg.repos[alias]
    with tempfile.TemporaryDirectory(prefix='resume-', dir=history.store.directory) as directory:
        os.chmod(directory, 0o700)
        bundle = Path(directory) / 'source.bundle'
        bundle.write_bytes(data)
        os.chmod(bundle, 0o600)
        history.run(['git', '-C', repository, 'bundle', 'verify', str(bundle)])
        advertised = history.run(['git', 'bundle', 'list-heads', str(bundle)]).strip()
        if advertised != value['revision'] + ' HEAD':
            raise history.error('source bundle revision mismatch')
        reference = 'refs/t/resume/' + value['token']
        try:
            history.run(['git', '-C', repository, 'fetch', '--no-tags', str(bundle), 'HEAD:' + reference])
            if history.run(['git', '-C', repository, 'rev-parse', reference + '^{commit}']).strip() != value['revision']:
                raise history.error('imported source revision mismatch')
            # Other local evidence for this conversation must not diverge silently.
            for a in row.get('associations', []):
                commit = a.get('commit', '')
                workspace = a.get('workspace', '')
                if a.get('host') == 'local' and a.get('generation') and a['generation'] == history.generation(workspace):
                    actual_branch = history.run(['git', '-C', workspace, 'branch', '--show-current'], optional=True).strip()
                    # Only exact durable local ownership can strengthen cached evidence.
                    local = [(b.get('observed', 0), r['sid'], r['agent']) for r in history.store.records()
                             for b in r.get('associations', []) if b.get('host') == 'local' and b.get('evidence') in LIFECYCLE
                             and os.path.realpath(b['workspace']) == os.path.realpath(workspace)]
                    if actual_branch == a.get('branch') and max(local, default=(0, '', ''))[1:] == (row['sid'], row['agent']):
                        commit = history.run(['git', '-C', workspace, 'rev-parse', 'HEAD']).strip()
                if a.get('host') == 'local' and re.fullmatch(r'[a-fA-F0-9]{40,64}', commit) and history.run(['git', '-C', repository, 'cat-file', '-t', commit], optional=True).strip() == 'commit':
                    base = history.run(['git', '-C', repository, 'merge-base', commit, value['revision']], optional=True).strip()
                    if base != commit:
                        raise history.error('local and source conversation revisions diverged; reconcile them explicitly before resume')
        finally:
            history.run(['git', '-C', repository, 'update-ref', '-d', reference], optional=True)
    print('t resume: carried ' + ('checkpoint ' if value.get('checkpoint') else 'source commit ') + value['revision'], file=sys.stderr)
    return value['revision']
