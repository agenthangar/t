"""Best-effort, account-local reconciliation; never synthesize conversation turns.

Native files are authoritative. The control socket can acknowledge main-thread
model/effort changes for subsequent turns, but cannot verify instruction reloads.
"""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
import subprocess
import time
import uuid
from contextlib import contextmanager, closing
from types import SimpleNamespace
import sqlite3

TIMEOUT = 3
MAX_THREADS = 200
MAX_HOSTS = 8
MAX_SCOPES = 25
_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]


def workflow():
    return runpy.run_path(str(Path(__file__).with_name('t_workflow.py')))


def state_path():
    return workflow()['config_path']().with_name('workflow-propagation.json')


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def status():
    """Read only: no locks, connections, native-file writes, or retries."""
    w = workflow()
    before = w['snapshot'](state_path())
    return json.loads(before[0]) if before else {'revision': 0, 'native': 'not recorded', 'runtime': 'not attempted'}


@contextmanager
def locked(blocking=True):
    path = state_path().with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def write(state):
    w = workflow()
    path = state_path()
    before = w['snapshot'](path)
    w['publish']([(path, before, w['encode'](state))])
    path.chmod(0o600)


def changed_scopes(previous, document):
    w = workflow()
    scopes = {None, *previous['repositories'], *document['repositories']}
    return [scope for scope in sorted(scopes, key=lambda item: item or '')
            if w['effective'](previous, scope) != w['effective'](document, scope)]


@_spinner("Resolving workflow repository scopes")
def git(cwd, *args):
    result = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=0.1)
    return result.stdout.strip() if result.returncode == 0 else ''


def identity(cwd):
    remote = git(cwd, 'config', '--get', 'remote.origin.url')
    match = re.fullmatch(r'(?:https://github.com/|git@github.com:|ssh://git@github.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote)
    return match[1].lower() if match else None


def portable(document, scopes, identity_cache=None):
    """Only verified host-independent identities cross SSH, never local paths."""
    entries, missing = {}, 0
    identity_cache = {} if identity_cache is None else identity_cache
    for index, scope in enumerate(scopes):
        if scope is None:
            continue
        if index >= MAX_SCOPES:
            missing += 1
            continue
        if scope not in identity_cache and len(identity_cache) < MAX_SCOPES:
            try:
                identity_cache[scope] = identity(scope)
            except (OSError, subprocess.TimeoutExpired):
                identity_cache[scope] = None
        key = identity_cache.get(scope)
        if key:
            entries[key] = document['repositories'].get(scope)
        else:
            missing += 1
    return {'version': 1, 'global': None in scopes, 'default': document['default'],
            'workflows': {name: document['workflows'][name] for name in
                          {document['default'], *(entry['workflow'] for entry in entries.values()
                                                  if entry and 'workflow' in entry)}},
            'repositories': entries}, missing


def settings_delta(before, after):
    return {target: after.get(key) or None for key, target in
            [('DEV_MODEL[codex]', 'model'), ('DEV_EFFORT[codex]', 'effort')]
            if before.get(key) != after.get(key)}


def rebind_source(path, before, after, effective_before, effective_after):
    """Advance an existing guard only for a verified unchanged-model transaction."""
    state = status()
    guard = state.get('context', {}).get('settings_source', {})
    values = state.get('main_settings', state.get('settings', {}))
    if (not values or not guard or guard.get('path') != os.path.realpath(path) or
            guard.get('hash') != digest(before) or settings_delta(effective_before, effective_after)):
        return None
    for field, key in (('model', 'DEV_MODEL[codex]'), ('effort', 'DEV_EFFORT[codex]')):
        if field in values and (effective_before.get(key) or None) != values[field]:
            return None
    return {'path': os.path.realpath(path), 'hash': digest(after)}


def verified_settings(text, cached):
    """Bootstrap only literal source assignments agreeing with the derived cache."""
    literal, unsafe = {}, set()
    control = False
    for line in text.splitlines():
        try:
            tokens = list(shlex.shlex(line, posix=True, punctuation_chars=True))
        except ValueError:
            tokens = []
            control = True
        if not tokens:
            continue
        if (tokens[0] in ('if', 'elif', 'else', 'then', 'fi', 'for', 'while', 'until',
                          'case', 'esac', 'function', 'repeat', 'select', 'do', 'done',
                          'source', '.', 'eval') or any(token in (';', '&&', '||', '|', '&', '(', ')', '{', '}') for token in tokens)):
            control = True
        match = re.fullmatch(r'DEV_(MODEL|EFFORT)\[codex\]=(.*)', line)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]+\])?=.*', line):
            control = True
        for name, key in (('MODEL', 'model'), ('EFFORT', 'effort')):
            if any('DEV_' + name in token for token in tokens) and (not match or match[1] != name):
                unsafe.add(key)
        if not match:
            continue
        key = 'model' if match[1] == 'MODEL' else 'effort'
        try:
            words = shlex.split(match[2], comments=True)
        except ValueError:
            words = []
        value = words[0] if len(words) == 1 else ''
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*', value):
            literal[key] = value
        else:
            unsafe.add(key)
    return {key: value for key, value in cached.items()
            if not control and key not in unsafe and literal.get(key) == value}


def desired(previous, document, *, settings=None, context=None):
    """Build a durable intent under the caller's propagation lock, before publication."""
    context = context or {}
    scopes = changed_scopes(previous, document)
    old = status()
    inherited_context = old.get('context', {})
    if not settings and 'settings_source' not in context and (old.get('main_settings') or old.get('settings')) and inherited_context.get('settings_source'):
        context = {**context, 'settings_source': inherited_context['settings_source']}
    remote_settings = copy.deepcopy(old.get('remote_settings', {}))
    remote_scopes = copy.deepcopy(old.get('remote_scopes', {}))
    for target in set((context or inherited_context).get('hosts', {}).values()):
        if scopes:
            remote_scopes[target] = list(dict.fromkeys([*remote_scopes.get(target, []), *scopes]))
        if settings:
            remote_settings[target] = {**remote_settings.get(target, {}), **settings}
    state = {'revision': old['revision'] + 1, 'origin': old.get('origin', uuid.uuid4().hex),
             'received': old.get('received', {}), 'identity_scan': old.get('identity_scan', {}), 'workflow_hash': digest(document),
             'native': 'published',
             'coverage': 'only loaded root threads in the connected owning runtime are supported; other sessions and runtimes remain unverified',
             'instructions': 'runtime refresh unverified; native files apply when the agent reloads them',
             'scopes': scopes, 'settings': {**old.get('settings', {}), **(settings or {})},
             'main_settings': {**old.get('main_settings', old.get('settings', {})), **(settings or {})},
             'context': context or inherited_context,
             'threads': {} if settings else old.get('threads', {}),
             'hosts': old.get('hosts', {}),
             'remote_settings': remote_settings, 'remote_scopes': remote_scopes, 'host_cursor': old.get('host_cursor', 0),
             'attempts': 0, 'runtime': 'pending'}
    state['unmapped_scopes'] = 0
    return state


def changed(previous, document, *, settings=None, context=None):
    """Queue explicit main-setting changes, or receive a host's scoped revision."""
    with locked():
        if workflow()['load']()[0] != document:
            return
        write(desired(previous, document, settings=settings, context=context))
    return retry()


class Proxy:
    """Connect only to an owning control socket, never start a new backend."""
    @_spinner("Connecting to the conversation service")
    def __enter__(self):
        self.process = subprocess.Popen(['codex', 'app-server', 'proxy'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env={k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')},
                                        start_new_session=True)
        try:
            self.deadline, self.counter = time.monotonic() + TIMEOUT, 0
            transport = runpy.run_path(str(Path(__file__).with_name("t_codex_socket.py")))["WebSocket"]
            self.socket = transport(self.process.stdout, self.process.stdin, self.deadline)
            self.socket.handshake()
            self.call('initialize', {'clientInfo': {'name': 't-workflow-sync', 'version': '1'}})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def send(self, message):
        self.socket.send(json.dumps(message).encode('utf-8'))

    @_spinner("Updating conversation settings")
    def call(self, method, params):
        self.counter += 1
        self.send({'id': self.counter, 'method': method, 'params': params})
        while True:
            reply = json.loads(self.socket.receive())
            if not isinstance(reply, dict):
                raise ValueError('invalid runtime response')
            if reply.get('id') == self.counter:
                if 'error' in reply or not isinstance(reply.get('result'), dict):
                    raise ValueError('runtime refused request')
                return reply['result']

    def __exit__(self, *_):
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def current(state):
    if digest(workflow()['load']()[0]) != state['workflow_hash']:
        return False
    source = state['context'].get('settings_source')
    return not source or digest(Path(source['path']).read_text()) == source['hash']


def launch_policy(w, document, cwd):
    """Resolve only the actual destination's canonical repository scope."""
    repository = None
    if document['repositories']:
        try:
            raw_common = git(cwd or os.getcwd(), 'rev-parse', '--path-format=absolute', '--git-common-dir')
            if not raw_common or not os.path.isabs(raw_common):
                return None
            common = os.path.realpath(raw_common)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not os.path.isabs(common) or not os.path.isdir(common):
            return None
        repository = next((scope for scope in document['repositories']
                           if os.path.realpath(os.path.join(scope, '.git')) == common), None)
    return w['effective'](document, repository)


def session_defaults(policy, agent):
    if not policy or not policy['enabled'] or policy.get('stages_enabled', True):
        return {}
    return {key: value for key, value in policy.get('session_defaults', {}).get(agent, {}).items() if value}


def setting_args(values, agent):
    result = []
    model, effort = values.get('model'), values.get('effort')
    if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*', model):
        result.extend(['--model', model])
    if isinstance(effort, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', effort):
        result.extend(['-c', 'model_reasoning_effort=' + json.dumps(effort)] if agent == 'codex' else ['--effort', effort])
    return result


def launch_settings(agent, cwd=None):
    """Read-only scoped defaults for a fresh session; never write global defaults."""
    if agent not in ('codex', 'claude'):
        return []
    w = workflow()
    document, _ = w['load']()
    return setting_args(session_defaults(launch_policy(w, document, cwd), agent), agent)


def resume_settings(sid, cwd=None, agent='codex'):
    """Read-only launch overrides for a guarded selected dormant root conversation."""
    if agent not in ('codex', 'claude') or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sid):
        return []
    w = workflow()
    document, _ = w['load']()
    policy = launch_policy(w, document, cwd)
    if not policy or not policy['enabled']:
        return []
    # Existing pickers/launchers retain their one-owner checks. An indexed child
    # must not inherit root-workflow overrides even if explicitly selected.
    if agent == 'codex':
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        for database in sorted(home.glob('state_*.sqlite'), reverse=True)[:4]:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as connection:
                row = connection.execute('SELECT source FROM threads WHERE id=?', (sid,)).fetchone()
            if row and 'subagent' in str(row[0]).lower():
                return []
            if row:
                break
    scoped = session_defaults(policy, agent)
    values = {}
    path = state_path()
    if agent == 'codex' and path.exists() and path.stat().st_size <= 1024 * 1024:
        try:
            state = status()
            if isinstance(state, dict) and state.get('revision') and current(state):
                existing = state.get('main_settings', state.get('settings', {}))
                if isinstance(existing, dict):
                    values.update(existing)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # The journal cannot invalidate independent scoped preferences.
    if scoped.get('model'):
        values.pop('effort', None)  # A global effort may not suit the selected model.
    values.update(scoped)
    return setting_args(values, agent)


def local(state):
    if not state['settings']:
        state['runtime'] = 'instructions refresh unverified; no main model/effort changes requested'
        return
    reset = any(value is None for value in state['settings'].values())
    with Proxy() as proxy:
        cursor, seen, count = None, set(), 0
        while True:
            page = proxy.call('thread/loaded/list', {'cursor': cursor, 'limit': 50})
            ids = page.get('data')
            if not isinstance(ids, list) or any(not isinstance(sid, str) for sid in ids):
                raise ValueError('invalid loaded-thread response')
            for sid in ids:
                count += 1
                if count > MAX_THREADS:
                    raise ValueError('thread limit reached; retry pending')
                if sid in state['threads']:
                    continue
                thread = proxy.call('thread/read', {'threadId': sid, 'includeTurns': False}).get('thread', {})
                # Fail closed for unknown sources and stage agents.
                if thread.get('parentThreadId') or thread.get('source') not in ('cli', 'vscode', 'exec', 'appServer'):
                    continue
                params = {k: v for k, v in state['settings'].items()
                          if v is not None and thread.get('model' if k == 'model' else 'reasoningEffort') != v}
                if params:
                    if not current(state):
                        raise ValueError('newer workflow saved; superseded attempt stopped')
                    proxy.call('thread/settings/update', {'threadId': sid, **params})
                state['threads'][sid] = 'accepted for subsequent turns' if params else 'already current'
            cursor = page.get('nextCursor')
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError('invalid pagination cursor')
            seen.add(cursor)
        state['runtime'] = ('pending: clearing launch defaults requires agent reload' if reset else
                            'main settings accepted for subsequent turns in loaded root threads; current turns and instruction reload unverified'
                            if state['threads'] else 'pending: no supported loaded root threads')
        if state['threads'] and not reset:
            state['settings'] = {}


@_spinner("Synchronizing workflows across hosts")
def remote(state):
    hosts = sorted(target for target in set(state['context'].get('hosts', {}).values())
                   if state.get('remote_scopes', {}).get(target) or state.get('remote_settings', {}).get(target))
    offset = state.get('host_cursor', 0) % len(hosts) if hosts else 0
    hosts = hosts[offset:] + hosts[:offset]
    state['host_cursor'] = offset + 1
    identity_cache = {}
    for target in hosts[:MAX_HOSTS]:
        if not current(state):
            return
        scopes = state.get('remote_scopes', {}).get(target, [])
        settings = state.get('remote_settings', {}).get(target, {})
        if not scopes and not settings:
            continue
        portable_doc, missing = portable(workflow()['load']()[0], scopes, identity_cache)
        payload = {**portable_doc, 'settings': settings}
        state['unmapped_scopes'] = missing
        if not portable_doc:
            state['hosts'][target] = 'pending: no portable workflow payload'
            continue
        try:
            inner = 't config workflow sync --receive'
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2', '--', target,
                                     "zsh -lic '" + inner + "'"], input=json.dumps({**payload, 'request_id': state.get('origin', '0' * 32) + ':' + str(state.get('revision', 1))}),
                                    capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise ValueError('host unreachable or sync unsupported')
            reply = json.loads(result.stdout)
            state['hosts'][target] = reply['summary']
            if not reply.get('defaults_pending'):
                state.setdefault('remote_settings', {}).pop(target, None)
            if not reply.get('pending_scopes'):
                sent = {scope for scope in scopes[:MAX_SCOPES]
                        if scope is None or identity_cache.get(scope)}
                remaining = [scope for scope in scopes if scope not in sent]
                if remaining:
                    state.setdefault('remote_scopes', {})[target] = remaining
                else:
                    state.setdefault('remote_scopes', {}).pop(target, None)
            if state['unmapped_scopes']:
                state['hosts'][target] += '; pending: additional scopes or local identities unverified'
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            state['hosts'][target] = 'pending: host unreachable or sync unsupported'
    for target in hosts[MAX_HOSTS:]:
        state['hosts'][target] = 'pending: host limit reached'


def retry(*, boundary=False, remote_hosts=True):
    with locked(blocking=not boundary) as acquired:
        if not acquired:
            return status()
        state = status()
        if not state['revision'] or not current(state):
            return state
        state['attempts'] += 1
        try:
            local(state)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            state['runtime'] = 'pending: owning runtime unavailable, unsupported, or attempt incomplete'
        if state['context'].get('unverified_main_defaults'):
            state['runtime'] += '; pending: bootstrap main defaults source/cache could not be verified'
        if remote_hosts:
            remote(state)
        if current(state):
            write(state)
        return state


def native_defaults(settings):
    """Stage only account-owned root Codex defaults; prove unrelated TOML unchanged."""
    if not settings:
        return [], None
    try:
        tomllib = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("t_toml.py"))))
        w = workflow()
        path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
        before = w['snapshot'](path)
        if before and path.stat().st_uid != os.getuid():
            raise ValueError('native config is not account-owned')
        text = before[0] if before else ''
        parsed = tomllib.loads(text)
        expected = copy.deepcopy(parsed)
        lines = text.splitlines(keepends=True)
        boundary = next((i for i, line in enumerate(lines) if re.match(r'\s*\[', line)), len(lines))
        root, tail = lines[:boundary], lines[boundary:]
        for field, key in (('model', 'model'), ('effort', 'model_reasoning_effort')):
            if field not in settings:
                continue
            value = settings[field]
            matches = [i for i, line in enumerate(root) if re.match(r'\s*' + key + r'\s*=', line)]
            if len(matches) > 1 or (key in parsed and not matches):
                raise ValueError('native root key requires unsupported syntax')
            if value is None:
                expected.pop(key, None)
                if matches:
                    root[matches[0]] = ''
            else:
                expected[key] = value
                line = key + ' = ' + json.dumps(value) + '\n'
                if matches:
                    prior = root[matches[0]]
                    if '#' in prior:
                        line = line.rstrip('\n') + '  #' + prior.split('#', 1)[1].rstrip('\n') + '\n'
                    root[matches[0]] = line
                else:
                    if root and not root[-1].endswith('\n'):
                        root[-1] += '\n'
                    root.append(line)
        after = ''.join([*root, *tail])
        if tomllib.loads(after) != expected:
            raise ValueError('native defaults cannot be edited without unrelated changes')
        return [(path, before, after)], None
    except (ImportError, OSError, ValueError):
        return [], 'native Codex defaults require supported TOML and account-owned regular config'


def receive(payload, repos, settings_writer=None):
    """One host only; resolve scope identities locally, never fan out."""
    if not isinstance(payload, dict) or payload.get('version') != 1:
        raise ValueError('unsupported workflow propagation payload')
    request_id = payload.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}:[1-9][0-9]*', request_id)):
        raise ValueError('invalid propagation request identity')
    previous_receipts = status().get('received', {})
    batch = digest({key: value for key, value in payload.items() if key != 'request_id'})
    receipt = previous_receipts.get(request_id.split(':')[0], {}) if request_id else {}
    if request_id and (receipt.get('revision', 0) > int(request_id.split(':')[1]) or
                       (receipt.get('revision') == int(request_id.split(':')[1]) and batch in receipt.get('batches', []))):
        return {'pending_scopes': 0, 'summary': 'already received or superseded; ' + status()['runtime']}
    w = workflow()
    document, before = w['load']()
    after = copy.deepcopy(document)
    incoming = w['validate']({'version': 3, 'default': payload['default'],
                             'workflows': payload['workflows'], 'repositories': {}})
    if payload.get('global'):
        selected = incoming['workflows'][incoming['default']]
        name = 'synced-' + digest(selected)[:16]
        after['default'] = name
        after['workflows'][name] = selected
    paths = sorted({os.path.realpath(path) for path in repos.values()}) if payload['repositories'] else []
    scan_key = digest([request_id, paths])
    with locked():
        scan_state = status()
        scan = scan_state.get('identity_scan', {})
        if scan.get('key') != scan_key or scan.get('cursor', 0) >= len(paths):
            scan = {'key': scan_key, 'cursor': 0, 'identities': {}}
        deadline = time.monotonic() + 0.75
        attempts = 0
        while scan['cursor'] < len(paths) and attempts < 8 and time.monotonic() < deadline:
            path = paths[scan['cursor']]
            attempts += 1
            try:
                key = identity(path)
            except (OSError, subprocess.TimeoutExpired):
                break  # Retry this registration on the next bounded attempt.
            if key:
                scan['identities'].setdefault(key, []).append(path)
            scan['cursor'] += 1
        scan_state['identity_scan'] = scan
        write(scan_state)
    identities = scan['identities']
    incomplete = scan['cursor'] < len(paths)
    pending = 0
    for key, entry in payload['repositories'].items():
        paths = identities.get(key, [])
        if incomplete or len(paths) != 1:
            pending += 1
            continue
        if entry is None:
            after['repositories'].pop(paths[0], None)
        else:
            if 'workflow' in entry:
                entry = {'inline': copy.deepcopy(incoming['workflows'][entry['workflow']]),
                         **({'excluded': True} if entry.get('excluded') else {})}
            after['repositories'][paths[0]] = entry
    settings = payload.get('settings', {})
    if set(settings) - {'model', 'effort'} or any(v is not None and (not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*', v)) for v in settings.values()):
        raise ValueError('invalid main settings')
    w['validate'](after)
    defaults_pending, extra = None, []
    context = {'hosts': {}, 'repos': {}}
    persist_settings = bool(settings) and batch not in receipt.get('settings_batches', [])
    if persist_settings:
        extra, defaults_pending = native_defaults(settings)
        if settings_writer is None:
            defaults_pending = 'authoritative launch config writer unavailable'
        if not defaults_pending:
            launch_extra = settings_writer(settings)
            extra.extend(launch_extra)
            if launch_extra:
                source, _, text = launch_extra[-1]
                context['settings_source'] = {'path': str(source), 'hash': digest(text)}
        else:
            extra = []
    runtime_settings = settings if persist_settings and not defaults_pending else {}
    if after != document or runtime_settings or extra:
        w['save'](after, before, extra=extra, propagation_context=context, main_settings=runtime_settings)
    if request_id:
        with locked():
            state = status()
            origin, revision = request_id.split(':')
            revision = int(revision)
            receipt = state.setdefault('received', {}).get(origin, {})
            if receipt.get('revision', 0) <= revision:
                batches = receipt.get('batches', []) if receipt.get('revision') == revision else []
                settings_batches = receipt.get('settings_batches', []) if receipt.get('revision') == revision else []
                state['received'][origin] = {'revision': revision,
                    'settings_batches': list(dict.fromkeys([*settings_batches, *([batch] if settings and not defaults_pending else [])])),
                    'batches': list(dict.fromkeys([*batches, *([batch] if not pending and not defaults_pending else [])]))}
            write(state)
    return {'pending_scopes': pending, 'defaults_pending': bool(defaults_pending),
            'summary': ('pending: ' + defaults_pending if defaults_pending else
                        'pending: unmapped repository identities' if pending else
                        'native instructions published; launch defaults ' + ('published; ' if settings else 'unchanged; ') + status()['runtime'])}


if __name__ == '__main__':
    try:
        if len(sys.argv) == 4 and sys.argv[1] == '--launch-settings':
            print('\n'.join(launch_settings(sys.argv[2], sys.argv[3])), end='')
        elif len(sys.argv) in (3, 4, 5) and sys.argv[1] == '--resume-settings':
            print('\n'.join(resume_settings(sys.argv[2], sys.argv[3] if len(sys.argv) >= 4 else None,
                                           sys.argv[4] if len(sys.argv) == 5 else 'codex')), end='')
        else:
            state = status()
            if '--attach-status' in sys.argv and state.get('main_settings', state.get('settings')):
                print('Workflow defaults: live attach keeps this owner; guarded resume/restart applies applicable model/effort defaults. Live refresh remains unverified.')
            if '--background' in sys.argv:
                retry(boundary=True)
            elif state['revision']:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--background'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True,
                                 env={k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')})
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        pass  # SessionStart retries must never prevent a session from opening.
