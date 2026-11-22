"""Private idea workspaces, native conversations and reviewed GitHub publication.

Only publish/ is eligible for export. The reviewed snapshot and staging Git
repository live beside (never inside) the agent workspace. All mutations are
serialized; an uncertain remote creation is never adopted by name.
"""
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r'idea-[0-9a-f]{32}\Z')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z')
SID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
MAX_FILE = 100 * 1024 * 1024


class IdeaError(ValueError):
    pass


def module(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


_spinner = module('t_progress')['spinner']


def state_root():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/ideas'


def clean_env():
    identities = {'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_RESUME_ID',
                  'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CURSOR_SESSION_ID',
                  'CURSOR_AGENT', 'DEV_AGENT', 'TMUX', 'TMUX_PANE', 'T_SESSION_ID',
                  'T_WORKFLOW_CWD', 'T_LAUNCH_CONTEXT', 'T_ISSUE_TOKEN'}
    return {k: v for k, v in os.environ.items() if k not in identities and
            (not k.startswith('GIT_') or k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM', 'GIT_SSH_COMMAND', 'GIT_SSH')) and not k.startswith(('COV_CORE_', 'T_RESUME_', 'T_FRESH_', 'T_LAUNCH_', 'T_TITLE_'))}


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise IdeaError('unsafe idea directory: ' + str(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise IdeaError('idea directory is not owned by this user')
    os.chmod(path, 0o700)


def _folder(identifier):
    if not isinstance(identifier, str) or not ID.fullmatch(identifier):
        raise IdeaError('use the exact idea ID shown by t idea list')
    root = state_root().absolute()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        # macOS /var is commonly a system symlink; only reject user state paths
        # whose resolved workspace would be different from its recorded identity.
        root = root.resolve()
    folder = root / identifier
    if folder.is_symlink():
        raise IdeaError('unsafe idea directory')
    return folder


def save(record):
    folder = _folder(record['id'])
    _private_directory(folder)
    record['updated_at'] = time.time()
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, ensure_ascii=True, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / 'idea.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(identifier):
    folder = _folder(identifier)
    path = folder / 'idea.json'
    if path.is_symlink():
        raise IdeaError('unsafe idea metadata')
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise IdeaError('idea not found or unreadable: ' + identifier) from error
    if (record.get('id') != identifier or record.get('agent') not in ('claude', 'codex') or
            record.get('workspace') != str(folder / 'workspace')):
        raise IdeaError('invalid idea metadata')
    workspace = Path(record['workspace'])
    if workspace.is_symlink() or not workspace.is_dir():
        raise IdeaError('idea workspace is missing or unsafe')
    return record


def list_ideas():
    root = state_root()
    if not root.exists():
        return []
    if root.is_symlink():
        raise IdeaError('unsafe ideas state directory')
    records = []
    for path in root.glob('idea-*'):
        if ID.fullmatch(path.name):
            records.append(load(path.name))
    return sorted(records, key=lambda r: (-r.get('updated_at', 0), r['id']))


def create(title, agent):
    if agent not in ('claude', 'codex'):
        raise IdeaError('ideation supports the configured Codex or Claude default')
    title = str(title).strip()
    if not title or len(title) > 200 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in title):
        raise IdeaError('idea title must be 1–200 printable characters')
    root = state_root().absolute().resolve()
    # The workspace must never inherit a parent repository, including a user-
    # configured XDG_STATE_HOME placed beneath a checkout.
    if any((p / '.git').exists() or (p / '.git').is_symlink() for p in (root, *root.parents)):
        raise IdeaError('XDG state directory is inside a Git repository; choose one outside Git')
    _private_directory(root)
    identifier = 'idea-' + uuid.uuid4().hex
    folder = root / identifier
    folder.mkdir(mode=0o700)
    workspace = folder / 'workspace'
    workspace.mkdir(mode=0o700)
    (workspace / 'publish').mkdir(mode=0o700)
    info = workspace.stat()
    record = dict(version=1, id=identifier, title=title, agent=agent, workspace=str(workspace),
                  workspace_identity=f'{info.st_dev}:{info.st_ino}', status='draft',
                  conversation_id='', created_at=time.time(), publication=None)
    save(record)
    return record


@contextmanager
def idea_lock(record):
    path = _folder(record['id']) / 'operation.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IdeaError('this idea is busy with an active operation') from error
        yield
    finally:
        os.close(fd)


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise IdeaError('unsafe publication path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') or p.lower() == '.git' or p.lower().startswith('.git:') for p in value.split('/')):
        raise IdeaError('unsafe publication path: ' + value)
    return path


def _read_file(record, name):
    relative = _relative(name)
    workspace = Path(record['workspace'])
    descriptors = []
    try:
        fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        descriptors.append(fd)
        for part in ('publish', *relative.parts[:-1]):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
            descriptors.append(fd)
        file_fd = os.open(relative.name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=fd)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise IdeaError('publication requires regular files under 100 MiB: ' + name)
        with os.fdopen(os.dup(file_fd), 'rb') as stream:
            data = stream.read(MAX_FILE + 1)
        after = os.fstat(file_fd)
        if len(data) > MAX_FILE or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise IdeaError('file changed during review: ' + name)
        return data, 0o755 if before.st_mode & 0o111 else 0o644
    except OSError as error:
        raise IdeaError('unsafe or unreadable publication file: ' + name) from error
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def eligible_files(record):
    publish = Path(record['workspace']) / 'publish'
    if publish.is_symlink() or not publish.is_dir():
        raise IdeaError('publish/ must be a real directory')
    files = []
    for directory, children, names in os.walk(publish, followlinks=False):
        children[:] = sorted(c for c in children if c.lower() != '.git' and not (Path(directory) / c).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(publish).as_posix()
            try:
                _relative(relative)
            except IdeaError:
                continue
            if stat.S_ISREG(path.lstat().st_mode) and path.stat().st_size <= MAX_FILE:
                files.append(relative)
    return sorted(files)


def snapshot(record, paths):
    paths = sorted(set(paths))
    folder = _folder(record['id']) / ('review-' + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    entries = []
    try:
        for name in paths:
            data, mode = _read_file(record, name)
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            target.chmod(mode & ~0o222)
            entries.append(dict(path=name, sha256=hashlib.sha256(data).hexdigest(), size=len(data), mode=mode))
    except BaseException:
        shutil.rmtree(folder)
        raise
    return dict(path=str(folder), files=entries)


def _run(argv, *, cwd=None, check=True, timeout=120, env=None, text=True):
    tool = Path(argv[0]).name
    message = {'gh': 'Checking GitHub', 'git': 'Preparing repository'}.get(tool, 'Checking agent readiness')
    try:
        with _spinner(message):
            result = subprocess.run([str(a) for a in argv], cwd=cwd, env=clean_env() if env is None else env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=text, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IdeaError(f'cannot run {argv[0]}: {error}') from error
    if check and result.returncode:
        raise IdeaError(result.stderr.strip() or result.stdout.strip() or f'{argv[0]} exited {result.returncode}')
    return result


def _git(path, *args, check=True):
    return _run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                 '-c', 'core.autocrlf=false', '-C', path, *args], check=check).stdout.strip()


def _native_candidates(record):
    api = module('t_share_sessions')
    # Do not turn an unreadable native index into evidence of absence when
    # deciding whether a failed startup can safely be retried.
    if record['agent'] == 'codex':
        database = api['_codex_home']() / 'state_5.sqlite'
        if database.exists():
            try:
                connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
                try:
                    connection.execute('select id,cwd,source from threads limit 1').fetchall()
                finally:
                    connection.close()
            except sqlite3.Error as error:
                raise IdeaError('native Codex identity index is unavailable; no conversation was guessed') from error
    elif record.get('conversation_id'):
        projects = api['_claude_home']() / 'projects'
        if projects.exists():
            try:
                for directory in projects.iterdir():
                    if directory.is_dir():
                        matches = [p for p in directory.iterdir() if p.name == record['conversation_id'] + '.jsonl']
                        for path in matches:
                            with path.open('rb') as stream:
                                stream.read(65536)
            except OSError as error:
                raise IdeaError('native Claude identity records are unavailable; no conversation was guessed') from error
    rows = api['_claude_targets' if record['agent'] == 'claude' else '_codex_targets']()
    found = {row['id']: row for row in rows if row['cwd'] == record['workspace']}
    if record['agent'] == 'codex':
        # Native history fallback must exclude subagents just as the index does.
        # A selected ID is verified against its own native session_meta record.
        for sid in list(found):
            try:
                path = module('t_lifecycle')['selected_rollout'](sid)
                item = json.loads(path.open().readline(65537))
                payload = item.get('payload', {})
                source = payload.get('source', payload.get('thread_source', ''))
                if (item.get('type') != 'session_meta' or payload.get('id') != sid or
                        payload.get('cwd') != record['workspace'] or
                        'subagent' in json.dumps(source).lower()):
                    del found[sid]
            except (OSError, ValueError):
                del found[sid]
    return found


def _launch_arguments(cfg, agent, arguments):
    # Match _dev_agent_default_args: a scoped model clears the account effort,
    # while an explicit scoped effort can then set it again.
    model = getattr(cfg, 'models', {}).get(agent, '')
    effort = getattr(cfg, 'efforts', {}).get(agent, '')
    remaining = []
    index = 0
    while index < len(arguments):
        key = arguments[index]
        value = arguments[index + 1] if index + 1 < len(arguments) else ''
        if key == '--model':
            model, effort = value, ''
        elif key == '--effort':
            effort = value
        elif key == '-c' and value.startswith('model_reasoning_effort='):
            effort = value.partition('=')[2].strip('"')
        else:
            remaining += [key, value]
        index += 2
    result = (['--model', model] if model else [])
    if effort:
        result += ['-c', 'model_reasoning_effort=' + json.dumps(effort)] if agent == 'codex' else ['--effort', effort]
    result += remaining
    if not any('service_tier=' in arg or 'fastMode' in arg for arg in result):
        speed = str(getattr(cfg, 'fast', {}).get(agent, ''))
        if agent == 'codex' and speed in ('0', '1', 'fast', 'ultrafast'):
            result += ['-c', 'service_tier=' + ('default' if speed == '0' else 'ultrafast' if speed == 'ultrafast' else 'fast')]
            if speed != '0':
                result += ['--enable', 'fast_mode']
        elif agent == 'claude' and speed in ('0', '1'):
            result += ['--settings', json.dumps({'fastMode': speed == '1'})]
    return result


def _assert_no_process(record):
    api = module('t_terminal_close')
    # Inspect rooted processes as well as exact SID arguments: an agent may have
    # changed cwd while retaining this conversation's native writer identity.
    sid = record.get('conversation_id', '')
    listing = _run(['ps', '-axo', 'pid=,command=']).stdout
    table = {}
    for line in listing.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            table[int(fields[0])] = fields[1]
    rooted = api['rooted_processes'](record['workspace'])
    for pid, row in table.items():
        command = row.get('command', '') if isinstance(row, dict) else str(row)
        if not re.search(r'(^|[ /])(claude|codex(?:-[^ /]+)?)(?:\s|$)', command):
            continue
        if pid in rooted or sid and sid in command:
            raise IdeaError(f'an existing native agent owns this idea (pid {pid}); exit it before continuing')


def launch(cfg, record, resume=False):
    """Run native I/O in the current terminal; preserve the exact saved identity."""
    with idea_lock(record):
        current = load(record['id'])
        record.clear(); record.update(current)
        if record.get('publication'):
            raise IdeaError('this idea already has a publication in progress; use t idea publish ' + record['id'])
        workspace = Path(record['workspace'])
        if any((p / '.git').exists() or (p / '.git').is_symlink() for p in (workspace, *workspace.parents)):
            raise IdeaError('idea workspace is now inside a Git repository')
        _assert_no_process(record)
        candidates = _native_candidates(record)
        sid = record.get('conversation_id', '')
        if not sid and candidates:
            if len(candidates) != 1:
                raise IdeaError('multiple native conversations claim this workspace; no conversation was guessed')
            sid = next(iter(candidates))
            record['conversation_id'] = sid
            save(record)
        initial_attempt = not record.get('launched')
        failed_start = record.get('last_exit_code') not in (None, 0, 130)
        if record.get('launched') and not candidates and failed_start:
            # Explicit retry after a known exited, failed launch is permitted
            # only after the same native workspace and process checks above.
            record['launched'] = False
            if record['agent'] == 'codex':
                sid = ''
                record['conversation_id'] = ''
        elif record.get('launched') and (not sid or sid not in candidates):
            raise IdeaError('native conversation identity is not yet verified; draft retained at ' + str(workspace))
        lifecycle = module('t_lifecycle')
        with lifecycle['launch_claims'](str(workspace), sid):
            _assert_no_process(record)
            if record['agent'] == 'codex':
                lifecycle['native_preflight']('codex', sid, str(workspace),
                    new_workspace=record['workspace_identity'] if initial_attempt else None,
                    retry='Retry: t idea resume ' + record['id'])
            executable = shutil.which(record['agent'])
            if not executable:
                fallback = Path.home() / '.local/bin' / record['agent']
                executable = str(fallback) if os.access(fallback, os.X_OK) else None
            if not executable:
                raise IdeaError(record['agent'] + ' is not installed; run t agent install ' + record['agent'])
            policy_env = clean_env()
            policy_env.update(T_WORKFLOW_DEFAULT_MODEL=getattr(cfg, 'models', {}).get(record['agent'], ''),
                              T_LAUNCH_DEFAULT_MODEL=getattr(cfg, 'models', {}).get(record['agent'], ''),
                              T_LAUNCH_DEFAULT_EFFORT=getattr(cfg, 'efforts', {}).get(record['agent'], ''),
                              T_LAUNCH_DEFAULT_SPEED=str(getattr(cfg, 'fast', {}).get(record['agent'], '')))
            frame = _run([sys.executable, ROOT / 'libexec/t_launch_policy.py', 'launch', record['agent'], workspace], env=policy_env).stdout.split('\0')
            count = int(frame[0])
            bundle = {'prefix': frame[1:1 + count], 'args': frame[1 + count:]}
            arguments = _launch_arguments(cfg, record['agent'], bundle['args'])
            if record['agent'] == 'claude':
                if not sid:
                    sid = str(uuid.uuid4())
                    record['conversation_id'] = sid
                argv = [executable, '--resume' if record.get('launched') or resume and candidates else '--session-id', sid, *arguments]
            else:
                argv = [executable, *(['resume', sid] if sid else []), *arguments, '--no-daemon']
            if not record.get('launched') and not candidates:
                prompt = ('Brainstorm this idea with me: ' + record['title'] + '\n'
                          'This is a private idea workspace, with no repository yet. Discuss and explore before building. '
                          'Keep private notes here. Put only files intended for eventual repository publication under publish/. '
                          'Do not create a Git repository, authenticate, create a remote, or publish anything. '
                          'I will review each selected file in t before creating a repository.')
                argv += ['--', prompt]
            argv = [*bundle['prefix'], *argv]
            record['launched'] = True
            record['status'] = 'brainstorming'
            save(record)
            result = 130
            try:
                result = subprocess.run(argv, cwd=workspace, env=clean_env()).returncode
                result = 128 - result if result < 0 else result
            except KeyboardInterrupt:
                result = 130
            except OSError as error:
                result = 127
                raise IdeaError('native agent could not start: ' + str(error)) from error
            finally:
                if record['agent'] == 'codex':
                    found = _native_candidates(record)
                    if not sid and len(found) == 1:
                        record['conversation_id'] = next(iter(found))
                    elif sid and sid not in found:
                        record['identity_warning'] = 'Saved conversation is no longer indexed at this workspace'
                record['status'] = 'draft'
                record['last_exit_code'] = result
                save(record)
            return result


def _gh(*args, check=True):
    return _run(['gh', 'api', '--hostname', 'github.com', *args], check=check, timeout=60)


def _json(result):
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise IdeaError('GitHub returned invalid repository identity') from error


def _validate_review(review):
    for key in ('name', 'alias', 'owner'):
        if not NAME.fullmatch(review.get(key, '')) or review[key] in ('.', '..') or review[key].endswith('.git'):
            raise IdeaError('invalid ' + key)
    if type(review.get('private')) is not bool:
        raise IdeaError('invalid repository visibility')
    expected = Path.home() / 'code' / review['name']
    if review.get('path') != str(expected):
        raise IdeaError('publication destination must be ' + str(expected))


def _verify_remote(publication):
    info = _json(_gh('repos/' + publication['slug']))
    if (info.get('id') != publication['repository_id'] or info.get('full_name') != publication['slug'] or
            type(info.get('private')) is not bool or info['private'] != publication['private']):
        raise IdeaError('GitHub repository ID, name or visibility changed; publication stopped')
    return info


def _verify_origin(path, slug):
    allowed = {f'git@github.com:{slug}.git', f'https://github.com/{slug}.git',
               f'ssh://git@github.com/{slug}.git'}
    raw = _git(path, 'config', '--get', 'remote.origin.url')
    fetch = _git(path, 'remote', 'get-url', '--all', 'origin').splitlines()
    push = _git(path, 'remote', 'get-url', '--push', '--all', 'origin').splitlines()
    if len(fetch) != 1 or len(push) != 1 or any(url not in allowed for url in (raw, *fetch, *push)):
        raise IdeaError('repository origin or effective push destination changed')


def _discard_reviews(record, keep=None):
    for folder in _folder(record['id']).glob('review-*'):
        if str(folder) != keep and not folder.is_symlink() and folder.is_dir():
            shutil.rmtree(folder)


def _remote_head(stage):
    result = _run(['git', '-C', stage, 'ls-remote', '--heads', 'origin', 'refs/heads/main'])
    rows = result.stdout.splitlines()
    if not rows:
        return None
    if len(rows) != 1 or not re.fullmatch(r'[0-9a-f]{40,64}\trefs/heads/main', rows[0]):
        raise IdeaError('cannot verify remote main')
    return rows[0].split()[0]


def _stage(record, review):
    reviewed = review['snapshot']
    source = Path(reviewed['path'])
    folder = _folder(record['id'])
    if source.parent != folder or not source.name.startswith('review-') or source.is_symlink():
        raise IdeaError('invalid reviewed snapshot')
    stage = folder / ('stage-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    for entry in reviewed['files']:
        name = str(_relative(entry['path']))
        path = source / name
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != folder.parent):
            raise IdeaError('reviewed snapshot is unsafe')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256'] or len(data) != entry['size']:
            raise IdeaError('reviewed file changed; review files again')
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(entry['mode'])
    _git(stage, 'init', '-q', '-b', 'main')
    # Disable attributes filters from system/global Git config for these exact
    # reviewed bytes, and bypass ignore rules only for explicitly selected files.
    for entry in reviewed['files']:
        name = entry['path']
        blob = _git(stage, 'hash-object', '-w', '--no-filters', '--', name)
        _git(stage, 'update-index', '--add', '--cacheinfo', '100755' if entry['mode'] & 0o111 else '100644', blob, name)
    _git(stage, 'commit', '--allow-empty', '-qm', 'Initial idea')
    return str(stage), _git(stage, 'rev-parse', 'HEAD')


def publish(cfg, record, review, api, *, _already_locked=False):
    """Apply the confirmed review; checkpoints make retries exact and resumable."""
    with nullcontext() if _already_locked else idea_lock(record):
        fresh = load(record['id'])
        record.clear(); record.update(fresh)
        _private_directory(state_root())
        with module('t_lifecycle')['claim'](state_root() / 'publication-locks', 'local-publication'):
            publication = record.get('publication')
            if record['status'] == 'published' and publication and publication.get('registered'):
                return publication
            if not publication:
                _validate_review(review)
                path = Path(review['path'])
                if os.path.lexists(path):
                    raise IdeaError('local destination already exists: ' + str(path))
                if review['alias'] in getattr(cfg, 'repos', {}):
                    raise IdeaError('repository alias already exists: ' + review['alias'])
                collision = _gh('repos/' + review['owner'] + '/' + review['name'], check=False)
                if collision.returncode == 0:
                    raise IdeaError('GitHub repository already exists; choose another name')
                if '404' not in collision.stderr + collision.stdout:
                    raise IdeaError('cannot verify GitHub name availability')
                stage, commit = _stage(record, review)
                publication = {k: review[k] for k in ('owner', 'name', 'private', 'alias', 'path')}
                publication.update(slug=review['owner'] + '/' + review['name'], stage=stage, commit=commit,
                                   files=review['snapshot']['files'], expected_main=None)
                record['publication'] = publication
                record['status'] = 'pending publication'
                save(record)
            stage = publication['stage']
            if _git(stage, 'rev-parse', 'HEAD') != publication['commit']:
                raise IdeaError('approved staging commit changed; publication stopped')
            if not publication.get('repository_id'):
                collision = _gh('repos/' + publication['slug'], check=False)
                if collision.returncode == 0:
                    if not publication.get('creation_attempted'):
                        record['publication'] = None
                        record['status'] = 'draft'
                        save(record)
                        raise IdeaError('GitHub repository already exists; draft retained so you can choose another name')
                    raise IdeaError('GitHub repository already exists; previous creation is unverified and no repository was adopted')
                if '404' not in collision.stderr + collision.stdout:
                    raise IdeaError('cannot verify GitHub name availability: ' + (collision.stderr.strip() or collision.stdout.strip()))
                login = _gh('user', '--jq', '.login').stdout.strip()
                endpoint = 'user/repos' if login == publication['owner'] else 'orgs/' + publication['owner'] + '/repos'
                publication['creation_attempted'] = True
                save(record)
                info = _json(_gh('-X', 'POST', endpoint, '-f', 'name=' + publication['name'],
                                 '-F', 'private=' + str(publication['private']).lower(), '-F', 'auto_init=false'))
                if type(info.get('id')) is not int or info['id'] <= 0:
                    raise IdeaError('GitHub create response has no immutable repository ID')
                publication['repository_id'] = info['id']
                publication['creation_response'] = {'id': info['id'], 'full_name': info.get('full_name'), 'private': info.get('private')}
                # Store the ID even when the response needs investigation; retry
                # verifies ID, slug and visibility before any git operation.
                save(record)
            response = publication.get('creation_response') or {}
            if response and (response.get('full_name') != publication['slug'] or response.get('private') is not publication['private']):
                raise IdeaError('GitHub create response did not match the approved repository identity')
            info = _verify_remote(publication)
            url = info.get('ssh_url') or info.get('clone_url')
            allowed_urls = {f'git@github.com:{publication["slug"]}.git', f'https://github.com/{publication["slug"]}.git'}
            if url not in allowed_urls:
                raise IdeaError('GitHub returned an unexpected clone URL')
            if publication.get('url') and publication['url'] != url:
                raise IdeaError('approved repository origin changed')
            publication['url'] = url
            publication['html_url'] = info.get('html_url') or 'https://github.com/' + publication['slug']
            origin = _git(stage, 'config', '--get', 'remote.origin.url', check=False)
            if origin and origin != url:
                raise IdeaError('staging origin changed')
            if not origin:
                _git(stage, 'remote', 'add', 'origin', url)
            _verify_origin(stage, publication['slug'])
            head = _remote_head(stage)
            if head not in (publication['expected_main'], publication['commit']):
                raise IdeaError('remote main changed since review; no push or clone attempted')
            if head != publication['commit']:
                _git(stage, 'push', 'origin', publication['commit'] + ':refs/heads/main')
            if _remote_head(stage) != publication['commit']:
                raise IdeaError('remote main did not match the approved commit')
            publication['pushed'] = True
            save(record)
            _gh('-X', 'PATCH', 'repos/' + publication['slug'], '-F', 'allow_squash_merge=true',
                '-F', 'allow_merge_commit=false', '-F', 'allow_rebase_merge=false',
                '-F', 'allow_auto_merge=true', '-F', 'delete_branch_on_merge=true', '-f', 'default_branch=main')
            _verify_remote(publication)
            if _remote_head(stage) != publication['commit']:
                raise IdeaError('remote main changed before local clone')
            destination = Path(publication['path'])
            if not publication.get('cloned') and os.path.lexists(destination):
                proof = publication.get('clone_identity')
                info = destination.lstat()
                if not (proof and stat.S_ISDIR(info.st_mode) and proof == [info.st_dev, info.st_ino] and
                        _git(destination, 'config', '--get', 'remote.origin.url') == url and
                        _git(destination, 'rev-parse', 'HEAD') == publication['commit']):
                    raise IdeaError('local destination already exists; it was not overwritten')
                publication['cloned'] = True
                save(record)
            if not publication.get('cloned'):
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.parent.is_symlink():
                    raise IdeaError('local code directory must not be a symlink')
                pending = publication.get('clone_pending')
                if not pending:
                    pending = str(destination.parent / ('.t-idea-clone-' + uuid.uuid4().hex))
                    publication['clone_pending'] = pending
                    save(record)
                pending_path = Path(pending)
                if pending_path.parent != destination.parent or not pending_path.name.startswith('.t-idea-clone-') or pending_path.is_symlink():
                    raise IdeaError('invalid pending clone destination')
                if not pending_path.exists():
                    _run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-checkout', '--', url, pending], cwd=stage, timeout=600)
                if (_git(pending, 'config', '--get', 'remote.origin.url') != url or
                        _git(pending, 'rev-parse', 'refs/remotes/origin/main') != publication['commit']):
                    raise IdeaError('pending local clone does not match the approved repository')
                _verify_origin(pending, publication['slug'])
                # Populate bytes directly from the reviewed Git objects. Native
                # checkout would run user-configured attributes/smudge filters.
                _git(pending, 'update-ref', 'refs/heads/main', publication['commit'])
                _git(pending, 'symbolic-ref', 'HEAD', 'refs/heads/main')
                _git(pending, 'read-tree', publication['commit'])
                for entry in publication['files']:
                    relative = _relative(entry['path'])
                    result = _run(['git', '-C', pending, 'cat-file', 'blob', publication['commit'] + ':' + str(relative)],
                                  check=False, text=False, timeout=60)
                    data = result.stdout
                    if result.returncode or hashlib.sha256(data).hexdigest() != entry['sha256'] or len(data) != entry['size']:
                        raise IdeaError('approved Git object changed before clone completion')
                    directory = pending_path
                    for component in relative.parts[:-1]:
                        directory /= component
                        if directory.is_symlink():
                            raise IdeaError('unsafe local clone directory')
                        directory.mkdir(exist_ok=True)
                    target = directory / relative.name
                    if target.is_symlink():
                        raise IdeaError('unsafe local clone file')
                    target.write_bytes(data)
                    target.chmod(entry['mode'])
                if os.path.lexists(destination):
                    raise IdeaError('local destination appeared during clone; retained pending clone')
                info = pending_path.stat()
                publication['clone_identity'] = [info.st_dev, info.st_ino]
                save(record)
                # Reserve the destination exclusively, then atomically fill only
                # our empty folder. The pending inode proves crash recovery.
                destination.mkdir(mode=0o755)
                try:
                    os.rename(pending_path, destination)
                except BaseException:
                    destination.rmdir()
                    raise
                publication['cloned'] = True
                save(record)
            if (destination.is_symlink() or _git(destination, 'config', '--get', 'remote.origin.url') != url or
                    _git(destination, 'rev-parse', 'HEAD') != publication['commit']):
                raise IdeaError('local clone changed; registration stopped')
            _verify_origin(destination, publication['slug'])
            # Read authoritative local config too: a prior append may have
            # succeeded even if its shell cache refresh was interrupted.
            alias = publication['alias']
            try:
                local_text = Path(api['_current_local_rc']()).read_text()
            except FileNotFoundError:
                local_text = ''
            local_repos, _, _ = api['_local_entries'](local_text)
            registered = {**getattr(cfg, 'repos', {}), **local_repos}
            existing = registered.get(alias)
            if existing and os.path.realpath(existing) != os.path.realpath(destination):
                raise IdeaError('repository alias was taken before registration')
            if not existing:
                check = api['_new_alias_check'](alias, str(destination), local_repos, cfg.repos,
                                                api['_new_sh_whence'](alias))
                if check[0] == 'error':
                    raise IdeaError(check[1])
                api['_new_sh_register'](alias, str(destination), 't idea publish')
            api['_trust_auto']([str(destination)], say=None)
            publication['registered'] = True
            save(record)
            record['status'] = 'published'
            save(record)
            return publication


def show(record):
    publication = record.get('publication') or {}
    fields = [('ID', record['id']), ('Title', record['title']), ('Status', record['status']),
              ('Workspace', record['workspace']), ('Agent', record['agent']),
              ('Conversation', record.get('conversation_id') or 'Not yet recorded'),
              ('Files in publish/', str(len(eligible_files(record))))]
    if publication:
        fields += [('Repository', publication['slug']), ('Visibility', 'Private' if publication['private'] else 'Public'),
                   ('Selected files', str(len(publication['files']))), ('Commit', publication['commit']),
                   ('Local path', publication['path']), ('Alias', publication['alias'])]
        if publication.get('html_url'):
            fields.append(('URL', publication['html_url']))
        fields += [('Push', 'Complete' if publication.get('pushed') else 'Pending'),
                   ('Clone', 'Complete' if publication.get('cloned') else 'Pending'),
                   ('Registration', 'Complete' if publication.get('registered') else 'Pending')]
    return '\n'.join(f'{key}: {value}' for key, value in fields)


def _files_ui(ui, record, selected=None, focus=2, *, wrap):
    names = eligible_files(record)
    eligible = set(names)
    selected = set(selected or ()) & set(names)
    all_files, clear_files, continue_files, back = object(), object(), object(), object()
    rows = ([(all_files, 'Select all files'), (clear_files, 'Clear all files')] if names else [])
    rows += [(name, name) for name in names] + [(continue_files, 'Continue' if names else 'Continue with empty commit'), (back, 'Back')]
    focus = max(0, min(focus if names else 0, len(rows) - 1))
    while True:
        display = []
        if not names:
            display = [(line, None) for line in ui.row('No eligible files under publish/')]
        for index, (key, label) in enumerate(rows):
            for line in ui.row(label, selected=index == focus,
                               mark=('✓' if key in selected else '☐') if key in eligible else None):
                display.append((line, index))
        hints = ('↑↓/jk move · space mark · Ctrl-A all · v view · enter continue · q/esc back' if names else
                 '↑↓/jk move · enter continue · q/esc back')
        ui.frame('Files to publish', display, focus, hints)
        key = ui.read_key()
        focused = rows[focus][0]
        if key in ('up', 'k'):
            focus = (focus - 1) % len(rows)
        elif key in ('down', 'j'):
            focus = (focus + 1) % len(rows)
        elif key == '\x01' or key in ('space', 'enter') and focused is all_files:
            selected = set(names)
        elif key in ('space', 'enter') and focused is clear_files:
            selected.clear()
        elif key == 'space' and focused in eligible:
            selected.symmetric_difference_update({focused})
        elif key == 'enter':
            if focused is back:
                return None, selected, focus
            return snapshot(record, selected), selected, focus
        elif key == 'v' and focused in eligible:
            data, _ = _read_file(record, focused)
            text = data[:256 * 1024].decode('utf-8', errors='replace')
            text = ''.join(c if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) < 160 else '�' for c in text)
            body = [ui.rail + '  ' + part for line in text.splitlines()
                    for part in wrap(line.expandtabs(4), max(4, ui.cols() - 6))]
            if len(data) > 256 * 1024:
                body.append('  Content truncated at 256 KiB')
            ui.page(focused, body or ['  Empty file'], {'q', 'esc', 'enter'}, 'enter/q/esc back')
        elif key in ('q', 'esc'):
            return None, selected, focus
        elif key == 'abort':
            raise KeyboardInterrupt


def _review_ui(ui, cfg, record, api):
    publication = record.get('publication')
    if publication:
        body = [ui.rail + '  ' + line for line in show(record).splitlines()]
        body += [ui.rail + '  ' + entry['path'] for entry in publication['files']]
        chosen = ui.pick('Continue publication', [('retry', 'Retry publication'), ('back', 'Back')], context=[line.strip() for line in body])
        return {} if chosen == 'retry' else None
    selected, focus = set(), 2
    name = re.sub(r'[^a-z0-9._-]+', '-', record['title'].lower()).strip('-._')[:100] or 'my-idea'
    alias, owner, private = name, '', True
    while True:
        _discard_reviews(record)
        frozen, selected, focus = _files_ui(ui, record, selected, focus, wrap=api['_wrap_columns'])
        if frozen is None:
            return None
        previous_name = name
        name = ui.cooked_input('Repository name → ', name, prefill=name)
        if alias == previous_name:
            alias = name
        if not NAME.fullmatch(name) or name.endswith('.git') or name in ('.', '..'):
            ui.done('invalid', 'Use a repository name containing letters, digits, dots, dashes or underscores.')
            continue
        login = _gh('user', '--jq', '.login').stdout.strip()
        orgs = _gh('user/orgs', '--jq', '.[].login').stdout.split()
        owner_rows = api['_new_owner_rows'](login, orgs, owner or login)
        if not owner_rows:
            raise IdeaError('GitHub login is required only for publication; run gh auth login, then t idea publish ' + record['id'])
        owner = ui.pick('GitHub owner', owner_rows, default=next((i for i, r in enumerate(owner_rows) if r[0] == owner), 0))
        if owner is None:
            continue
        visibility = ui.pick('Visibility', [('private', 'Private'), ('public', 'Public')], default=0 if private else 1)
        if visibility is None:
            continue
        private = visibility == 'private'
        alias = ui.cooked_input('Alias → ', alias if alias != record['title'] else name, prefill=alias if alias != record['title'] else name)
        path = str(Path.home() / 'code' / name)
        review = dict(name=name, owner=owner, private=private, alias=alias, path=path, snapshot=frozen)
        try:
            _validate_review(review)
            if os.path.lexists(path):
                raise IdeaError('local destination already exists: ' + path)
            try:
                text = Path(api['_current_local_rc']()).read_text()
            except FileNotFoundError:
                text = ''
            repos, _, _ = api['_local_entries'](text)
            check = api['_new_alias_check'](alias, path, repos, cfg.repos, api['_new_sh_whence'](alias))
            if check[0] == 'error' or alias in cfg.repos or alias in repos:
                raise IdeaError(check[1] if check[0] == 'error' else 'repository alias already registered')
        except (OSError, ValueError) as error:
            ui.done('invalid', str(error))
            continue
        body = [f'  {owner}/{name}', '  Visibility: ' + ('Private' if private else 'Public'),
                '  Alias: ' + alias, '  Local path: ' + api['_tilde'](path), '  Branch: main',
                '  Files: ' + str(len(frozen['files']))]
        body += ['    ' + entry['path'] + ' · ' + str(entry['size']) + ' bytes' for entry in frozen['files']]
        if not frozen['files']:
            body.append('  Explicit empty initial commit · no workspace files included')
        body += ['  Private notes and native conversation history stay in the idea workspace.']
        key = ui.page('Create repository · review', body, {'y', 'n', 'q', 'esc'}, 'y create · n back · q cancel')
        if key == 'y':
            for entry in frozen['files']:
                data, mode = _read_file(record, entry['path'])
                if hashlib.sha256(data).hexdigest() != entry['sha256'] or mode != entry['mode']:
                    raise IdeaError('selected file changed since review; review files again: ' + entry['path'])
            collision = _gh('repos/' + owner + '/' + name, check=False)
            if collision.returncode == 0:
                ui.done('invalid', 'GitHub repository already exists; choose another name.')
                continue
            if '404' not in collision.stderr + collision.stdout:
                raise IdeaError('cannot verify GitHub name availability')
            return review
        if key in ('q', 'esc'):
            return None


def _publish_ui(ui, cfg, record, api):
    with idea_lock(record):
        fresh = load(record['id'])
        record.clear(); record.update(fresh)
        try:
            already_published = record['status'] == 'published'
            if already_published:
                result = record['publication']
            else:
                _assert_no_process(record)
                review = _review_ui(ui, cfg, record, api)
                if review is None:
                    return 130
                ui.restore()
                try:
                    result = publish(cfg, record, review, api, _already_locked=True)
                finally:
                    ui.raw()
            ui.done('Repository ready', result['html_url'])
            ui.done('Open', 't open ' + result['alias'])
            if not already_published and not os.environ.get('T_SETUP_SHIM'):
                ui.done('Shell', 'Open a new shell or run source ~/.zshrc to load the new alias.')
            return 0
        finally:
            _discard_reviews(record)


def run(cfg, args, api):
    action = getattr(args, 'idea_action', None)
    try:
        if action == 'list':
            records = list_ideas()
            if not records:
                print('No saved ideas. Start with t ideate or t idea start.')
            else:
                print('ID                                     AGENT   STATUS                TITLE')
                for record in records:
                    print(f'{record["id"]}  {record["agent"]:<6}  {record["status"]:<20}  {record["title"]}')
            return 0
        if action == 'show':
            print(show(load(args.identifier)))
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print('t idea: this action requires a terminal; use t idea list or t idea show ID to inspect saved ideas', file=sys.stderr)
            return 2
        ui = api['_RailUI']()
        ui._config_interrupts = True
        ui.intro('t ideate' if action is None else 't idea ' + action,
                 'Brainstorm privately with your native agent. Review selected files before creating a repository.')
        ui.raw()
        try:
            if action is None:
                records = [r for r in list_ideas() if r['status'] != 'published']
                rows = [('__new', 'New idea'), *[(r['id'], r['title']) for r in records], ('__back', 'Back')]
                chosen = ui.pick('Ideas', rows, values={r['id']: r['id'] + ' · ' + r['status'] for r in records})
                if chosen in (None, '__back'):
                    return 130
                if chosen == '__new':
                    action = 'start'
                else:
                    args.identifier = chosen
                    record = load(chosen)
                    choice = ui.pick(record['title'], [('resume', 'Continue brainstorming a saved idea'),
                        ('publish', 'Create repository' if not record.get('publication') else 'Continue publication'), ('back', 'Back')])
                    if choice in (None, 'back'):
                        return 130
                    action = choice
            if action == 'start':
                title = getattr(args, 'title', None) or ui.cooked_input('Idea title → ')
                if not title:
                    return 130
                record = create(title, cfg.agent_default)
                ui.done('Idea saved', record['id'])
                ui.done('Workspace', record['workspace'])
            else:
                record = load(args.identifier)
            if action in ('start', 'resume'):
                ui.restore()
                try:
                    result = launch(cfg, record, resume=action == 'resume')
                finally:
                    ui.raw()
                if result:
                    ui.done('Draft retained', record['id'] + ' · native exit ' + str(result))
                    return result
                choice = ui.pick('Next step', [('publish', 'Create repository'), ('save', 'Save for later'), ('back', 'Back')])
                if choice != 'publish':
                    ui.done('Idea saved', record['id'] + ' · Continue: t idea resume ' + record['id'])
                    return 0 if choice == 'save' else 130
            return _publish_ui(ui, cfg, record, api)
        finally:
            ui.restore()
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('t idea: ' + str(error), file=sys.stderr)
        return 1
