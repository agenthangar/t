"""Bounded local Codex workers. No cloud credentials, shared checkout, or daemon."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
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
ADAPTER_VERSION = '2'
TERMINAL = {'completed', 'cancelled', 'failed', 'timed_out', 'inactivity_timed_out', 'output_limit', 'busy'}
MAX_OUTPUT = 8 * 1024 * 1024
INSTRUCTIONS = (
    'Delegate only bounded investigations or small patches with concrete acceptance checks. '
    'Use check_availability before start_task, then wait_task for bounded waits; use cancel_task when no longer needed. '
    'Automatic workers skip busy or unknown runtimes; never interrupt another session. '
    'Only accepted: true means the worker accepted the task; accepted: false means continue in the lead. '
    'Omit timeout_seconds to inherit the workflow budget. Supply narrow paths and enough implementation context '
    'to avoid broad repository exploration; return partial findings when the assignment cannot be completed. '
    'The local worker has a separate snapshot, not the lead checkout. Review its findings, '
    'patch and test evidence before applying anything. Failed or timed-out tasks return to '
    'the lead; never automatically retry indefinitely. Local requests take fair turns through the shared machine queue. '
    'Returned code and findings enter the lead conversation; local inference is not an '
    'end-to-end privacy boundary. Architecture and ambiguous changes stay with the lead.'
)


def inference():
    return runpy.run_path(str(Path(__file__).with_name('t_local_inference.py')))


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
    result = {'enabled': data['enabled'], 'model': model_name(data.get('model'))}
    if 'provider' in data:
        if data['provider'] not in ('ollama', 'lmstudio'):
            raise ValueError('invalid local worker provider')
        result['provider'] = data['provider']
    return result


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
def ollama_server(context=32768):
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
               OLLAMA_CONTEXT_LENGTH=str(context), OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1')
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


def run_worker(job, base, model, timeout, cancel, provider='ollama', context=32768, progress=None):
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
        'Stay within the supplied paths and acceptance checks. Do not explore unrelated repository conventions. '
        'If required context is missing, return concise partial findings and the missing information. '
        f'Your execution budget is {timeout} seconds; report useful partial results before it expires. '
        'and remaining uncertainties honestly. Do not claim checks you did not run.\n\n'
        + json.dumps({'mode': assignment['mode'], 'assignment': assignment['assignment']})
    )
    # Isolated trust config: project-local configuration stays untrusted.
    (job / 'home/.codex/config.toml').write_text('')
    output = job / 'answer.txt'
    (job / 'prompt.txt').write_text(prompt)
    command = codex_command(workspace, output, model)
    command[command.index('ollama')] = provider
    command[command.index('model_context_window=32768')] = f'model_context_window={context}'
    command[command.index('model_auto_compact_token_limit=24576')] = f'model_auto_compact_token_limit={int(context*.75)}'
    if assignment['mode'] == 'investigate':
        command[command.index('workspace-write')] = 'read-only'
    started = time.monotonic()
    last_activity, observed, offset, pending = 0, 0, 0, b''
    commands = 0
    inactivity = assignment.get('inactivity_timeout_seconds', 300)
    with (job / 'prompt.txt').open() as stdin, (job / 'events.jsonl').open('w') as log:
        proc = subprocess.Popen(command, cwd=workspace, env=env, stdin=stdin, stdout=log,
                                stderr=log, start_new_session=True)
        try:
            while proc.poll() is None:
                if cancel.is_set():
                    return 'cancelled'
                waited = progress() if progress else 0
                elapsed = time.monotonic() - started - waited
                if elapsed > timeout:
                    return 'timed_out'
                generation = getattr(progress, 'generation_events', 0)
                if generation != observed:
                    last_activity, observed = elapsed, generation
                with (job / 'events.jsonl').open('rb') as events:
                    events.seek(offset)
                    chunk = events.read(MAX_OUTPUT + 1)
                    offset += len(chunk)
                pending += chunk
                lines = pending.split(b'\n')
                pending = lines.pop()[-MAX_OUTPUT:]
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    item = event.get('item', {}) if isinstance(event, dict) else {}
                    if isinstance(item, dict) and item.get('type') in ('command_execution', 'agent_message', 'reasoning', 'file_change'):
                        last_activity = elapsed
                    if isinstance(item, dict) and item.get('type') == 'command_execution' and item.get('status') in ('completed', 'failed'):
                        commands += 1
                if progress:
                    progress.observed = dict(completed_commands=commands, last_activity_execution_seconds=last_activity,
                                             inactivity_seconds=max(0, elapsed - last_activity),
                                             execution_seconds=max(0, elapsed))
                if elapsed - last_activity > inactivity:
                    return 'inactivity_timed_out'
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


def usage_status(job, task):
    """Small prompt-free live receipt, independent of the larger terminal result."""
    now = time.time()
    if now - task.get('_usage_updated', 0) < 2:
        return
    task['_usage_updated'] = now
    try:
        task['partial_patch_available'] = bool(git(job / 'workspace', 'status', '--porcelain'))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        task['partial_patch_available'] = None  # Telemetry must not interrupt work.
    keys = ('task_id', 'status', 'model', 'provider', 'resolved_model', 'revision', 'runtime_model',
            'identity_verified', 'parent_session_id', 'parent_identity_verified', 'started_at', 'generation_events', 'workflow_snapshot',
            'adapter_version', 'manager_protocol', 'timeout_seconds', 'max_timeout_seconds', 'inactivity_timeout_seconds', 'queue_timeout_seconds', 'queue_seconds',
            'loading_seconds', 'execution_seconds', 'request_ids', 'completed_commands', 'last_activity_execution_seconds',
            'inactivity_seconds', 'partial_patch_available')
    record = {k: task.get(k) for k in keys}
    record['updated_at'] = now
    temporary = job / 'usage.tmp'
    temporary.write_text(json.dumps(record)); temporary.chmod(0o600)
    temporary.replace(job / 'usage.json')


class Worker:
    def __init__(self):
        self.tasks = {}
        self.threads = []

    def configuration(self, workspace):
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise ValueError('workspace must be an absolute repository root')
        config = settings()
        routing = inference()['effective'](workspace)
        limits = inference()['policy'](routing)
        if routing:
            config = dict(config, model=routing['model'], provider=routing['provider'], context=routing['context_tokens'])
        chosen = dict(limits, provider=config.get('provider', 'ollama'), model=config['model'],
                      context_tokens=config.get('context', 32768))
        return config, chosen

    def check_availability(self, args, deadline=None):
        if not isinstance(args, dict) or set(args) != {'workspace'}:
            raise ValueError('provide only workspace')
        config, chosen = self.configuration(args['workspace'])
        if not config['enabled'] or chosen['mode'] in ('disabled', 'local-only'):
            view = dict(state='unknown', reason='Local delegation is disabled or requires a native local lead', blockers=[])
        else:
            probe = inference()['helper']('t_local_manager')['availability']
            view = probe() if deadline is None else probe(deadline=deadline)
        return dict(view, adapter_version=ADAPTER_VERSION, model=chosen['model'], provider=chosen['provider'],
                    effective_limits={key: chosen[key] for key in ('timeout_seconds', 'max_timeout_seconds', 'inactivity_timeout_seconds', 'queue_timeout_seconds')})

    def preflight(self, workspace):
        view = self.check_availability({'workspace': workspace})
        # A single short wait is justified only by observed completed generation.
        if view.get('retry_after_seconds') == 15:
            deadline = time.monotonic() + 15
            while view.get('retry_after_seconds') == 15 and time.monotonic() < deadline:
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
                view = self.check_availability({'workspace': workspace}, deadline=deadline)
        return view

    def start(self, args):
        if not isinstance(args, dict) or set(args) - {'workspace', 'assignment', 'mode', 'timeout_seconds', 'routing_reason', 'parent_session_id'}:
            raise ValueError('unexpected task arguments')
        for key in ('routing_reason', 'parent_session_id'):
            if key in args and (not isinstance(args[key], str) or len(args[key]) > 1000):
                raise ValueError(key + ' must be a string of at most 1000 characters')
        text = args.get('assignment')
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError('assignment must contain 1–16000 characters')
        mode = args.get('mode', 'investigate')
        timeout = args.get('timeout_seconds')
        if mode not in ('investigate', 'patch') or ('timeout_seconds' in args and (type(timeout) is not int or not 10 <= timeout <= 1800)):
            raise ValueError('choose investigate/patch and a timeout from 10 to 1800 seconds')
        workspace = args.get('workspace')
        config, limits = self.configuration(workspace)
        if limits['mode'] == 'disabled':
            raise ValueError('local delegation is disabled by the repository workflow')
        if limits['mode'] == 'local-only':
            raise ValueError('local-only work requires a new native local lead; this MCP connection cannot verify its lead identity')
        timeout = limits['timeout_seconds'] if timeout is None else min(timeout, limits['max_timeout_seconds'])
        if not config['enabled']:
            raise ValueError('local worker is disabled; run t config local')
        if limits['mode'] == 'automatic':
            admission = self.preflight(workspace)
            if admission['state'] != 'available':
                return dict(status='busy', accepted=False, availability=admission, adapter_version=ADAPTER_VERSION)
        state_root().mkdir(parents=True, exist_ok=True, mode=0o700)
        admitted = None
        try:
            task_id = uuid.uuid4().hex
            job = state_root() / task_id
            job.mkdir(mode=0o700)
            (job / 'assignment.json').write_text(json.dumps(dict(args, mode=mode, timeout_seconds=timeout, inactivity_timeout_seconds=limits['inactivity_timeout_seconds'])))
            source_head = snapshot(workspace, job / 'workspace')
            (job / 'assignment.json').chmod(0o600)
            cancel = threading.Event()
            if limits['mode'] == 'automatic':
                admitted = inference()['helper']('t_local_manager')['Client'](dict(limits, timeout_seconds=timeout), 'worker', cancel=cancel)
                try:
                    admitted.__enter__()
                except (OSError, ValueError) as exc:
                    shutil.rmtree(job)
                    return dict(status='busy', accepted=False, adapter_version=ADAPTER_VERSION,
                                availability=getattr(exc, 'availability', dict(state='unknown', reason=str(exc), blockers=[])))
            task = {'task_id': task_id, 'status': 'queued', 'source_head': source_head, 'model': config['model'],
                    'accepted': True, 'adapter_version': ADAPTER_VERSION, 'mode': limits['mode'],
                    'manager_protocol': admitted.info.get('protocol') if admitted else None,
                    'workspace': str(job / 'workspace'), 'source_workspace': workspace, 'started_at': time.time(),
                    'provider': config.get('provider', 'ollama'), 'context_tokens': config.get('context', 32768),
                    'routing_reason': str(args.get('routing_reason', 'explicit assignment'))[:1000],
                    'parent_session_id': str(args.get('parent_session_id', '')), 'identity_verified': False, 'parent_identity_verified': False,
                    'timeout_seconds': timeout, 'max_timeout_seconds': limits['max_timeout_seconds'], 'inactivity_timeout_seconds': limits['inactivity_timeout_seconds'],
                    'queue_timeout_seconds': limits['queue_timeout_seconds']}
            # Codex supplies this launch identity to its MCP child. A caller's
            # parent_session_id alone never establishes attribution.
            native_parent = os.environ.get('CODEX_THREAD_ID', '')
            if re.fullmatch(r'[0-9a-fA-F-]{36}', native_parent) and task['parent_session_id'] in ('', native_parent):
                task.update(parent_session_id=native_parent, parent_identity_verified=True)
            try:
                task['workflow_snapshot'] = runpy.run_path(str(Path(__file__).with_name('t_usage.py')))['launch_context']('codex', workspace)
            except (OSError, ValueError):
                pass  # Missing attribution must never block delegated work.
            usage_status(job, task)
            self.tasks[task_id] = (task, cancel)
            thread = threading.Thread(target=self.execute,
                                      args=(task, cancel, job, config['model'], timeout, admitted))
            thread.start()
            self.threads.append(thread)
            return dict(task)
        except BaseException:
            if admitted and admitted.info:
                admitted.__exit__(None, None, None)
            self.tasks.pop(task_id, None)
            raise

    def execute(self, task, cancel, job, model, timeout, admitted=None):
        status = 'failed'
        local = inference()
        chosen = local['policy']({'mode': task['mode'], 'provider': task['provider'], 'model': model,
                                  'context_tokens': task['context_tokens'], 'timeout_seconds': timeout,
                                  'queue_timeout_seconds': task['queue_timeout_seconds'],
                                  'inactivity_timeout_seconds': task['inactivity_timeout_seconds']})
        try:
            if cancel.is_set():
                status = 'cancelled'
            else:
                with local['helper']('t_local_status')['Activity'](model) as activity:
                    leased = None if admitted else local['leased_runtime'](chosen)
                    if leased:
                        task.update(resolved_model=model, revision=leased['revision'], runtime_model=leased['alias'], identity_verified=True)
                        with activity.endpoint(leased['base']) as observed:
                            status = run_worker(job, observed, leased['alias'], timeout, cancel, task['provider'], task['context_tokens'])
                    else:
                        with admitted or local['helper']('t_local_manager')['Client'](chosen, 'worker', activity, cancel) as client:
                            client.activity = activity
                            def progress():
                                state = client.last.get('state', 'working')
                                task.update(status=state if state in ('queued', 'loading', 'waiting_for_memory', 'waiting_for_legacy') else 'running',
                                            queue_seconds=round(client.last.get('queue_seconds', client.waited()), 2),
                                            loading_seconds=round(client.last.get('loading_seconds', 0), 2),
                                            request_ids=client.last.get('request_ids', []),
                                            manager_protocol=client.last.get('protocol'),
                                            identity_verified=client.last.get('identity_verified', False),
                                            revision=client.last.get('revision'), runtime_model=client.last.get('runtime_model'),
                                            owner_verified=client.last.get('owner_verified', False))
                                progress.generation_events = client.last.get('generation_events', 0)
                                task['generation_events'] = progress.generation_events
                                task.update(getattr(progress, 'observed', {}))
                                if task['identity_verified']:
                                    task['resolved_model'] = model
                                usage_status(job, task)
                                return client.waited()
                            status = run_worker(job, client.base, model, timeout, cancel,
                                                task['provider'], task['context_tokens'], progress=progress)
                            progress()
                        task.update(identity_verified=client.last.get('identity_verified', False),
                                    revision=client.last.get('revision'), runtime_model=client.last.get('runtime_model'),
                                    queue_seconds=round(client.last.get('queue_seconds', client.waited()), 2),
                                    loading_seconds=round(client.last.get('loading_seconds', 0), 2))
                        if task['identity_verified']:
                            task['resolved_model'] = model
                        if client.error:
                            task['error'] = client.error
                            status = 'failed'
                        if client.last.get('outcome'):
                            outcome = client.last['outcome']
                            phase = client.last.get('timeout_phase') or 'queue'
                            status = 'busy' if outcome == 'busy' else 'timed_out' if outcome == 'timed_out' and phase == 'execution' else 'failed'
                            task['failure_reason'] = phase + '_timeout' if outcome == 'timed_out' else outcome
                            task['error'] = client.last.get('failure_reason')
                task.update(collect(job))
                if status != 'completed':
                    events = job / 'events.jsonl'
                    task['diagnostic'] = events.read_bytes()[-4000:].decode(errors='replace') if events.exists() else ''
        except Exception as exc:
            task['error'] = str(exc)
        finally:
            if admitted and not admitted.closed.is_set():
                admitted.__exit__(None, None, None)
            task['elapsed_seconds'] = round(time.time() - task['started_at'], 2)
            task.setdefault('execution_seconds', max(0, task['elapsed_seconds'] - task.get('queue_seconds', 0) - task.get('loading_seconds', 0)))
            if status != 'completed':
                task.setdefault('failure_reason', status)
            task['partial_patch_available'] = bool(task.get('patch'))
            task['status'] = status
            (job / 'result.json').write_text(json.dumps(task))
            (job / 'result.json').chmod(0o600)

    def call(self, name, args):
        if name == 'check_availability':
            return self.check_availability(args)
        if name == 'start_task':
            return self.start(args)
        if name == 'wait_task':
            if not isinstance(args, dict) or set(args) - {'task_id', 'timeout_seconds'}:
                raise ValueError('invalid wait_task arguments')
            seconds = args.get('timeout_seconds', 60)
            if type(seconds) is not int or not 1 <= seconds <= 60:
                raise ValueError('wait timeout must be 1..60 seconds')
            watch = runpy.run_path(str(Path(__file__).with_name('t_watch.py')))
            activity = watch['Activity'](args.get('task_id', ''), 'local worker')
            return watch['wait'](lambda: self.call('task_status', {'task_id': args.get('task_id')}),
                                 seconds, activity=activity)
        if name not in ('task_status', 'cancel_task'):
            raise ValueError('unknown local worker tool')
        if not isinstance(args, dict) or set(args) != {'task_id'} or not isinstance(args['task_id'], str) or args['task_id'] not in self.tasks:
            raise ValueError('unknown task_id in this MCP connection')
        task, cancel = self.tasks[args['task_id']]
        if name == 'cancel_task' and task['status'] not in TERMINAL:
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
        {'name': 'check_availability', 'description': 'Read shared-runtime availability, blockers and effective limits without starting inference. Automatic delegation skips busy or unknown runtimes.',
         'annotations': {'readOnlyHint': True},
         'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['workspace'],
                         'properties': {'workspace': {'type': 'string'}}}},
        {'name': 'wait_task', 'description': 'Wait up to 60 seconds for a task owned by this connection. Returns terminal evidence or pending; timeout does not cancel the task.',
         'annotations': {'readOnlyHint': True},
         'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['task_id'],
                         'properties': {'task_id': {'type': 'string'}, 'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 60}}}},
        {'name': 'start_task', 'description': INSTRUCTIONS,
         'inputSchema': {'type': 'object', 'additionalProperties': False,
                         'required': ['workspace', 'assignment'], 'properties': {
                             'workspace': {'type': 'string', 'description': 'Absolute repository root; current tracked and nonignored files are snapshotted.'},
                             'assignment': {'type': 'string', 'minLength': 1, 'maxLength': 16000},
                             'routing_reason': {'type': 'string', 'description': 'Why this bounded task is suitable for local delegation.'},
                             'parent_session_id': {'type': 'string', 'description': 'Lead session ID if known; user-supplied, not verified ownership.'},
                             'mode': {'type': 'string', 'enum': ['investigate', 'patch'], 'default': 'investigate'},
                             'timeout_seconds': {'type': 'integer', 'minimum': 10, 'maximum': 1800, 'description': 'Omit to inherit the workflow task limit (default 900 seconds). Explicit requests are capped by its maximum task limit.'}}}},
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
                                             {'name': 't-local-worker', 'version': ADAPTER_VERSION}, INSTRUCTIONS)
            if response is not None:
                print(json.dumps(response), flush=True)
    finally:
        worker.close()
        signal.signal(signal.SIGTERM, previous)
    return 0
