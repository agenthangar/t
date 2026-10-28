"""Bounded local Codex workers. No cloud credentials, shared checkout, or daemon."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

DEFAULT_MODEL = 'gpt-oss:20b'
MAX_OUTPUT = 8 * 1024 * 1024
INSTRUCTIONS = (
    'Delegate only bounded investigations or small patches with concrete acceptance checks. '
    'Use start_task, then task_status; use cancel_task when no longer needed. '
    'The local worker has a separate snapshot, not the lead checkout. Review its findings, '
    'patch and test evidence before applying anything. Failed or timed-out tasks return to '
    'the lead; never automatically retry indefinitely. One task runs locally at a time. '
    'Returned code and findings enter the lead conversation; local inference is not an '
    'end-to-end privacy boundary. Architecture and ambiguous changes stay with the lead.'
)


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 't/local-worker.json'


def state_root():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 't/local-worker'


def model_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', value):
        raise ValueError('use an Ollama model name, for example gpt-oss:20b')
    if 'cloud' in value.lower() or '://' in value or '..' in value:
        raise ValueError('local models only; cloud models and URLs are not supported')
    return value


def settings():
    try:
        data = json.loads(config_path().read_text())
    except FileNotFoundError:
        return {'enabled': False, 'model': DEFAULT_MODEL}
    if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
        raise ValueError('invalid local worker configuration')
    return {'enabled': data['enabled'], 'model': model_name(data.get('model'))}


def save_settings(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream)
        stream.write('\n')
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def executable(name):
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    native = Path.home() / '.local/bin' / name
    return str(native.resolve()) if native.is_file() and os.access(native, os.X_OK) else None


def environment(home):
    """Allowlist: never inherit auth, proxies, hooks, session identity, or COV_CORE_*."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if key in os.environ}
    env.update(HOME=str(home), CODEX_HOME=str(home / '.codex'),
               XDG_CONFIG_HOME=str(home / '.config'), XDG_STATE_HOME=str(home / '.local/state'),
               XDG_CACHE_HOME=str(home / '.cache'), GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0', NO_COLOR='1')
    Path(env['CODEX_HOME']).mkdir(exist_ok=True)
    return env


def stop(process):
    """Only signal process groups created by this adapter; reap before releasing ownership."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@contextmanager
def reservation():
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('a local worker or model setup is already running') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def request(base, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=body, headers={'Content-Type': 'application/json'})
    # Bypass proxy configuration even when the lead uses a corporate proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
        return json.load(response)


@contextmanager
def ollama_server():
    binary = executable('ollama')
    if not binary:
        raise ValueError('Ollama is missing; run t config local')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    root = state_root()
    env = environment(root / 'ollama-home')
    env.update(OLLAMA_HOST=f'127.0.0.1:{port}', OLLAMA_NO_CLOUD='1',
               OLLAMA_MODELS=str(Path.home() / '.ollama/models'),
               OLLAMA_CONTEXT_LENGTH='32768', OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1')
    with (root / 'ollama.log').open('w') as log:
        proc = subprocess.Popen([binary, 'serve'], env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while True:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise ValueError('Ollama did not start; inspect ' + str(root / 'ollama.log'))
                try:
                    request(base, '/api/tags')
                    break
                except (OSError, ValueError):
                    time.sleep(.1)
            yield base, env
        finally:
            stop(proc)


def check_model(base, model):
    info = request(base, '/api/show', {'model': model})
    if info.get('remote_host') or info.get('remote_model'):
        raise ValueError('the selected model is cloud-backed; choose a downloaded local model')
    if 'tools' not in info.get('capabilities', []):
        raise ValueError('the selected local model must support tool calling')
    return info


def git(directory, *args, data=None):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                             '-C', str(directory), *args], input=data, capture_output=True,
                            env={**{k: os.environ[k] for k in ('PATH', 'LANG', 'HOME') if k in os.environ},
                                 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
                                 'GIT_TERMINAL_PROMPT': '0'},
                            timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[-4000:] or 'git operation failed')
    return result.stdout


def snapshot(source, destination):
    """Copy the current tracked tree and nonignored files into a separate Git database."""
    source = Path(source).resolve(strict=True)
    top = Path(git(source, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    if source != top:
        raise ValueError('workspace must be the repository root')
    head = git(source, 'rev-parse', 'HEAD').decode().strip()
    entries = git(source, 'ls-files', '--stage', '-z').split(b'\0')
    if any(entry.startswith(b'160000 ') for entry in entries):
        raise ValueError('local worker snapshots do not yet support submodules')
    names = set(git(source, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''}
    destination.mkdir()
    total = 0
    for raw in sorted(names):
        relative = Path(os.fsdecode(raw))
        src, dst = source / relative, destination / relative
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe repository path')
        if not src.exists() and not src.is_symlink():
            continue  # deleted tracked file
        # Reject symlinks, including parent components, rather than copying outside the repo.
        if src.is_symlink() or not src.resolve().is_relative_to(source) or not src.is_file():
            raise ValueError('snapshot requires regular files: ' + str(relative))
        total += src.stat().st_size
        if total > 256 * 1024 * 1024:
            raise ValueError('snapshot exceeds 256 MiB; narrow the repository before delegation')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    git(destination, 'init', '-q')
    git(destination, 'add', '--all', '--force')
    git(destination, '-c', 'user.name=t local worker', '-c', 'user.email=local@localhost',
        'commit', '-qm', 'Local task baseline', '--allow-empty', '--no-verify')
    return head


def codex_command(workspace, output, model):
    binary = executable('codex')
    if not binary:
        raise ValueError('Codex CLI is missing; run t config local')
    return [binary, 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
            '--oss', '--local-provider', 'ollama', '--model', model,
            '--sandbox', 'workspace-write', '--json', '--color', 'never',
            '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
            '-c', 'model_context_window=32768', '-c', 'model_auto_compact_token_limit=24576',
            '-c', 'mcp_servers={}', '-c', 'features.multi_agent=false',
            '-c', 'sandbox_workspace_write.network_access=false',
            '-c', 'shell_environment_policy.inherit="all"',
            '-C', str(workspace), '--output-last-message', str(output), '-']


def run_worker(job, base, model, timeout, cancel):
    workspace = job / 'workspace'
    env = environment(job / 'home')
    env.update(OLLAMA_HOST=base, CODEX_OSS_BASE_URL=base + '/v1')
    assignment = json.loads((job / 'assignment.json').read_text())
    prompt = (
        'You are a local worker in an isolated repository snapshot. Complete only the assignment below. '
        f'Your working directory is {workspace}. Use this directory for every command. '
        'Do not delegate, access the network, install dependencies, commit, push, or publish. '
        'Repository release/PR instructions are the lead agent responsibility. '
        'For investigate mode do not edit project files. For patch mode make the smallest change. '
        'Use the exec_command shell tool to read files, edit with Python or shell, and run checks. '
        'Do not call apply_patch: this local provider does not expose that tool. '
        'Run available targeted checks. Report findings, changes, commands and exit outcomes, '
        'and remaining uncertainties honestly. Do not claim checks you did not run.\n\n'
        + json.dumps({'mode': assignment['mode'], 'assignment': assignment['assignment']})
    )
    # Isolated trust config: project-local configuration stays untrusted.
    (job / 'home/.codex/config.toml').write_text('')
    output = job / 'answer.txt'
    (job / 'prompt.txt').write_text(prompt)
    command = codex_command(workspace, output, model)
    if assignment['mode'] == 'investigate':
        command[command.index('workspace-write')] = 'read-only'
    started = time.monotonic()
    with (job / 'prompt.txt').open() as stdin, (job / 'events.jsonl').open('w') as log:
        proc = subprocess.Popen(command, cwd=workspace, env=env, stdin=stdin, stdout=log,
                                stderr=log, start_new_session=True)
        try:
            while proc.poll() is None:
                if cancel.is_set():
                    return 'cancelled'
                if time.monotonic() - started > timeout:
                    return 'timed_out'
                if log.tell() > MAX_OUTPUT or (job / 'events.jsonl').stat().st_size > MAX_OUTPUT:
                    return 'output_limit'
                cancel.wait(.1)
            return 'completed' if proc.returncode == 0 else 'failed'
        finally:
            stop(proc)


def collect(job):
    """Evidence is observed command events; assistant findings remain explicitly unverified."""
    evidence = []
    events = job / 'events.jsonl'
    if events.exists():
        for line in events.read_bytes()[:MAX_OUTPUT].decode(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item', {}) if isinstance(event, dict) else {}
            if isinstance(item, dict) and item.get('type') == 'command_execution':
                evidence.append({key: str(item[key])[:2000] if key in ('command', 'aggregated_output') else item[key]
                                 for key in ('command', 'exit_code', 'status', 'aggregated_output') if key in item})
    workspace = job / 'workspace'
    git(workspace, 'add', '--all')
    patch = git(workspace, 'diff', '--cached', '--binary', 'HEAD')
    (job / 'changes.patch').write_bytes(patch)
    answer = job / 'answer.txt'
    return {'findings': answer.read_bytes()[:32000].decode(errors='replace') if answer.exists() else '',
            'evidence': evidence[-30:], 'patch': patch[:32000].decode(errors='replace'),
            'patch_truncated': len(patch) > 32000, 'patch_path': str(job / 'changes.patch'),
            'events_path': str(events), 'validated_by_lead': False}


class Worker:
    def __init__(self):
        self.tasks = {}
        self.threads = []

    def start(self, args):
        if not isinstance(args, dict) or set(args) - {'workspace', 'assignment', 'mode', 'timeout_seconds'}:
            raise ValueError('unexpected task arguments')
        text = args.get('assignment')
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError('assignment must contain 1–16000 characters')
        mode = args.get('mode', 'investigate')
        timeout = args.get('timeout_seconds', 300)
        if mode not in ('investigate', 'patch') or type(timeout) is not int or not 10 <= timeout <= 1800:
            raise ValueError('choose investigate/patch and a timeout from 10 to 1800 seconds')
        workspace = args.get('workspace')
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise ValueError('workspace must be an absolute repository root')
        config = settings()
        if not config['enabled']:
            raise ValueError('local worker is disabled; run t config local')
        lease = reservation()
        lease.__enter__()
        try:
            task_id = uuid.uuid4().hex
            job = state_root() / task_id
            job.mkdir(mode=0o700)
            (job / 'assignment.json').write_text(json.dumps(dict(args, mode=mode)))
            cancel = threading.Event()
            task = {'task_id': task_id, 'status': 'running', 'model': config['model'],
                    'workspace': str(job / 'workspace'), 'started_at': time.time()}
            self.tasks[task_id] = (task, cancel)
            thread = threading.Thread(target=self.execute,
                                      args=(task, cancel, lease, job, workspace, config['model'], timeout))
            thread.start()
            self.threads.append(thread)
            return dict(task)
        except BaseException:
            lease.__exit__(None, None, None)
            raise

    def execute(self, task, cancel, lease, job, source, model, timeout):
        status = 'failed'
        try:
            task['source_head'] = snapshot(source, job / 'workspace')
            if cancel.is_set():
                status = 'cancelled'
            else:
                with ollama_server() as (base, _):
                    check_model(base, model)
                    status = run_worker(job, base, model, timeout, cancel)
                task.update(collect(job))
                if status != 'completed':
                    events = job / 'events.jsonl'
                    task['diagnostic'] = events.read_bytes()[-4000:].decode(errors='replace') if events.exists() else ''
        except Exception as exc:
            task['error'] = str(exc)
        finally:
            task['elapsed_seconds'] = round(time.time() - task['started_at'], 2)
            task['status'] = status
            try:
                (job / 'result.json').write_text(json.dumps(task))
            finally:
                lease.__exit__(None, None, None)

    def call(self, name, args):
        if name == 'start_task':
            return self.start(args)
        if name not in ('task_status', 'cancel_task'):
            raise ValueError('unknown local worker tool')
        if not isinstance(args, dict) or set(args) != {'task_id'} or not isinstance(args['task_id'], str) or args['task_id'] not in self.tasks:
            raise ValueError('unknown task_id in this MCP connection')
        task, cancel = self.tasks[args['task_id']]
        if name == 'cancel_task' and task['status'] == 'running':
            cancel.set()
            return {'task_id': task['task_id'], 'status': 'cancelling'}
        return dict(task)

    def close(self):
        for _, cancel in self.tasks.values():
            cancel.set()
        for thread in self.threads:
            thread.join()


def tool_specs():
    return [
        {'name': 'start_task', 'description': INSTRUCTIONS,
         'inputSchema': {'type': 'object', 'additionalProperties': False,
                         'required': ['workspace', 'assignment'], 'properties': {
                             'workspace': {'type': 'string', 'description': 'Absolute repository root; current tracked and nonignored files are snapshotted.'},
                             'assignment': {'type': 'string', 'minLength': 1, 'maxLength': 16000},
                             'mode': {'type': 'string', 'enum': ['investigate', 'patch'], 'default': 'investigate'},
                             'timeout_seconds': {'type': 'integer', 'minimum': 10, 'maximum': 1800, 'default': 300}}}},
        *[{'name': name, 'description': description,
           'annotations': {'readOnlyHint': name == 'task_status'},
           'inputSchema': {'type': 'object', 'additionalProperties': False,
                           'required': ['task_id'], 'properties': {'task_id': {'type': 'string'}}}}
          for name, description in [('task_status', 'Read status, findings, patch and observed command evidence.'),
                                    ('cancel_task', 'Cancel a task owned by this MCP connection. Poll until terminal.')]]
    ]


def serve(api):
    worker = Worker()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        for line in api['sys'].stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                response = api['_mcp_error'](None, -32700, 'parse error')
            else:
                response = api['_mcp_handle'](msg, tool_specs(), worker.call,
                                             {'name': 't-local-worker', 'version': '1'}, INSTRUCTIONS)
            if response is not None:
                print(json.dumps(response), flush=True)
    finally:
        worker.close()
        signal.signal(signal.SIGTERM, previous)
    return 0
