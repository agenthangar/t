#!/usr/bin/env python3
"""Persistent evaluation configuration, immutable manifests, and durable run state."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = 'swe-bench-pro-v2'
BENCHMARK_REVISION = '66f92766bba642462d4bbe5479e83f91f9211862'
BENCHMARK_URL = 'https://github.com/scaleapi/SWE-bench_Pro-os'
PROFILES = {
    'quick': {'label': 'Quick', 'tasks': 3, 'seconds': 900, 'description': 'Reduced-budget smoke evaluation'},
    'standard': {'label': 'Standard', 'tasks': 11, 'seconds': 3000, 'description': 'One fixed task per repository'},
    'advanced': {'label': 'Advanced · HARD-51', 'tasks': 51, 'seconds': 3000, 'description': 'Official difficult subset'},
    'full': {'label': 'Full', 'tasks': 642, 'seconds': 3000, 'description': 'Complete validated public split'},
}
MODES = {
    'frontier-only': ('Frontier-only', 'The frontier model completes the task without local delegation.'),
    'automatic': ('Automatic routing', 'The frontier model decides whether to delegate work locally.'),
    'required-local': ('Required local delegation', 'The frontier model must assign work locally, then review and validate the result.'),
    'local-only': ('Local-only', 'The selected local model completes the task with cloud access disabled.'),
    'outage': ('Outage continuation', 'After a frontier connection failure, the local model continues from the saved checkpoint.'),
}
TERMINAL = {'completed', 'failed', 'interrupted', 'cancelled', 'blocked'}
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
RUN_ID = re.compile(r'eval-[0-9]{8}-[0-9a-f]{12}\Z')


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def paths():
    home = Path.home()
    return {
        'config': Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 't/eval.json',
        'state': Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 't/evals',
        'cache': Path(os.environ.get('XDG_CACHE_HOME', home / '.cache')) / 't/eval',
    }


def atomic(path, value):
    """Durable replacement; readers never observe half a checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return copy.deepcopy(default)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def defaults(name='standard'):
    return {'name': name, 'benchmark': BENCHMARK, 'profile': 'standard',
            'frontier_model': 'gpt-6-astra', 'reasoning_effort': 'automatic',
            'local_model': 'mlx-community/Qwen3.8-27B-8bit', 'local_provider': 'lmstudio',
            'context_tokens': 32768, 'modes': list(MODES), 'task_seconds': 3000,
            'repetitions': 1, 'concurrency': 1, 'spend_limit_usd': None, 'pricing': None}


def validate(value):
    if not isinstance(value, dict) or set(value) - set(defaults()):
        raise ValueError('invalid evaluation configuration fields')
    result = dict(defaults(), **value)
    if not isinstance(result['name'], str) or not NAME.fullmatch(result['name']):
        raise ValueError('configuration name must contain 1–64 letters, digits, underscores or hyphens')
    if result['benchmark'] != BENCHMARK or result['profile'] not in PROFILES:
        raise ValueError('unknown benchmark or profile')
    for key in ('frontier_model', 'local_model'):
        if not isinstance(result[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}', result[key]):
            raise ValueError('invalid ' + key)
    if result['local_provider'] not in ('lmstudio', 'ollama'):
        raise ValueError('local provider must be lmstudio or ollama')
    if result['reasoning_effort'] not in ('automatic', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise ValueError('unsupported reasoning effort')
    modes = result['modes']
    if not isinstance(modes, list) or not modes or any(not isinstance(m, str) or m not in MODES for m in modes) or len(set(modes)) != len(modes):
        raise ValueError('select one or more distinct workflow modes')
    for key, minimum, maximum in (('context_tokens', 4096, 131072), ('task_seconds', 60, 7200), ('repetitions', 1, 10)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f'{key} must be an integer from {minimum} to {maximum}')
    if type(result['concurrency']) is not int or result['concurrency'] not in (1, 2, 4):
        raise ValueError('concurrency must be 1, 2 or 4')
    limit = result['spend_limit_usd']
    if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0):
        raise ValueError('spend limit must be a nonnegative USD amount or null')
    pricing = result['pricing']
    if pricing is not None:
        keys = {'model', 'source', 'as_of', 'service_tier', 'input_per_million', 'cached_input_per_million', 'output_per_million', 'kind'}
        if not isinstance(pricing, dict) or set(pricing) != keys:
            raise ValueError('pricing requires model, source, as_of, service_tier, kind and three per-million rates')
        if pricing['model'] != result['frontier_model'] or pricing['kind'] not in ('api-estimate', 'api-equivalent'):
            raise ValueError('pricing must identify the selected model and billing basis')
        for key in ('source', 'as_of', 'service_tier'):
            if not isinstance(pricing[key], str) or not pricing[key].strip():
                raise ValueError('pricing metadata is required')
        for key in ('input_per_million', 'cached_input_per_million', 'output_per_million'):
            if type(pricing[key]) not in (int, float) or not math.isfinite(pricing[key]) or pricing[key] < 0:
                raise ValueError('pricing rates must be finite nonnegative numbers')
    return copy.deepcopy(result)


def configurations():
    document = read(paths()['config'], {'version': 1, 'configurations': {}})
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('configurations'), dict):
        raise ValueError('unsupported evaluation configuration document')
    result = {name: validate(value) for name, value in document['configurations'].items()}
    if any(name != value['name'] for name, value in result.items()):
        raise ValueError('configuration name differs from its key')
    return result


@contextlib.contextmanager
def lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def save_config(value, original=None):
    value = validate(value)
    with lock(paths()['config'].with_suffix('.lock')):
        current = configurations()
        if original and original != value['name'] and value['name'] in current:
            raise ValueError('configuration name already exists')
        if original and original != value['name']:
            current.pop(original, None)
        current[value['name']] = value
        atomic(paths()['config'], {'version': 1, 'configurations': current})
    return value


def delete_config(name):
    with lock(paths()['config'].with_suffix('.lock')):
        current = configurations()
        if name not in current:
            raise ValueError('unknown configuration: ' + name)
        del current[name]
        atomic(paths()['config'], {'version': 1, 'configurations': current})


def plan(config):
    config = validate(config)
    count = PROFILES[config['profile']]['tasks'] * len(config['modes']) * config['repetitions']
    return {'config': config, 'attempts': count, 'maximum_agent_seconds': count * config['task_seconds'],
            'estimated_cost_usd': None, 'cost_note': 'No comparable measurements; cost is unknown.',
            'benchmark_revision': BENCHMARK_REVISION,
            'requirements': ['Harbor', 'Docker Linux/amd64 with enforced egress policy', 'Codex CLI',
                             'ready selected local model' if config['modes'] != ['frontier-only'] else 'frontier credentials'],
            'cached_benchmark': (paths()['cache'] / BENCHMARK_REVISION / 'v2/SHA256SUMS').is_file()}


def run_directory(run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError('invalid evaluation run ID')
    return paths()['state'] / run_id


def create_run(config):
    config = validate(config)
    run_id = 'eval-' + time.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:12]
    directory = run_directory(run_id)
    manifest = {'version': 1, 'id': run_id, 'created_at': time.time(), 'config': config,
                'benchmark_revision': BENCHMARK_REVISION, 'seed': 0, 'source_hash': source_hash(),
                'source_revision': subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5).stdout.strip()}
    atomic(directory / 'manifest.json', manifest)
    for source in source_files(ROOT):
        target = directory / 'source' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
    atomic(directory / 'state.json', {'version': 1, 'id': run_id, 'manifest_hash': digest(manifest),
           'status': 'pending', 'phase': 'preparation', 'updated_at': time.time(), 'trials': [],
           'total': plan(config)['attempts'], 'diagnostic': None})
    return run_id


def source_files(root):
    files = [root / p for p in ('bin/t', 'ui/fzf.sh', 'libexec/mlx-models.json', 'libexec/local-models.json', 'libexec/t_issue_policy.txt')]
    return files + sorted((root / 'libexec').glob('*.py'))


def source_hash(root=ROOT):
    return digest([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in source_files(root)])


def load_run(run_id):
    directory = run_directory(run_id)
    manifest, state = read(directory / 'manifest.json'), read(directory / 'state.json')
    if not manifest or not state:
        raise ValueError('unknown evaluation run: ' + run_id)
    if digest(manifest) != state.get('manifest_hash'):
        raise ValueError('run manifest changed; refuse to resume altered settings')
    if state['status'] in ('running', 'pending') and time.time() - state['updated_at'] > 30 and not owned(directory):
        state = {**state, 'status': 'interrupted', 'diagnostic': 'Worker stopped; resume to recover durable results.'}
    return {'manifest': manifest, **state}


def owned(directory):
    try:
        with (directory / 'owner.lock').open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False
    except FileNotFoundError:
        return False


def list_runs():
    root = paths()['state']
    if not root.exists():
        return []
    result = []
    for directory in sorted(root.iterdir(), reverse=True):
        if directory.is_dir() and RUN_ID.fullmatch(directory.name) and (directory / 'state.json').is_file():
            result.append(load_run(directory.name))
    return sorted(result, key=lambda r: r['manifest']['created_at'], reverse=True)


class Journal:
    def __init__(self, run_id):
        self.directory = run_directory(run_id)
        value = load_run(run_id)
        self.manifest = value.pop('manifest')
        self.state = value
        self.mutex = threading.RLock()

    def checkpoint(self, **changes):
        with self.mutex:
            self.state.update(changes, updated_at=time.time())
            atomic(self.directory / 'state.json', self.state)

    def event(self, kind, **data):
        with self.mutex:
            record = {'at': time.time(), 'kind': kind, **data}
            with (self.directory / 'events.jsonl').open('a') as stream:
                stream.write(json.dumps(record, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            self.checkpoint(**({'phase': data['action']} if kind == 'preparation' and 'action' in data else {}))

    def finish_trial(self, trial):
        with self.mutex:
            atomic(self.directory / 'trials' / trial['id'] / 'result.json', trial)
            self.state['trials'] = [t for t in self.state['trials'] if t['id'] != trial['id']] + [trial]
            self.event('trial_completed', trial_id=trial['id'], status=trial['status'])

    def start_trial(self, trial):
        with self.mutex:
            self.state['trials'].append({**trial, 'status': 'running', 'resolved': None, 'workflow_valid': None})
            self.event('trial_started', **trial)

    def recover(self):
        # This runs only after acquiring the exclusive owner lock. A trial whose
        # result was fsynced before a crash must not be executed a second time.
        for trial in list(self.state['trials']):
            if trial['status'] == 'running':
                result = read(self.directory / 'trials' / trial['id'] / 'result.json')
                self.finish_trial(result or {**trial, 'status': 'interrupted',
                    'diagnostic': 'Previous worker stopped before a durable completion.'})


def summarize(run):
    config = run['manifest']['config']
    modes = {}
    for mode in config['modes']:
        trials = [t for t in run['trials'] if t['mode'] == mode]
        # Retries remain cost-bearing evidence, but each task/repetition has one score.
        latest = {}
        for trial in trials:
            latest[(trial['task'], trial['repetition'])] = trial
        scored = [t for t in latest.values() if isinstance(t.get('resolved'), bool)]
        expected = PROFILES[config['profile']]['tasks'] * config['repetitions']
        resolved = sum(t['resolved'] for t in scored)
        usage = {provider: {'output_tokens': 0, 'request_seconds': 0, 'requests': 0} for provider in ('frontier', 'local')}
        costs = []
        for trial in trials:
            if trial.get('cost_usd') is not None:
                costs.append(trial['cost_usd'])
            for provider, metrics in trial.get('usage', {}).items():
                if provider in usage:
                    for key in usage[provider]:
                        usage[provider][key] += metrics.get(key, 0)
        modes[mode] = {'label': MODES[mode][0], 'resolved': resolved, 'graded': len(scored), 'expected': expected,
                       'score_percent': 100 * resolved / expected, 'coverage_percent': 100 * len(scored) / expected,
                       'provisional': len(scored) != expected, 'attempts': len(trials),
                       'workflow_failures': sum(t.get('workflow_valid') is False for t in trials),
                       'estimated_cost_usd': sum(costs) if len(costs) == len(trials) and trials else (0 if mode == 'local-only' else None),
                       'observed_tokens_per_second': {p: v['output_tokens'] / v['request_seconds'] if v['requests'] and v['request_seconds'] > 0 else None for p, v in usage.items()}}
    return {'id': run['id'], 'status': run['status'], 'phase': run['phase'], 'profile': config['profile'],
            'benchmark': config['benchmark'], 'total': run['total'], 'finished': len({(t['task'], t['mode'], t['repetition']) for t in run['trials'] if t['status'] in TERMINAL}),
            'modes': modes, 'diagnostic': run.get('diagnostic'), 'updated_at': run['updated_at']}


def compare_runs(first, second):
    keys = ('benchmark', 'profile', 'task_seconds', 'repetitions', 'concurrency', 'context_tokens')
    a, b = first['manifest'], second['manifest']
    differences = [k for k in keys if a['config'][k] != b['config'][k]]
    for key in ('benchmark_revision', 'seed'):
        if a[key] != b[key]:
            differences.append(key)
    if first.get('task_ids') != second.get('task_ids'):
        differences.append('task_ids')
    if differences:
        raise ValueError('incompatible comparisons: ' + ', '.join(differences))
    return {'runs': [summarize(first), summarize(second)], 'matching_modes': sorted(set(a['config']['modes']) & set(b['config']['modes']))}


def request_cancel(run_id):
    value = load_run(run_id)
    if value['status'] in TERMINAL:
        return value
    atomic(run_directory(run_id) / 'cancel.json', {'requested_at': time.time()})
    return value


def launch(run_id, retry_interrupted=False):
    directory = run_directory(run_id)
    value = load_run(run_id)
    if value['manifest']['source_hash'] != source_hash(directory / 'source'):
        raise ValueError('saved workflow source changed; refuse to resume altered code')
    with lock(directory / 'launch.lock', blocking=False):
        with lock(directory / 'owner.lock', blocking=False):
            (directory / 'cancel.json').unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('COV_CORE_', 'T_DEV_CONTEXT'))}
        with (directory / 'worker.log').open('a') as log:
            command = [sys.executable, str(directory / 'source/libexec/t_eval.py'), '--worker', run_id]
            if retry_interrupted:
                command.append('--retry-interrupted')
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
        # Keep the launch lock until the child owns the run or exits.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            if read(directory / 'state.json').get('owner_pid') == process.pid:
                break
            time.sleep(.05)
        return process.pid


def worker(run_id, retry_interrupted=False):
    journal = Journal(run_id)
    with lock(journal.directory / 'owner.lock', blocking=False):
        journal.recover()
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(5):
                journal.checkpoint()
        thread = threading.Thread(target=heartbeat, daemon=True)
        def cancel(*_):
            request_cancel(run_id)
        previous = {s: signal.signal(s, cancel) for s in (signal.SIGTERM, signal.SIGINT)}
        thread.start()
        try:
            journal.checkpoint(status='running', owner_pid=os.getpid(), diagnostic=None)
            helper('t_eval_engine')['execute'](journal, retry_interrupted)
        except Exception as error:
            journal.checkpoint(status='blocked', diagnostic=str(error))
        finally:
            stop.set()
            thread.join(timeout=6)
            journal.checkpoint(owner_pid=None)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    if len(sys.argv) in (3, 4) and sys.argv[1] == '--worker':
        worker(sys.argv[2], '--retry-interrupted' in sys.argv[3:])
    else:
        raise SystemExit('private eval worker; use t eval')
