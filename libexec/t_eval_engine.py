"""Pinned benchmark preparation and bounded, resumable Harbor trial orchestration."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import tarfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
HARBOR_VERSION = '0.25.0'


def core():
    return runpy.run_path(str(ROOT / 'libexec/t_eval.py'))


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=kwargs.pop('timeout', 120), **kwargs)
    if result.returncode:
        raise ValueError('Command failed: ' + command[0] + '\n' + result.stderr[-3000:])
    return result.stdout.strip()


def selected_tasks(root, profile):
    tasks = sorted((Path(root) / 'v2/tasks').iterdir())
    tasks = [p for p in tasks if p.is_dir() and (p / 'task.toml').is_file()]
    if profile == 'full':
        selected = tasks
    elif profile == 'advanced':
        names = set((Path(root) / 'v2/hard51_ids.txt').read_text().splitlines())
        selected = [p for p in tasks if p.name in names]
    else:
        groups = {}
        for task in tasks:
            # Instance IDs retain repository identity before the 40-digit commit.
            match = re.fullmatch(r'instance_(.+)-[0-9a-f]{40}(?:-.+)?', task.name)
            if not match:
                raise ValueError('unrecognized benchmark task identity: ' + task.name)
            groups.setdefault(match[1], []).append(task)
        order = lambda p: hashlib.sha256(('0:' + p.name).encode()).hexdigest()
        selected = [min(group, key=order) for _, group in sorted(groups.items())]
        if profile == 'quick':
            selected = sorted(selected, key=order)[:3]
    expected = core()['PROFILES'][profile]['tasks']
    if len(selected) != expected:
        raise ValueError(f'benchmark task count changed: expected {expected}, found {len(selected)}')
    return selected


def verify_checksums(root):
    base = Path(root) / 'v2'
    for line in (base / 'SHA256SUMS').read_text().splitlines():
        checksum, relative = line.split(maxsplit=1)
        relative = relative.lstrip('*')
        path = base / relative
        if not path.resolve().is_relative_to(base.resolve()) or path.is_symlink():
            raise ValueError('unsafe benchmark checksum path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('benchmark checksum mismatch: ' + relative)


def prepare_benchmark(journal):
    m = core()
    cache = m['paths']()['cache']
    root = cache / m['BENCHMARK_REVISION']
    with m['lock'](cache / 'prepare.lock'):
        if not (root / '.git').is_dir():
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            run(['git', 'init', '-q', str(root)])
        if not (root / 'v2/SHA256SUMS').exists():
            journal.event('preparation', action='download pinned benchmark')
            run(['git', '-C', str(root), 'fetch', '--depth', '1', m['BENCHMARK_URL'], m['BENCHMARK_REVISION']], timeout=600)
            run(['git', '-C', str(root), 'checkout', '--detach', m['BENCHMARK_REVISION']])
        if run(['git', '-C', str(root), 'rev-parse', 'HEAD']) != m['BENCHMARK_REVISION']:
            raise ValueError('benchmark cache revision mismatch')
        verify_checksums(root)
    selected = selected_tasks(root, journal.manifest['config']['profile'])
    names = [p.name for p in selected]
    if journal.state.get('task_ids') not in (None, names):
        raise ValueError('selected tasks changed since this run started')
    journal.checkpoint(task_ids=names)
    return root, selected


def harbor_python(journal):
    explicit = os.environ.get('T_EVAL_PYTHON')
    if explicit:
        python = Path(explicit)
    else:
        root = core()['paths']()['cache'] / ('harbor-' + HARBOR_VERSION)
        python = root / 'bin/python'
        with core()['lock'](root.parent / 'harbor.lock'):
            if not python.exists():
                if sys.version_info < (3, 12):
                    raise ValueError('evaluation requires Python 3.12 or newer')
                journal.event('preparation', action='create isolated Harbor environment')
                run([sys.executable, '-m', 'venv', str(root)], timeout=120)
            ready = subprocess.run([str(python), '-c', f'import importlib.metadata as m;assert m.version("harbor")=="{HARBOR_VERSION}"'], capture_output=True)
            if ready.returncode:
                journal.event('preparation', action='install pinned Harbor library')
                run([str(python), '-m', 'pip', 'install', 'harbor==' + HARBOR_VERSION], timeout=900)
    run([str(python), '-c', f'import importlib.metadata as m;assert m.version("harbor")=="{HARBOR_VERSION}"'])
    return python


def prepare_runtime(config, journal):
    with core()['lock'](core()['paths']()['cache'] / 'runtime.lock'):
        return runtime_owned(config, journal)


def runtime_owned(config, journal):
    if not shutil.which('docker') or not shutil.which('colima'):
        raise ValueError('Install Docker CLI and Colima first: brew install colima docker docker-compose docker-buildx. Then resume this run.')
    if not shutil.which('codex'):
        raise ValueError('Codex CLI is missing; install it before resuming')
    docker_config = core()['paths']()['cache'] / 'docker'
    plugins = {str(Path(path).resolve().parent) for name in ('docker-compose', 'docker-buildx')
               if (path := shutil.which(name))}
    settings = {'cliPluginsExtraDirs': sorted(plugins)}
    core()['atomic'](docker_config / 'config.json', settings)
    os.environ['DOCKER_CONFIG'] = str(docker_config)
    for plugin in ('compose', 'buildx'):
        try:
            run(['docker', plugin, 'version'])
        except ValueError as error:
            raise ValueError(f'Docker {plugin} is missing; brew install docker-{plugin}, then resume this run') from error
    memory = 4 + 4 * config['concurrency']
    context = 'colima-t-eval'
    contexts = run(['docker', 'context', 'ls', '--format', '{{.Name}}']).splitlines()
    if context not in contexts or subprocess.run(['docker', '--context', context, 'info'], capture_output=True).returncode:
        journal.event('preparation', action='start dedicated evaluation VM', memory_gib=memory)
        run(['colima', 'start', '--profile', 't-eval', '--vm-type', 'vz', '--vz-rosetta',
             '--runtime', 'docker', '--cpu', '8', '--memory', str(memory), '--disk', '100'], timeout=600)
    # Only this process and descendants use the dedicated engine; no global switch.
    os.environ['DOCKER_CONTEXT'] = context
    info = json.loads(run(['docker', 'info', '--format', '{{json .}}']))
    if info.get('OSType') != 'linux':
        raise ValueError('benchmark environments require Linux containers')
    if info.get('MemTotal', 0) < (memory - 1) * 1024**3:
        raise ValueError(f'evaluation VM needs {memory} GiB; stop it and restart colima --profile t-eval with --memory {memory}')
    version = run(['codex', '--version'])
    match = re.search(r'\b(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?)\b', version)
    if not match:
        raise ValueError('cannot pin the installed Codex CLI version')
    journal.checkpoint(runtime={'harbor_version': HARBOR_VERSION, 'codex_version': match[1],
                                'docker_context': context, 'vm_memory_gib': memory})
    return match[1], memory


def ready_local(config, vm_memory):
    local = core()['helper']('t_local_inference')
    chosen = local['policy']({'model': config['local_model'], 'provider': config['local_provider'], 'context_tokens': config['context_tokens']})
    catalogue = local['catalogue'](context=config['context_tokens'])
    row = next((r for r in catalogue['models'] if r['model'] == chosen['model'] and r['provider'] == chosen['provider']), None)
    if not row or not row.get('ready') or row.get('fit') is not True:
        raise ValueError('selected local model is not verified and ready; use t config models install ' + chosen['model'])
    ram = catalogue['hardware'].get('ram_bytes')
    required = row.get('memory_bytes', row['bytes'] * 1.2) + (vm_memory + 8) * 1024**3
    if not ram or required > ram:
        raise ValueError('selected model does not fit alongside the evaluation VM and OS reserve')
    return local, chosen


def native_codex(version, journal):
    """Pin official Linux binaries to the VM CPU, independently of task images."""
    architecture = run(['docker', 'info', '--format', '{{.Architecture}}'])
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}.get(architecture)
    if not arch:
        raise ValueError('unsupported evaluation VM CPU: ' + architecture)
    m = core()
    destination = m['paths']()['cache'] / ('codex-' + version + '-linux-' + arch)
    with m['lock'](destination.with_suffix('.lock')):
        receipt = m['read'](destination / 'receipt.json')
        if receipt:
            for name, checksum in receipt['files'].items():
                path = destination / name
                if not path.resolve().is_relative_to(destination.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                    raise ValueError('cached Codex binary checksum mismatch')
        else:
            journal.event('preparation', action='cache pinned native Linux Codex', version=version, architecture=arch)
            url = 'https://registry.npmjs.org/@openai%2Fcodex/' + version + '-linux-' + arch
            with urllib.request.urlopen(url, timeout=30) as response:
                metadata = json.load(response)
            distribution = metadata['dist']
            if not distribution['tarball'].startswith('https://registry.npmjs.org/@openai/codex/-/'):
                raise ValueError('unexpected Codex artifact origin')
            with urllib.request.urlopen(distribution['tarball'], timeout=300) as response:
                archive = response.read()
            integrity = 'sha512-' + base64.b64encode(hashlib.sha512(archive).digest()).decode()
            if integrity != distribution['integrity']:
                raise ValueError('Codex artifact integrity mismatch')
            triple = 'aarch64' if arch == 'arm64' else 'x86_64'
            prefix = 'package/vendor/' + triple + '-unknown-linux-musl/'
            required = {'bin/codex', 'bin/codex-code-mode-host', 'codex-resources/bwrap', 'codex-path/rg'}
            files = {}
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                for member in bundle.getmembers():
                    name = member.name.removeprefix(prefix)
                    if name not in required or not member.isfile():
                        continue
                    content = bundle.extractfile(member).read()
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    path.chmod(0o755)
                    files[name] = hashlib.sha256(content).hexdigest()
            if set(files) != required:
                raise ValueError('official Codex package is missing required Linux helpers')
            receipt = {'version': version, 'architecture': arch, 'integrity': integrity, 'files': files}
            m['atomic'](destination / 'receipt.json', receipt)
    journal.checkpoint(native_codex=receipt)
    return str(destination)


def pin_image(task, destination):
    data = tomllib.loads((task / 'task.toml').read_text())
    image = data['environment']['docker_image']
    try:
        metadata = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    except ValueError:
        run(['docker', 'pull', '--platform', 'linux/amd64', image], timeout=1800)
        metadata = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    if metadata.get('Architecture') != 'amd64' or not metadata.get('RepoDigests'):
        raise ValueError('task image is not a pinned amd64 image')
    digest = metadata['RepoDigests'][0]
    if not destination.exists():
        shutil.copytree(task, destination)
        config = (destination / 'task.toml').read_text().replace(json.dumps(image), json.dumps(digest))
        (destination / 'task.toml').write_text(config)
        (destination / 'environment/Dockerfile').write_text('FROM ' + digest + '\n')
    elif digest not in (destination / 'task.toml').read_text():
        raise ValueError('cached task image digest changed; refuse to alter a run')
    for source in task.rglob('*'):
        relative = source.relative_to(task)
        if source.is_file() and str(relative) not in ('task.toml', 'environment/Dockerfile'):
            target = destination / relative
            if target.is_symlink() or not target.is_file() or source.read_bytes() != target.read_bytes():
                raise ValueError('cached task differs from the pinned publisher files: ' + str(relative))
    return digest


def child_job(python, job, directory, journal):
    m = core()
    m['atomic'](directory / 'job.json', job)
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'DOCKER_CONTEXT', 'DOCKER_CONFIG',
               'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'TZ', 'LANG'}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env['PYTHONPATH'] = str(ROOT / 'libexec')
    with (directory / 'harbor.log').open('a') as log:
        proc = subprocess.Popen([str(python), str(ROOT / 'libexec/t_eval_harbor.py'), str(directory / 'job.json')],
                                stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        deadline = time.monotonic() + job['config']['task_seconds'] + 7200
        stopped = False
        while proc.poll() is None:
            if (journal.directory / 'cancel.json').exists() or time.monotonic() >= deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                stopped = True
                break
            time.sleep(.25)
        cleanup_containers(job['trial_id'])
        value = m['read'](directory / 'outcome.json')
        if stopped:
            return {'status': 'interrupted', 'resolved': None, 'workflow_valid': None, 'diagnostic': 'execution interrupted; evidence retained'}
        if not value:
            return {'status': 'failed', 'resolved': None, 'workflow_valid': None, 'diagnostic': 'Harbor failed; inspect harbor.log'}
        return value


def cleanup_containers(trial_id):
    """Reap only containers in this unique trial's Compose projects after exit."""
    if not re.fullmatch('[0-9a-f]{32}', trial_id):
        raise ValueError('invalid cleanup ownership identifier')
    rows = run(['docker', 'ps', '-a', '--format', '{{.ID}}\t{{.Label "com.docker.compose.project"}}'])
    owned = [row.split('\t')[0] for row in rows.splitlines()
             if '\t' in row and row.split('\t')[1].startswith(trial_id + '-')]
    if owned:
        run(['docker', 'rm', '-f', *owned], timeout=60)


def execute(journal, retry_interrupted=False):
    m = core()
    config = journal.manifest['config']
    has_frontier = any(mode != 'local-only' for mode in config['modes'])
    if has_frontier and config['spend_limit_usd'] is not None and config['spend_limit_usd'] > 0 and not config['pricing']:
        raise ValueError('a spend guardrail requires a dated pricing snapshot in the configuration; unknown prices cannot enforce a USD limit')
    if has_frontier and config['spend_limit_usd'] == 0:
        raise ValueError('zero cloud budget permits only local-only mode')
    codex_version, memory = prepare_runtime(config, journal)
    native = native_codex(codex_version, journal)
    python = harbor_python(journal)
    benchmark, tasks = prepare_benchmark(journal)
    images = dict(journal.state.get('images', {}))
    for index, task in enumerate(tasks, 1):
        journal.event('preparation', action=f'Preparing benchmark image {index}/{len(tasks)}', task=task.name)
        images[task.name] = pin_image(task, journal.directory / 'tasks' / task.name)
        journal.checkpoint(images=images)
    # Persist task ownership before launching; an unrecorded completion is never inferred.
    previous = {(t['task'], t['mode'], t['repetition']): t for t in journal.state['trials']}
    jobs = []
    for task in tasks:
        for mode in config['modes']:
            for repetition in range(config['repetitions']):
                key = (task.name, mode, repetition)
                existing = previous.get(key)
                if existing and not (retry_interrupted and existing['status'] in ('interrupted', 'failed') and existing.get('resolved') is None):
                    continue
                jobs.append(key)
    with ExitStack() as stack:
        local_runtime = None
        if any(mode != 'frontier-only' for mode in config['modes']):
            local, chosen = ready_local(config, memory)
            stack.enter_context(local['helper']('t_local_worker')['reservation']())
            local_runtime = stack.enter_context(local['provider_runtime'](chosen))
            journal.checkpoint(local_identity={'model': config['local_model'], 'provider': config['local_provider'],
                                               'revision': local_runtime[2]['revision'], 'context_tokens': config['context_tokens']})
        proxy = m['helper']('t_eval_proxy')
        gateway = stack.enter_context(proxy['Gateway'](config, journal, local_runtime))
        for previous_trial in list(journal.state['trials']):
            if previous_trial['status'] == 'interrupted' and 'usage' not in previous_trial:
                journal.finish_trial({**previous_trial, **gateway.totals(previous_trial['id'])})
        journal.checkpoint(phase='executing', status='running')
        def trial(key):
            task, mode, repetition = key
            if (journal.directory / 'cancel.json').exists():
                return
            trial_id = uuid.uuid4().hex
            directory = journal.directory / 'trials' / trial_id
            capability = gateway.lease(trial_id, mode)
            started = time.time()
            job = {'config': config, 'mode': mode, 'task': str(journal.directory / 'tasks' / task),
                   'benchmark_root': str(benchmark), 'directory': str(directory), 'codex_version': codex_version, 'native_codex': native,
                   'capability': capability, 'trial_id': trial_id}
            journal.start_trial({'id': trial_id, 'task': task, 'mode': mode, 'repetition': repetition})
            try:
                outcome = child_job(python, job, directory, journal)
                # Settle cancelled/in-flight requests before taking the usage snapshot.
                gateway.close_lease(capability['token'])
                outcome.update(id=trial_id, task=task, mode=mode, repetition=repetition,
                               elapsed_seconds=time.time()-started, **gateway.totals(trial_id))
                journal.finish_trial(outcome)
            finally:
                gateway.close_lease(capability['token'])
                for path in directory.rglob('*'):
                    if path.is_file() and not path.is_symlink() and path.suffix in ('.json', '.jsonl', '.txt', '.toml', '.log', '.md'):
                        value = path.read_text(errors='replace')
                        if capability['token'] in value:
                            path.write_text(value.replace(capability['token'], '[redacted-capability]'))
                # Capability material must not remain in persistent job artifacts.
                (directory / 'job.json').unlink(missing_ok=True)
        with ThreadPoolExecutor(max_workers=config['concurrency']) as executor:
            futures = [executor.submit(trial, key) for key in jobs]
            for future in as_completed(futures):
                future.result()
        if (journal.directory / 'cancel.json').exists():
            journal.checkpoint(status='cancelled', phase='stopped')
        else:
            latest = {(t['task'], t['mode'], t['repetition']): t for t in journal.state['trials']}
            incomplete = any(t.get('resolved') is None for t in latest.values()) or len(latest) < journal.state['total']
            journal.checkpoint(status='failed' if incomplete else 'completed', phase='finished')
