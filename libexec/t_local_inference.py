"""Local inference policy, pinned MLX artifacts and owned LM Studio model leases."""
from contextlib import contextmanager
import fcntl
import hashlib
import platform
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import signal
import statistics
import subprocess
import sys
import time
import textwrap
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']
DEFAULT_MODEL = 'mlx-community/Qwen3.8-27B-8bit'
MODES = ('disabled', 'manual', 'automatic', 'local-only')
DEFAULT = {'mode': 'manual', 'provider': 'lmstudio', 'model': DEFAULT_MODEL,
           'context_tokens': 32768, 'timeout_seconds': 300, 'queue_timeout_seconds': 300, 'on_cloud_failure': 'pause', 'cloud_retry_seconds': 300}
SUITES = ('swe-bench-verified-bash-only', 'swe-bench-pro', 'deepswe', 'gpqa', 'terminal-bench', 'livebench-coding')


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def policy(value=None):
    if value is None:
        return dict(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('unsupported inference policy fields')
    result = dict(DEFAULT, **value)
    if result['mode'] not in MODES or result['provider'] not in ('ollama', 'lmstudio'):
        raise ValueError('unsupported inference mode or provider')
    if result['on_cloud_failure'] not in ('pause', 'local', 'ask'):
        raise ValueError('on_cloud_failure must be pause, local or ask')
    if result['mode'] == 'disabled' and result['on_cloud_failure'] != 'pause':
        raise ValueError('disabled routing cannot continue locally on a cloud failure')
    name = result['model']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', name) or '..' in name or 'cloud' in name.lower() or '://' in name:
        raise ValueError('select an explicit local model artifact')
    if type(result['context_tokens']) is not int or not 4096 <= result['context_tokens'] <= 131072:
        raise ValueError('context_tokens must be 4096–131072')
    if type(result['timeout_seconds']) is not int or not 10 <= result['timeout_seconds'] <= 1800:
        raise ValueError('timeout_seconds must be 10–1800')
    if type(result['queue_timeout_seconds']) is not int or not 10 <= result['queue_timeout_seconds'] <= 1800:
        raise ValueError('queue_timeout_seconds must be 10–1800')
    if type(result['cloud_retry_seconds']) is not int or not 15 <= result['cloud_retry_seconds'] <= 300:
        raise ValueError('cloud_retry_seconds must be 15–300')
    return result


def effective(workspace):
    w = helper('t_workflow')
    doc, saved = w['load']()
    if not saved:
        return None  # Recommended defaults do not opt an unconfigured install into supervision.
    sync = helper('t_workflow_sync')
    scoped = w['tool_document'](doc, 'codex')
    selected = sync['launch_policy'](w, scoped, workspace)
    if selected is None and ('local-only' in json.dumps(scoped)):
        raise ValueError('Cannot resolve local-only workflow scope; refusing cloud fallback')
    if selected and not selected['enabled']:
        return None
    return policy(selected.get('inference')) if selected else None


def instructions(value):
    p = policy(value)
    if p['mode'] == 'disabled':
        return 'Local delegation is disabled in this workflow.'
    identity = f"{p['provider']} / {p['model']}"
    base = (f"Local inference: {identity}; context {p['context_tokens']}; task budget {p['timeout_seconds']} seconds; separate queue wait {p['queue_timeout_seconds']} seconds. "
            'Use the t-local-worker MCP start_task/task_status/cancel_task tools for local delegation. '
            'Announce Local requested with provider, model and reason before delegating. '
            'Say Local accepted only after start_task succeeds; a request is not proof of inference. '
            'Label returned findings Local result with the verified model, and label your own validation Lead checked. '
            'If busy or unavailable, say Local unavailable; lead continuing. '
            'The adapter snapshots the repository; review its patch and observed command evidence before applying. '
            'Do not claim the selected model ran without runtime evidence. ')
    if p['on_cloud_failure'] in ('local', 'ask') and p['mode'] != 'local-only':
        base += (f"Cloud connection failure policy: retry for up to {p['cloud_retry_seconds']} seconds, "
                 'or until native retries are exhausted. The native launch supervisor can stop its owned cloud runtime and '
                 'start a fresh local session from a bounded private checkpoint. Authentication and quota errors do not trigger this. '
                 'Provider changes require explicit user choice or the saved automatic fallback policy. ')
    if p['mode'] == 'local-only':
        return base + ('LOCAL ONLY: lead and every worker must use this exact local model. Never call cloud models or cloud subagents. '
                       'If local inference is unavailable, stop and report it; no cloud fallback. Ignore retained cloud model-stage preferences. '
                       'This is an inference policy, not an offline-tools policy.')
    if p['mode'] == 'manual':
        return base + 'Delegate locally only when the user requests it.'
    return base + ('AUTOMATIC HYBRID: before each meaningful subtask, assess scope and validation. '
                   'Automatically delegate bounded investigations, mechanical edits and small patches with concrete acceptance checks. '
                   'Keep architecture, ambiguous cross-system debugging and final acceptance in the lead. '
                   'Supply routing_reason, relevant paths and acceptance checks in start_task. '
                   'Poll while doing independent work; wait up to five seconds between polls when otherwise idle. '
                   'After delegating a patch, wait for its terminal result before editing the assigned source; do not duplicate its work. '
                   'If busy, unavailable, incorrect, failed or timed out, '
                   'use partial evidence and continue in the lead; explain escalation and do not repeat the same failed assignment. '
                   'Do not send the full lead transcript. This is task delegation, not per-token model switching.')


def trusted_connection():
    if not helper('t_local_worker')['settings']()['enabled']:
        return False
    path = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'config.toml'
    try:
        data = helper('t_toml')['loads'](path.read_text())
    except (OSError, ValueError):
        return False
    entry = data.get('mcp_servers', {}).get('t-local-worker', {})
    return (isinstance(entry.get('command'), str) and Path(entry['command']).resolve() == (ROOT/'bin/t').resolve()
            and entry.get('args') == ['mcp', '--local-worker'] and not entry.get('url') and entry.get('enabled', True))


def approval_args(value):
    """Only the three reviewed, snapshot-only worker operations are preapproved."""
    if policy(value)['mode'] != 'automatic':
        return []
    return [word for name in ('start_task', 'task_status', 'cancel_task')
            for word in ('-c', f'mcp_servers.t-local-worker.tools.{name}.approval_mode="approve"')]


def artifacts():
    return json.loads((ROOT / 'libexec/mlx-models.json').read_text())['models']


def artifact(name):
    row = next((r for r in artifacts() if r['model'] == name), None)
    if row is None:
        raise ValueError('unknown MLX artifact; select an exact model from t config models list')
    return dict(row)


def model_directory(name):
    artifact(name)
    return Path.home() / '.lmstudio/models' / name


def receipt(name):
    try:
        data = json.loads((model_directory(name) / '.t-artifact.json').read_text())
        if data.get('model') != name or not re.fullmatch('[0-9a-f]{40}', data.get('revision', '')):
            return {}
        for item in data.get('files', []):
            path = model_directory(name) / safe_file(item['name'])
            if not path.is_file() or path.stat().st_size != item['size']:
                return {}
        return data if data.get('files') else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def safe_file(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or name.startswith('.'):
        raise ValueError('unsafe artifact filename')
    return name


@_spinner("Contacting model runtime")
def request(url, data=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 't-local-inference'})
    # Local inference never passes through an inherited HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def report(row, machine, context=32768):
    result = dict(row, provider='lmstudio')
    ram = machine.get('ram_bytes')
    memory = int(row.get('resident_bytes', row['bytes']) * 1.2 + 4 * 1024**3 * context / 32768)
    capacity = ram * .8 if ram and 'Apple' in machine.get('accelerator', '') else None
    fit = memory <= capacity if capacity else None
    installed = receipt(row['model'])
    memory_fit = fit
    if row.get('minimum_ram_bytes') and ram and ram < row['minimum_ram_bytes']:
        memory_fit = False
    verified = installed.get('verified_context', 0) >= context
    if installed.get('verified_hardware') is not None or row.get('special_runtime'):
        verified = verified and installed.get('verified_hardware') == {
            k: machine.get(k) for k in ('ram_bytes', 'accelerator')}
    fit = memory_fit
    if row.get('special_runtime') and fit is True and not verified:
        fit = None
    result.update(context_tokens=context, memory_bytes=memory, fit=fit, installed=bool(installed),
                  ready=bool(verified and fit is True), verification_allowed=memory_fit is True,
                  revision=installed.get('revision'),
                  verification_error=installed.get('verification_error'),
                  reason='Runtime verification required for memory-mapped weights' if row.get('special_runtime') and fit is None else
                         'estimated fit' if fit else 'memory estimate exceeds budget' if fit is False else 'Apple unified memory required')
    result['disk_fit'] = bool(installed) or machine['free_disk_bytes'] >= row['bytes'] + 2 * 1024**3
    measure = installed.get('measurement')
    if measure:
        result['measurement'] = dict(measure, stale=(measure.get('revision') != installed.get('revision') or
            measure.get('context_tokens') != context or any(measure.get('hardware', {}).get(k) != machine.get(k) for k in ('ram_bytes', 'accelerator'))))
    return result


def lms():
    found = shutil.which('lms') or str(Path.home() / '.lmstudio/bin/lms')
    if not Path(found).is_file():
        raise ValueError('LM Studio runtime missing; install llmster from https://lmstudio.ai/docs/developer/core/headless')
    return found


@_spinner("Preparing local MLX runtime")
def run_lms(*args, timeout=300):
    try:
        return subprocess.run([lms(), *args], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError((exc.stderr or exc.stdout or str(exc))[-3000:]) from exc


@contextmanager
def runtime(name, context=32768, ttl=1800):
    info = receipt(name)
    if not info:
        raise ValueError('model is not completely downloaded; use t config models install')
    base = 'http://127.0.0.1:1234'
    owned_server = False
    alias = 't-local-' + re.sub('[^a-z0-9-]', '-', name.split('/')[-1].lower()) + '-' + uuid.uuid4().hex[:12]
    loaded = False
    try:
        try:
            request(base + '/v1/models')
        except OSError:
            run_lms('server', 'start', '--port', '1234', '--bind', '127.0.0.1')
            owned_server = True
        inventory = json.loads(run_lms('ls', '--json'))
        matches = [row for row in inventory if row.get('indexedModelIdentifier') == name and row.get('deviceIdentifier') is None]
        if len(matches) != 1 or sum(row.get('modelKey') == matches[0].get('modelKey') for row in inventory) != 1:
            raise ValueError('LM Studio cannot uniquely resolve this exact local artifact')
        expiry = ['--ttl', str(ttl)] if ttl is not None else []
        run_lms('load', matches[0]['modelKey'], '--identifier', alias, '--context-length', str(context), *expiry, '--yes')
        loaded = True
        active = json.loads(run_lms('ps', '--json'))
        observed = next((r for r in active if r.get('identifier') == alias), {})
        if observed.get('indexedModelIdentifier') != name or observed.get('deviceIdentifier') is not None or observed.get('contextLength') != context:
            raise ValueError('Loaded runtime identity or context does not match the selected local artifact')
        info = dict(info, runtime={k: observed.get(k) for k in ('modelKey', 'format', 'quantization', 'contextLength')})
        yield base, alias, info
    finally:
        try:
            if loaded:
                run_lms('unload', alias)
        finally:
            if owned_server:
                run_lms('server', 'stop')


def download_command(command, cancel=None, **kwargs):
    if cancel is None:
        return subprocess.run(command, check=True, **kwargs)
    if cancel.is_set():
        raise ValueError('Model action cancelled')
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    try:
        while process.poll() is None:
            if cancel.wait(.1):
                raise ValueError('Model action cancelled; partial files can be resumed')
        if process.returncode:
            raise ValueError('Model download failed; retry to resume partial files')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def install(name, download_only=False, context=32768, cancel=None):
    artifact(name)
    directory = model_directory(name)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.t-install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another install or test of this artifact is in progress') from None
        return install_locked(name, download_only, context, cancel)


def install_locked(name, download_only=False, context=32768, cancel=None):
    policy({'context_tokens': context})
    row = artifact(name)
    destination = model_directory(name)
    machine = helper('t_local_models')['hardware']()
    suitability = report(row, machine, context)
    if not suitability['disk_fit'] or (not download_only and not suitability.get('verification_allowed', suitability['fit'] is True)):
        raise ValueError(suitability['reason'] if suitability['disk_fit'] else 'insufficient disk space')
    if not download_only and not receipt(name):
        lms()  # Report a missing runtime before starting a large download.
    existing = receipt(name)
    if not existing:
        manifest = request('https://huggingface.co/api/models/' + name + '?blobs=true')
        revision = manifest['sha']
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise ValueError('invalid artifact revision')
        files = []
        for item in manifest['siblings']:
            filename = item['rfilename']
            if filename.startswith('.') or not filename.endswith(('.json', '.safetensors', '.model', '.txt', '.md', '.jinja')):
                continue
            safe_file(filename)
            files.append({'name': filename, 'size': item['size'], 'sha256': item.get('lfs', {}).get('sha256'), 'blob_sha1': item.get('blobId')})
        if not any(f['name'].endswith('.safetensors') for f in files):
            raise ValueError('artifact has no weights')
        destination.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(files):
            path = destination / item['name']
            path.parent.mkdir(parents=True, exist_ok=True)
            if cancel is None:
                print(f"{name}: file {i+1}/{len(files)} · {item['name']}", flush=True, file=sys.stderr)
            if not path.exists() or path.stat().st_size != item['size']:
                partial = path.with_name(path.name + '.partial')
                url = f'https://huggingface.co/{name}/resolve/{revision}/' + urllib.parse.quote(item['name'], safe='/')
                download_command(['curl', '-fL', '--retry', '3', '--connect-timeout', '30', '--continue-at', '-', '--progress-bar', '-o', str(partial), url], cancel)
                if partial.stat().st_size != item['size']:
                    raise ValueError('incomplete artifact file: ' + item['name'])
                partial.replace(path)
            expected = item['sha256'] or item.get('blob_sha1')
            if expected:
                with path.open('rb') as stream:
                    hasher = hashlib.sha256() if item['sha256'] else hashlib.sha1(f"blob {item['size']}\0".encode(), usedforsecurity=False)
                    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                        if cancel is not None and cancel.is_set():
                            raise ValueError('Model action cancelled')
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                if digest != expected:
                    path.unlink()
                    raise ValueError('artifact checksum mismatch: ' + item['name'])
        existing = {'model': name, 'revision': revision, 'files': files}
        (destination / '.t-artifact.json').write_text(json.dumps(existing, indent=2))
    if not download_only:
        # Contention and runtime outages do not erase prior valid inference evidence.
        existing.pop('verification_error', None)
        (destination / '.t-artifact.json').write_text(json.dumps(existing, indent=2))
        try:
            lms()
            with managed_runtime(name, 'lmstudio', context, 'verification', cancel=cancel) as (base, alias, _):
                answer = request(base + '/v1/responses', {'model': alias, 'input': 'Reply with OK.', 'max_output_tokens': 128}, timeout=2100)
                output = answer.get('output') if isinstance(answer, dict) else None
                if not isinstance(output, list) or not any(isinstance(item, dict) and item.get('type') == 'message' and
                           isinstance(item.get('content'), list) and
                           any(isinstance(part, dict) and part.get('type') == 'output_text' and
                               isinstance(part.get('text'), str) and part['text'].strip()
                               for part in item['content']) for item in output):
                    raise ValueError('local runtime returned no output text; readiness not verified')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            existing['verification_error'] = re.sub(r'[\x00-\x1f\x7f]', ' ', str(exc))[-600:]
            (destination / '.t-artifact.json').write_text(json.dumps(existing, indent=2))
            raise
        existing['verified_context'] = context
        existing['verified_hardware'] = {k: machine.get(k) for k in ('ram_bytes', 'accelerator')}
        (destination / '.t-artifact.json').write_text(json.dumps(existing, indent=2))
    return dict(model=name, downloaded=True, ready=report(row, machine, context)['ready'], revision=existing['revision'])


def measure(name, context=32768, cancel=None):
    policy({'context_tokens': context})
    suitability = report(artifact(name), helper('t_local_models')['hardware'](), context)
    if suitability['fit'] is not True:
        raise ValueError(suitability['reason'])
    readings = []
    with managed_runtime(name, 'lmstudio', context, 'measurement', cancel=cancel) as (base, alias, info):
        for iteration in range(4):
            start = time.monotonic()
            result = request(base + '/v1/chat/completions', {'model': alias, 'messages': [{'role': 'user', 'content':
                f'Case {iteration}: Write a Python binary search function with docstring and examples. Return code only.'}],
                'max_tokens': 256, 'temperature': 0}, timeout=2100)
            elapsed = time.monotonic() - start
            tokens = result.get('usage', {}).get('completion_tokens')
            if not isinstance(tokens, int) or tokens <= 0:
                raise ValueError('runtime did not report output token usage; no speed measurement saved')
            if iteration:
                readings.append(tokens / elapsed)
        measurement = {'metric': 'output tokens / request wall seconds (includes prefill)', 'median': statistics.median(readings),
                       'min': min(readings), 'max': max(readings), 'context_tokens': context, 'workload_version': 1,
                       'hardware': helper('t_local_models')['hardware'](), 'platform': platform.platform(),
                       'cli_version': run_lms('--version').strip(), 'runtime_inventory': run_lms('runtime', 'ls').strip(), 'measured_at': time.time(), 'revision': info['revision']}
    info['measurement'] = measurement
    (model_directory(name) / '.t-artifact.json').write_text(json.dumps(info, indent=2))
    return measurement


def measurement_path(name):
    root = Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state')) / 't/local-worker'
    return root / ('measurement-' + hashlib.sha256(name.encode()).hexdigest() + '.json')


def ollama_revision(name):
    match = re.fullmatch(r'([A-Za-z0-9._-]+):([A-Za-z0-9._-]+)', name)
    if match:
        try:
            path = Path.home()/'.ollama/models/manifests/registry.ollama.ai/library'/match[1]/match[2]
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    return None


def catalogue(suite='swe-bench-verified-bash-only', context=32768):
    policy({'context_tokens': context})
    if suite not in SUITES:
        raise ValueError('unknown benchmark suite')
    models = helper('t_local_models')
    machine = models['hardware']()
    rows = [report(row, machine, context) for row in artifacts()]
    legacy = models['report']()
    for row in legacy['models']:
        row = dict(row, provider='ollama', installed=models['missing_bytes'](row['model'], row['bytes']) == 0)
        row['context_tokens'] = context
        row['memory_bytes'] = int(row.get('memory_bytes', row['bytes'] * 1.2 + 4 * 1024**3) + 4 * 1024**3 * (context/32768 - 1))
        if context != 32768:
            ram, vram = machine.get('ram_bytes'), machine.get('vram_bytes')
            capacity = min(ram*.8, vram*.9) if ram and vram else None
            row['fit'] = row['memory_bytes'] <= capacity if capacity else None
            row['reason'] = 'estimated fit' if row['fit'] else 'memory estimate exceeds budget' if row['fit'] is False else 'accelerator capacity unknown'
        try:
            evidence = json.loads(measurement_path(row['model']).with_suffix('.ready.json').read_text())
            row['ready'] = bool(row['installed'] and evidence.get('revision') == ollama_revision(row['model']) and
                evidence.get('context_tokens', 0) >= context and
                all(evidence.get('hardware', {}).get(k) == machine.get(k) for k in ('ram_bytes', 'accelerator')))
        except (OSError, ValueError, TypeError):
            row['ready'] = False
        row['disk_fit'] = row.get('download_bytes', row['bytes']) <= machine['free_disk_bytes']
        row['benchmarks'] = ({'livebench-coding': {'score': row['score'], 'source': legacy['benchmark']['source'],
                              'version': legacy['benchmark']['release'], 'kind': 'base-model benchmark; local artifact not evaluated'}}
                             if row['score'] is not None else {})
        try:
            measured = json.loads(measurement_path(row['model']).read_text())
            if measured['model'] == row['model'] and all(isinstance(measured[k], (int, float)) for k in ('median', 'min', 'max')):
                row['measurement'] = dict(measured, stale=(not measured.get('revision') or measured['revision'] != ollama_revision(row['model']) or
                    measured.get('context_tokens') != context or any(measured.get('hardware', {}).get(k) != machine.get(k) for k in ('ram_bytes', 'accelerator'))))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        rows.append(row)
    for row in rows:
        row['benchmark'] = row.get('benchmarks', {}).get(suite)
    return {'hardware': machine, 'suite': suite, 'context_tokens': context, 'models': rows}


def detail_lines(row):
    lines = [row['model'], f"Provider: {row['provider']} · Precision: {row.get('precision', 'runtime metadata')}",
             f"Memory fit {'✓' if row['fit'] is True else '✕' if row['fit'] is False else '?'} · {row['reason']}",
             f"Download: {row['bytes']/1e9:.1f} GB · Runtime estimate: {row.get('memory_bytes', 0)/1e9:.1f} GB",
             f"Context capacity: {row.get('context_tokens', 32768)}; estimated runtime overhead and OS headroom included.",
             'Readiness: ' + ('Verified' if row.get('ready') else 'Downloaded; inference unverified' if row['installed'] else 'Not installed')]
    if row.get('revision'):
        lines.append('Artifact revision: ' + row['revision'])
    if row.get('source'):
        lines.append('Artifact source: ' + row['source'])
    lines.append('Publisher benchmarks · original models; local quantization not evaluated:')
    for suite, score in row.get('benchmarks', {}).items():
        lines.extend([f"  {suite}: {score['score']} · {score['version']}", '  ' + score.get('harness', score['kind']), '  ' + score['source']])
    if not row.get('benchmarks'):
        lines.append('  No verified results in this catalog.')
    measure = row.get('measurement')
    if measure:
        lines.extend([f"Measured speed: {measure['median']:.1f} · {measure['metric']}",
                      f"Range: {measure['min']:.1f}–{measure['max']:.1f}; context {measure['context_tokens']}",
                      'Stale measurement; rerun measure.' if measure.get('stale') else 'Measured on this machine; workload-specific, not a quality score.'])
    return lines


def model_status(row):
    if row.get('ready'):
        return 'Ready'
    if row.get('verification_error'):
        return 'Verification failed'
    return 'Downloaded · not verified' if row['installed'] else 'Not downloaded'


def model_page(ui, title, lines):
    width = max(1, ui.cols() - 6) if hasattr(ui, 'cols') else 74
    rail = getattr(ui, 'rail', '  │')
    body = [rail + '   ' + part for line in lines
            for part in (textwrap.wrap(line, width=width) or [''])]
    result = ui.page(title, body, accept=('enter', 'q', 'esc', 'abort'), keys='enter/q back')
    if result == 'abort':
        raise KeyboardInterrupt


def model_menu(ui, row, context, use_label='Use in workflow'):
    """One compact overview; provenance is available without blocking actions."""
    focus = 'use' if row.get('ready') else 'install'
    while True:
        disabled = {}
        if not row.get('ready'):
            disabled['use'] = disabled['measure'] = 'Verify readiness first.'
        if not row.get('verification_allowed', row['fit'] is True):
            disabled['install'] = row['reason']
        if not row.get('disk_fit', True):
            disabled['download'] = disabled['install'] = 'Insufficient disk space'
        actions = [('use', use_label)] if row.get('ready') else []
        actions += [('install', 'Test model' if row['installed'] else 'Install and test')]
        if not row['installed']:
            actions.append(('download', 'Download only'))
        if row.get('ready'):
            actions.append(('measure', 'Measure speed'))
        actions += [('unload', 'Unload model'), ('guide', 'CLI guide'), ('app', 'Open desktop app'),
                    ('benchmarks', 'Benchmarks ›'), ('artifact', 'Artifact & runtime ›'), ('back', 'Back')]
        view = helper('t_local_manager')['status']()
        instances = [x for x in view.get('models', []) if x['model'] == row['model'] and x.get('owned')]
        if not instances:
            disabled['unload'] = 'This model has no instance loaded by t.'
        elif any(x.get('active') or x.get('pinned') for x in instances):
            disabled['unload'] = 'Finish the active response and conversation before unloading.'
        summary = [('Model', model_name(row)),
                   ('Runtime', f"{row['provider']} · {row.get('precision', 'runtime metadata')}"),
                   ('Files', f"{row['bytes']/1e9:.1f} GB · {'On disk' if row['installed'] else 'To download'}"),
                   ('Context', f'{context:,} tokens'), ('Status', model_status(row)),
                   ('Memory fit', '✓' if row['fit'] is True else '✕' if row['fit'] is False else '?')]
        action = ui.pick('Model tools', actions, context=summary,
                         default=next((i for i, (key, _) in enumerate(actions) if key == focus), 0),
                         sections={'benchmarks': 'Details'}, disabled=disabled,
                         accents={'use': 'g', 'install': 'g'})
        if action in (None, 'back'):
            return None
        focus = action
        if action == 'guide':
            helper('t_local_runtime_ui')['provider_menu'](ui, row['provider'])
            continue
        if action == 'app':
            try:
                helper('t_local_apps')['open_desktop'](row['provider'])
                model_page(ui, 'Desktop app opened', ['Use t’s monitor for the exact runtime serving your sessions.'])
            except ValueError as exc:
                model_page(ui, 'Could not open desktop app', [str(exc)])
            continue
        if action == 'unload':
            try:
                for instance in instances:
                    helper('t_local_manager')['rpc']('release', alias=instance['alias'])
            except (OSError, ValueError) as exc:
                model_page(ui, 'Unload unavailable', [str(exc)])
            continue
        if action == 'use':
            return {'model': row['model'], 'provider': row['provider']}
        if action == 'benchmarks':
            scores = row.get('benchmarks', {})
            lines = ['Original model; not local scores.'] if scores else ['No published results in this catalog.']
            choice = ui.pick('Publisher benchmarks', [(suite, suite) for suite in scores] +
                             [('sources', 'Sources & methodology ›'), ('back', 'Back')], context=lines,
                             values={suite: str(score['score']) for suite, score in scores.items()})
            if choice == 'sources' or choice in scores:
                lines = ['Original model results; local quantization not evaluated.'] if scores else []
                for suite, score in scores.items():
                    if choice != 'sources' and choice != suite:
                        continue
                    lines += [f"{suite}: {score['score']} · {score['version']}", score.get('harness', score['kind']), score['source'], '']
                model_page(ui, 'Sources & methodology', lines or ['No published sources in this catalog.'])
            continue
        if action == 'artifact':
            lines = detail_lines(row)
            start = next(i for i, line in enumerate(lines) if line.startswith('Publisher benchmarks'))
            lines = lines[:start]
            if row.get('memory_note'):
                lines += [row['memory_note'], 'The installed runtime must confirm support; its memory guardrails remain active.']
            if row.get('verification_error'):
                lines += ['Last verification failed:', row['verification_error'], 'Update the LM Studio runtime or choose another model, then retry.']
            if row.get('measurement'):
                lines += detail_lines(row)[-3:]
            model_page(ui, 'Artifact & runtime', lines)
            continue
        if action != 'measure':
            review = ([f"Reuse downloaded files · {row['bytes']/1e9:.1f} GB"] if row['installed'] else
                      [f"Download up to {row['bytes']/1e9:.1f} GB",
                       str(model_directory(row['model']) if row['provider'] == 'lmstudio' else Path.home()/'.ollama/models')])
            if action == 'install':
                review = [row['model'].split('/')[-1], row['provider'], '',
                          'Check       Load model and generate a short reply',
                          'Wait        Up to 5 minutes for a turn', f'Context     {context:,} tokens',
                          '', 'May unload idle models loaded by t.']
            if ui.pick('Test model' if row['installed'] else 'Review download',
                       [('yes', 'Start test' if row['installed'] else 'Continue'), ('no', 'Cancel')], context=review) != 'yes':
                continue
        name = row['model']
        def operation(cancel):
            return ((measure(name, context, cancel=cancel) if action == 'measure' else
                     install(name, action == 'download', context, cancel=cancel)) if row['provider'] == 'lmstudio' else
                    ollama_action(name, action, action == 'download', context, cancel=cancel))
        helper('t_local_runtime_ui')['run_model_action'](ui, operation, name)
        row = next(r for r in catalogue(context=context)['models'] if r['model'] == row['model'])


BENCHMARK_LABELS = {'swe-bench-pro': 'SWE Pro', 'deepswe': 'DeepSWE',
                    'terminal-bench': 'Terminal', 'gpqa': 'GPQA',
                    'swe-bench-verified-bash-only': 'SWE Verified', 'livebench-coding': 'LiveBench'}


def model_name(row):
    name = row.get('base_model', row['model']).split('/')[-1]
    return re.sub(r'(?i)(?:-MLX)?-\d+bit$', '', name).replace('-', ' ')


def selection(row):
    return {'provider': row['provider'], 'model': row['model']}


def preparation_block(row):
    if not row.get('verification_allowed', row.get('fit') is True):
        return row.get('reason', 'Memory or runtime compatibility is unknown.')
    if not row['installed'] and not row.get('disk_fit', True):
        return 'Insufficient disk space for the download.'
    return None


def prepare_model(ui, row, context):
    """One explicit preparation; never change selection on failure or cancellation."""
    error = row.get('verification_error', '')
    while True:
        blocked = preparation_block(row)
        if blocked:
            ui.pick('Model unavailable', [('back', 'Choose another model')],
                    context=[('Model', model_name(row)), ('Reason', blocked),
                             ('Files', 'Retained on disk' if row['installed'] else 'Not downloaded')])
            return None
        summary = [('Model', model_name(row)), ('Provider', row['provider']),
                   ('Files', f"{row['bytes']/1e9:.1f} GB · " + ('On disk · Reuse files' if row['installed'] else 'To download')),
                   ('Context', f'{context:,} tokens')]
        if not row['installed']:
            summary.append(('Location', str(model_directory(row['model']) if row['provider'] == 'lmstudio' else Path.home()/'.ollama/models')))
        if error:
            summary.append(('Last check', error))
        action = 'Verify & use' if row['installed'] else 'Download, verify & use'
        if ui.pick('Prepare local model', [('prepare', action), ('back', 'Cancel · Keep current model')],
                   context=summary, accents={'prepare': 'g'}) != 'prepare':
            return None
        ui.restore()
        try:
            with helper('t_local_worker')['reservation']():
                if row['provider'] == 'lmstudio':
                    install(row['model'], False, context)
                else:
                    ollama_action(row['model'], 'install', context=context)
            error = ''
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc)
        finally:
            ui.raw()
        try:
            refreshed = next((r for r in catalogue(context=context)['models']
                              if selection(r) == selection(row)), None)
            if refreshed:
                row = refreshed
            else:
                error = error or 'Model no longer appears in the catalogue.'
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = error or str(exc)
        if not error and row.get('ready'):
            return selection(row)
        error = error or 'Readiness was not confirmed. Files retained; current model unchanged.'


def benchmark_source(ui, row, suite):
    score = row.get('benchmarks', {}).get(suite)
    if score is None:
        lines = ['No published result recorded for this model.']
    else:
        lines = [f"{model_name(row)} · {BENCHMARK_LABELS.get(suite, suite)}: {score['score']}",
                 'Publisher original-model result; not this local quantization.',
                 score.get('version', ''), score.get('harness', score.get('kind', '')), score.get('source', '')]
    model_page(ui, 'Benchmark source', lines)


def select_model(ui, current=DEFAULT_MODEL, context=32768, provider=None, *, use_label='Use in workflow'):
    focus = None
    while True:
        try:
            view = catalogue(context=context)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if ui.pick('Local models unavailable', [('retry', 'Scan again'), ('back', 'Back')], context=[str(exc)]) != 'retry':
                return None
            continue
        models = sorted(view['models'], key=lambda r: (bool(preparation_block(r)), not r.get('ready'), not r['installed']))
        # Model identifiers can collide across providers; retain exact identity.
        candidates = {(r['provider']+'::'+r['model'] if sum(v['model'] == r['model'] for v in models) > 1 else r['model']): r for r in models}
        configured = next((key for key, r in candidates.items() if r['model'] == current and (provider is None or r['provider'] == provider)), None)
        if focus not in candidates:
            focus = configured
        suites = [suite for suite in BENCHMARK_LABELS if any(suite in r.get('benchmarks', {}) for r in models)]
        columns = [f'{i+1}:{BENCHMARK_LABELS[suite]}' for i, suite in enumerate(suites)] + ['Readiness']
        cells = {key: [str(r.get('benchmarks', {}).get(suite, {}).get('score', '—')) for suite in suites] +
                 [model_status(r) if not preparation_block(r) else 'Unavailable'] for key, r in candidates.items()}
        notes = {key: r['provider']+' · '+r.get('precision', 'runtime precision')+(' · ✓ Configured' if key == configured else '') for key, r in candidates.items()}
        def details(key):
            r = candidates[key]
            action = 'View compatibility' if preparation_block(r) else 'Use this model' if r.get('ready') else 'Verify & use' if r['installed'] else 'Download, verify & use'
            return [f"Files {r['bytes']/1e9:.1f} GB · "+('On disk' if r['installed'] else 'Download required'),
                    f"Context {context:,} tokens · Enter → {action}"]
        rows = [('', 'Use default model')] + [(key, model_name(r)) for key, r in candidates.items()] + [('__refresh', 'Scan again'), ('__back', 'Back')]
        intro = ['Publisher scores · Original models, not local quantizations.']
        if not models:
            intro += ['No local models found.']
        elif configured is None:
            intro += ['Configured model unavailable · Choose a replacement.']
        choice = ui.pick('Choose a local model', rows, context=intro,
                         values={'': DEFAULT_MODEL.split('/')[-1]},
                         columns=columns, cells=cells, notes=notes, details=details,
                         sections={next(iter(candidates), '__refresh'): 'Compare models', '__refresh': 'Manage'},
                         default=next((i for i, r in enumerate(rows) if r[0] == focus), 0),
                         focused_shortcuts={**{str(i+1): suite for i, suite in enumerate(suites)}, 'm': 'tools'})
        if choice in (None, '__back'):
            return None
        if choice == '':
            return {'provider': DEFAULT['provider'], 'model': DEFAULT_MODEL}
        if choice == '__refresh':
            continue
        if isinstance(choice, tuple):
            action, focus = choice
            if action == 'tools':
                result = model_menu(ui, candidates[focus], context)
                if result:
                    return result
            else:
                benchmark_source(ui, candidates[focus], action)
            continue
        focus = choice
        row = candidates[choice]
        if row.get('ready') and not preparation_block(row):
            return selection(row)
        result = prepare_model(ui, row, context)
        if result:
            return result


def retry_label(seconds):
    return f'Up to {seconds // 60} minute' + ('s' if seconds != 60 else '') if seconds % 60 == 0 else f'Up to {seconds} seconds'


def cloud_failure_menu(ui, current):
    """Edit the shared staged policy; readiness operations remain explicit."""
    focus = 'cloud_retry_seconds'
    while True:
        rows = [('cloud_retry_seconds', 'Retry cloud for'), ('on_cloud_failure', 'Then')]
        values = {'cloud_retry_seconds': retry_label(current['cloud_retry_seconds']),
                  'on_cloud_failure': {'local': 'Continue locally', 'ask': 'Ask which local model', 'pause': 'Pause'}[current['on_cloud_failure']]}
        for key in values:
            if current[key] == DEFAULT[key]:
                values[key] = 'Default · ' + values[key]
        sections = {'cloud_retry_seconds': 'Cloud retries', 'reset': 'Manage'}
        selected = None
        discovery_failed = False
        if current['on_cloud_failure'] == 'local':
            rows += [('model', 'Local model ›'), ('readiness', 'Readiness ›')]
            sections['model'] = 'Local fallback'
            values['model'] = current['model'].split('/')[-1]
            try:
                selected = next((r for r in catalogue(context=current['context_tokens'])['models']
                                 if r['model'] == current['model'] and r['provider'] == current['provider']), None)
                values['readiness'] = model_status(selected) if selected else 'Model unavailable · Choose a model'
            except (OSError, ValueError, subprocess.SubprocessError):
                discovery_failed = True
                values['readiness'] = 'Status unavailable · Try again'
        rows += [('reset', 'Reset to defaults'), ('back', 'Back')]
        values['reset'] = 'Already at defaults' if all(current[k] == DEFAULT[k] for k in ('cloud_retry_seconds', 'on_cloud_failure')) else 'Cloud recovery settings'
        choice = ui.pick('Cloud connection failure', rows, values=values, sections=sections,
                         default=next((i for i, r in enumerate(rows) if r[0] == focus), 0))
        if choice in (None, 'back'):
            return
        focus = choice
        if choice == 'reset':
            for key in ('cloud_retry_seconds', 'on_cloud_failure'):
                current[key] = DEFAULT[key]
        elif choice == 'cloud_retry_seconds':
            value = ui.pick('Retry cloud for', [('', 'Use default · Up to 5 minutes')] +
                            [(str(v), retry_label(v)) for v in (15, 30, 60, 120, 300)])
            if value is not None:
                current[choice] = int(value) if value else DEFAULT[choice]
        elif choice == 'on_cloud_failure':
            value = ui.pick('Then', [('', 'Use default · Pause'), ('pause', 'Pause'), ('ask', 'Ask which local model'), ('local', 'Continue locally')],
                            disabled={'local': 'Enable local routing first.', 'ask': 'Enable local routing first.'} if current['mode'] == 'disabled' else {})
            if value is not None:
                current[choice] = value or DEFAULT[choice]
        elif choice in ('model', 'readiness'):
            if discovery_failed:
                continue
            result = model_menu(ui, selected, current['context_tokens']) if choice == 'readiness' and selected else select_model(ui, current['model'], current['context_tokens'], current['provider'])
            if result:
                current.update(result)


def workflow_menu(ui, value=None):
    current, focus = policy(value), 'mode'
    try:
        while True:
            rows = [('mode', 'Routing'), ('model', 'Local model'),
                    ('on_cloud_failure', 'Cloud connection failure ›'),
                    ('context_tokens', 'Context'), ('timeout_seconds', 'Task budget'),
                    ('queue_timeout_seconds', 'Queue wait'),
                    ('reset', 'Reset to defaults'), ('back', 'Back')]
            choice = ui.pick('Inference & routing', rows,
                            default=next(i for i, row in enumerate(rows) if row[0] == focus),
                            sections={'on_cloud_failure': 'Connection recovery', 'context_tokens': 'Local inference', 'reset': 'Manage'},
                            disabled={'on_cloud_failure': 'Local-only workflow · No cloud connection'} if current['mode'] == 'local-only' else {},
                            values={**{k: str(v) for k, v in current.items()}, 'on_cloud_failure':
                                    retry_label(current['cloud_retry_seconds']) + ' → ' + {'local': 'Continue locally', 'ask': 'Ask which local model', 'pause': 'Pause'}[current['on_cloud_failure']],
                                    'reset': 'Already at defaults' if current == DEFAULT else 'Routing settings'})
            if choice in (None, 'back'):
                return current
            focus = choice
            if choice == 'reset':
                current = policy()
            elif choice == 'model':
                selection = select_model(ui, current['model'], current['context_tokens'], current['provider'])
                if selection:
                    current.update(selection)
            elif choice == 'mode':
                selected = ui.pick('Routing', [('', 'Use default · Manual delegation')] + [(v, {
                    'automatic': 'Automatic hybrid · approve local worker tools', 'local-only': 'Local only · no cloud fallback',
                    'manual': 'Manual delegation', 'disabled': 'Disabled'}[v]) for v in MODES])
                if selected is not None:
                    current['mode'] = selected or DEFAULT['mode']
                    if current['mode'] == 'disabled':
                        current['on_cloud_failure'] = 'pause'
            elif choice == 'on_cloud_failure':
                cloud_failure_menu(ui, current)
            else:
                values = (8192, 16384, 32768, 65536) if choice == 'context_tokens' else (60, 300, 900, 1800)
                selected = ui.pick(choice.replace('_', ' ').title(), [('', 'Use default · ' + str(DEFAULT[choice]))] +
                                   [(str(v), str(v)) for v in values])
                if selected is not None:
                    current[choice] = int(selected) if selected else DEFAULT[choice]
    except KeyboardInterrupt as exc:
        if current != policy(value):
            exc.workflow_inference = current
        raise


def ollama_action(name, action, download_only=False, context=32768, cancel=None):
    w = helper('t_local_worker')
    if action in ('install', 'download') and not ollama_revision(name):
        with w['reservation'](), w['ollama_server'](context) as (_, env):
            download_command([w['executable']('ollama'), 'pull', name], cancel, env=env)
    if download_only or action == 'download':
        return {'model': name, 'provider': 'ollama', 'downloaded': True, 'ready': False}
    with managed_runtime(name, 'ollama', context, 'measurement' if action == 'measure' else 'verification', cancel=cancel) as (base, _, info):
        if action != 'measure':
            value = request(base + '/api/generate', {'model': name, 'stream': False,
                            'prompt': 'Reply with OK.', 'options': {'num_ctx': context, 'num_predict': 128}}, timeout=2100)
            if not isinstance(value.get('response'), str) or not value['response'].strip():
                raise ValueError('Runtime returned no output text; readiness not verified')
            evidence = {'revision': ollama_revision(name), 'context_tokens': context,
                        'hardware': helper('t_local_models')['hardware']()}
            path = measurement_path(name).with_suffix('.ready.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence))
            return {'model': name, 'provider': 'ollama', 'downloaded': True, 'ready': True}
        readings = []
        for iteration in range(4):
            value = request(base + '/api/generate', {'model': name, 'stream': False,
                'prompt': f'Case {iteration}: Write a Python binary search function with docstring and examples.',
                'options': {'num_ctx': context, 'num_predict': 256, 'temperature': 0}}, timeout=2100)
            tokens, duration = value.get('eval_count', 0), value.get('eval_duration', 0)
            if not tokens or not duration:
                raise ValueError('runtime returned no measured decode timing')
            if iteration:
                readings.append(tokens / (duration / 1e9))
        measurement = {'metric': 'decode tokens / second', 'median': statistics.median(readings),
            'min': min(readings), 'max': max(readings), 'measured_at': time.time(), 'context_tokens': context,
            'model': name, 'revision': ollama_revision(name), 'hardware': helper('t_local_models')['hardware'](), 'workload_version': 1}
        path = measurement_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(measurement, indent=2))
        return measurement


def command(args):
    action = args.models_action
    context = getattr(args, 'context', 32768)
    policy({'context_tokens': context})
    provider = getattr(args, 'provider', None)
    name = getattr(args, 'model_name', None)
    suite = getattr(args, 'suite', None) or ('livebench-coding' if getattr(args, 'benchmark', None) else 'swe-bench-verified-bash-only')
    if action in ('list', 'refresh'):
        if action == 'refresh':
            helper('t_local_models')['refresh']()
        if getattr(args, 'benchmark', None) and suite != 'livebench-coding':
            raise ValueError('--benchmark DATE requires --suite livebench-coding')
        view = catalogue(suite, context)
        if getattr(args, 'benchmark', None):
            legacy = helper('t_local_models')['report'](benchmark=args.benchmark)
            scores = {r['model']: r for r in legacy['models']}
            for row in view['models']:
                score = scores.get(row['model'], {}).get('score')
                row['benchmark'] = dict(legacy['benchmark'], score=score) if score is not None else None
        if provider:
            view['models'] = [r for r in view['models'] if r['provider'] == provider]
        if getattr(args, 'json', False):
            print(json.dumps(view, indent=2))
        else:
            print(f"Local models · {view['hardware']['accelerator']} · Context {view['context_tokens']}\nBenchmark: {suite}")
            print('MODEL · SCORE · MEMORY FIT · INSTALLED')
            for row in view['models']:
                print(f"{row['model']} · {row['benchmark']['score'] if row['benchmark'] else '—'} · "
                      f"{'✓' if row['fit'] is True else '✕' if row['fit'] is False else '?'} · {'yes' if row['installed'] else 'no'}")
            print('Publisher scores use different harnesses; inspect model details. Local quantization is not evaluated.')
        return 0
    provider = provider or ('lmstudio' if any(r['model'] == name for r in artifacts()) else 'ollama')
    row = next((r for r in catalogue(context=context)['models'] if r['model'] == name and r['provider'] == provider), None)
    if row is None:
        raise ValueError('unknown artifact; use t config models list')
    if action == 'show':
        result = row
    else:
        if action == 'install' and not getattr(args, 'yes', False):
            if not sys.stdin.isatty():
                raise ValueError('use --yes with an explicit model for noninteractive installation')
            api = runpy.run_path(str(ROOT / 'bin/t'))
            dialog = helper('t_onboarding')['Dialog'](api)
            try:
                if not dialog.review([f"Download {name} (up to {row['bytes']/1e9:.1f} GB) using {provider}.",
                                      'Download only; inference unverified.' if getattr(args, 'download_only', False) else 'Load the artifact and verify local inference.']):
                    return 130
            finally:
                dialog.close()
        result = (measure(name, context) if action == 'measure' else install(name, getattr(args, 'download_only', False), context)) if provider == 'lmstudio' else ollama_action(name, action, getattr(args, 'download_only', False), context)
    if action == 'show' and not getattr(args, 'json', False):
        print('\n'.join(detail_lines(result)))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


@contextmanager
def managed_runtime(model, provider, context=32768, kind='session', activity=None, chosen=None, cancel=None):
    if cancel is not None and cancel.is_set():
        raise ValueError('Model action cancelled')
    selected = policy(chosen or {'model': model, 'provider': provider, 'context_tokens': context})
    with helper('t_local_manager')['Client'](selected, kind, activity, cancel) as client:
        info = dict(receipt(model)) if provider == 'lmstudio' else {}
        yield client.base, model, info
        if client.error:
            raise ValueError(client.error)


@contextmanager
def provider_runtime(chosen):
    with managed_runtime(chosen['model'], chosen['provider'], chosen['context_tokens'], chosen=chosen) as value:
        yield value


def trusted_workspace(cwd):
    """Carry only an existing explicit trust decision for this exact workspace."""
    try:
        config = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'config.toml'
        projects = helper('t_toml')['loads'](config.read_text()).get('projects', {})
        return any(Path(path).is_absolute() and Path(path).resolve() == Path(cwd).resolve()
                   and isinstance(value, dict) and value.get('trust_level') == 'trusted'
                   for path, value in projects.items())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def lead(argv, cwd=None, chosen=None, runner=None):
    """Dedicated native CLI launch: no cloud auth, models or providers inherited."""
    chosen = policy(chosen) if chosen is not None else effective(cwd or os.getcwd())
    if not chosen or chosen['mode'] != 'local-only':
        if chosen:
            offline = helper('t_offline')
            try:
                offline['launch_options'](argv, cwd or os.getcwd())
            except ValueError:
                if chosen['on_cloud_failure'] in ('local', 'ask'):
                    raise
                print('Cloud retry limit unavailable for this invocation; using native tool retries.', file=sys.stderr)
            else:
                return offline['supervise'](argv, chosen, cwd or os.getcwd())
        return subprocess.call(argv)
    if '--approve-for-me' in argv[1:]:
        raise ValueError('Local-only inference requires user approval review; automatic cloud review is unavailable')
    if 'resume' in argv[1:]:
        raise ValueError('Local-only provider changes require a new session; cloud/local resume is not verified')
    if chosen['provider'] == 'lmstudio':
        fit = report(artifact(chosen['model']), helper('t_local_models')['hardware'](), chosen['context_tokens'])
    else:
        fit = next((r for r in catalogue(context=chosen['context_tokens'])['models'] if r['model'] == chosen['model'] and r['provider'] == 'ollama'), None)
        if fit is None:
            raise ValueError('Choose an installed model from the catalog')
    if fit['fit'] is not True:
        raise ValueError(fit['reason'])
    w = helper('t_local_worker')
    with helper('t_local_status')['Activity'](chosen['model']) as activity, helper('t_local_manager')['Client'](chosen, 'session', activity) as client:
        base, alias = client.base, chosen['model']
        home = w['state_root']() / ('lead-' + uuid.uuid4().hex)
        env = w['environment'](home)
        env.update(CODEX_OSS_BASE_URL=base + '/v1')
        codex_home = Path(env['CODEX_HOME'])
        if trusted_workspace(cwd or os.getcwd()):
            (codex_home/'config.toml').write_text('[projects.'+json.dumps(str(Path(cwd or os.getcwd()).resolve()))+']\ntrust_level = "trusted"\n')
        workflow = helper('t_workflow')
        document, _ = workflow['load']()
        (codex_home / 'AGENTS.md').write_text(workflow['render'](document) + '\n' + instructions(chosen) + '\nLocal requests take fair turns with other sessions; do not bypass the managed endpoint.\n')
        # Keep native sandbox/approval switches; strip incompatible cloud model/effort/speed defaults.
        filtered = []
        args = iter(argv[1:])
        for arg in args:
            if arg == '--':
                filtered.extend([arg, *args])
                break
            if arg in ('--model', '-m', '--profile', '-p'):
                next(args, None)
            elif arg in ('-c', '--config'):
                value = next(args, '')
                if value.split('=', 1)[0] in ('sandbox_mode', 'approval_policy'):
                    filtered.extend([arg, value])
            elif arg in ('--enable', '--disable', '--local-provider'):
                next(args, None)
            elif arg == '--oss':
                continue
            elif arg.startswith(('--model=', '--profile=', '--config=', '-c', '-m', '-p', '--enable=', '--disable=', '--local-provider=')):
                continue
            else:
                filtered.append(arg)
        forced = ['--oss', '--local-provider', chosen['provider'], '--model', alias, '-c', 'features.multi_agent=false',
                  '-c', 'features.fast_mode=false', '-c', 'approvals_reviewer="user"', '-c', 'mcp_servers={}', '-c', 'web_search="disabled"',
                  '-c', f'model_context_window={chosen["context_tokens"]}',
                  '-c', f'model_auto_compact_token_limit={int(chosen["context_tokens"]*.75)}']
        print(f"Local only · {chosen['model']} · {chosen['provider']} · shared request queue", file=sys.stderr)
        env.update(CODEX_OSS_BASE_URL=base + '/v1', OLLAMA_HOST=base)
        if runner is not None:
            result = runner([argv[0], *forced, *filtered], env=env)
            if client.error:
                raise ValueError(client.error)
            return result
        proc = subprocess.Popen([argv[0], *forced, *filtered], env=env)
        try:
            while proc.poll() is None:
                if client.error:
                    proc.terminate()
                    raise ValueError(client.error)
                time.sleep(.2)
            return proc.returncode
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()



if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['--launch-prefix']:
            try:
                selected = effective(sys.argv[2])
                required = selected is not None
            except (OSError, ValueError):
                required = True  # Launch guard reports the invalid policy; never fall back.
            if required:
                print('python3\n' + str(Path(__file__).resolve()) + '\n--lead')
            raise SystemExit(0)
        if sys.argv[1:2] == ['--lead']:
            def interrupted(signum, frame):
                raise KeyboardInterrupt
            for signum in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, interrupted)
            raise SystemExit(lead(sys.argv[2:]))
        raise SystemExit('use t config models or the workflow launcher')
    except KeyboardInterrupt:
        print('Local inference cancelled.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('Local inference unavailable: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
