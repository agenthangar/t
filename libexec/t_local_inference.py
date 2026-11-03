"""Local inference policy, pinned MLX artifacts and owned LM Studio model leases."""
from contextlib import contextmanager
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
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']
DEFAULT_MODEL = 'mlx-community/Qwen3.8-27B-8bit'
MODES = ('disabled', 'manual', 'automatic', 'local-only')
DEFAULT = {'mode': 'manual', 'provider': 'lmstudio', 'model': DEFAULT_MODEL,
           'context_tokens': 32768, 'timeout_seconds': 300}
SUITES = ('swe-bench-verified-bash-only', 'swe-bench-pro', 'deepswe', 'gpqa', 'terminal-bench', 'livebench-coding')


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def policy(value=None):
    if value is None:
        return dict(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('inference requires mode, provider, model, context_tokens and timeout_seconds')
    result = dict(DEFAULT, **value)
    if result['mode'] not in MODES or result['provider'] not in ('ollama', 'lmstudio'):
        raise ValueError('unsupported inference mode or provider')
    name = result['model']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', name) or '..' in name or 'cloud' in name.lower() or '://' in name:
        raise ValueError('select an explicit local model artifact')
    if type(result['context_tokens']) is not int or not 4096 <= result['context_tokens'] <= 131072:
        raise ValueError('context_tokens must be 4096–131072')
    if type(result['timeout_seconds']) is not int or not 10 <= result['timeout_seconds'] <= 1800:
        raise ValueError('timeout_seconds must be 10–1800')
    return result


def effective(workspace):
    w = helper('t_workflow')
    doc, _ = w['load']()
    sync = helper('t_workflow_sync')
    scoped = w['tool_document'](doc, 'codex')
    selected = sync['launch_policy'](w, scoped, workspace)
    if selected is None and ('local-only' in json.dumps(scoped)):
        raise ValueError('Cannot resolve local-only workflow scope; refusing cloud fallback')
    if selected and not selected['enabled']:
        return dict(DEFAULT, mode='disabled')
    return policy(selected['inference']) if selected and 'inference' in selected else None


def instructions(value):
    p = policy(value)
    if p['mode'] == 'disabled':
        return 'Local delegation is disabled in this workflow.'
    identity = f"{p['provider']} / {p['model']}"
    base = (f"Local inference: {identity}; context {p['context_tokens']}; task budget {p['timeout_seconds']} seconds. "
            'Use the t-local-worker MCP start_task/task_status/cancel_task tools for local delegation. '
            'Announce provider, model and reason before delegating and report the result or escalation. '
            'The adapter snapshots the repository; review its patch and observed command evidence before applying. '
            'Do not claim the selected model ran without runtime evidence. ')
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
    memory = int(row['bytes'] * 1.2 + 4 * 1024**3 * context / 32768)
    capacity = ram * .8 if ram and 'Apple' in machine.get('accelerator', '') else None
    fit = memory <= capacity if capacity else None
    if row.get('special_runtime'):
        fit = None
    installed = receipt(row['model'])
    result.update(context_tokens=context, memory_bytes=memory, fit=fit, installed=bool(installed),
                  ready=bool(installed.get('verified_context', 0) >= context and fit is True),
                  revision=installed.get('revision'),
                  reason='special runtime and memory mapping unverified' if row.get('special_runtime') else
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
    if artifact(name).get('special_runtime'):
        raise ValueError('Flash-Next runtime and memory mapping are unverified; artifact downloaded only')
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


def install(name, download_only=False, context=32768):
    policy({'context_tokens': context})
    row = artifact(name)
    destination = model_directory(name)
    machine = helper('t_local_models')['hardware']()
    suitability = report(row, machine, context)
    if not suitability['disk_fit'] or (not download_only and suitability['fit'] is not True):
        raise ValueError(suitability['reason'] if suitability['disk_fit'] else 'insufficient disk space')
    if not download_only:
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
            print(f"{name}: file {i+1}/{len(files)} · {item['name']}", flush=True, file=sys.stderr)
            if not path.exists() or path.stat().st_size != item['size']:
                partial = path.with_name(path.name + '.partial')
                url = f'https://huggingface.co/{name}/resolve/{revision}/' + urllib.parse.quote(item['name'], safe='/')
                subprocess.run(['curl', '-fL', '--retry', '3', '--connect-timeout', '30', '--continue-at', '-', '--progress-bar', '-o', str(partial), url], check=True)
                if partial.stat().st_size != item['size']:
                    raise ValueError('incomplete artifact file: ' + item['name'])
                partial.replace(path)
            expected = item['sha256'] or item.get('blob_sha1')
            if expected:
                with path.open('rb') as stream:
                    hasher = hashlib.sha256() if item['sha256'] else hashlib.sha1(f"blob {item['size']}\0".encode(), usedforsecurity=False)
                    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                if digest != expected:
                    path.unlink()
                    raise ValueError('artifact checksum mismatch: ' + item['name'])
        existing = {'model': name, 'revision': revision, 'files': files}
        (destination / '.t-artifact.json').write_text(json.dumps(existing, indent=2))
    if not download_only:
        with runtime(name, context) as (base, alias, _):
            answer = request(base + '/v1/responses', {'model': alias, 'input': 'Reply with OK.', 'max_output_tokens': 128}, timeout=300)
            if not answer.get('output'):
                raise ValueError('local runtime returned no output')
        existing['verified_context'] = max(existing.get('verified_context', 0), context)
        (destination / '.t-artifact.json').write_text(json.dumps(existing, indent=2))
    return dict(model=name, downloaded=True, ready=bool(existing.get('verified_context')), revision=existing['revision'])


def measure(name, context=32768):
    policy({'context_tokens': context})
    suitability = report(artifact(name), helper('t_local_models')['hardware'](), context)
    if suitability['fit'] is not True:
        raise ValueError(suitability['reason'])
    readings = []
    with runtime(name, context) as (base, alias, info):
        for iteration in range(4):
            start = time.monotonic()
            result = request(base + '/v1/chat/completions', {'model': alias, 'messages': [{'role': 'user', 'content':
                f'Case {iteration}: Write a Python binary search function with docstring and examples. Return code only.'}],
                'max_tokens': 256, 'temperature': 0}, timeout=300)
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
        row['ready'] = row['installed'] and row['fit']
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


def select_model(ui, current=DEFAULT_MODEL, context=32768):
    suite = 'swe-bench-verified-bash-only'
    while True:
        view = catalogue(suite, context)
        rows = [(r['model'], r['model'].split('/')[-1]) for r in view['models']]
        values = {r['model']: f"Memory fit {'✓' if r['fit'] is True else '✕' if r['fit'] is False else '?'} · "
                  f"{r['benchmark']['score'] if r['benchmark'] else '—'} · "
                  f"{'Ready' if r.get('ready') else 'Downloaded' if r['installed'] else 'Not installed'}" for r in view['models']}
        choice = ui.pick('Local model · ' + suite, rows + [('__suite', 'Change benchmark'), ('__back', 'Back')],
                         values=values, default=next((i for i,r in enumerate(rows) if r[0] == current),0),
                         accents={r['model']: 'y' for r in view['models'] if r['fit'] is not True})
        if choice in (None, '__back'):
            return None
        if choice == '__suite':
            suite = ui.pick('Benchmark suite', [(v,v) for v in SUITES]) or suite
            continue
        row = next(r for r in view['models'] if r['model'] == choice)
        detail = ui.page('Model details', detail_lines(row), accept=('enter', 'q', 'esc', 'abort'), keys='enter actions · q back')
        if detail == 'abort':
            raise KeyboardInterrupt
        if detail != 'enter':
            continue
        disabled = {}
        if not row.get('ready'):
            disabled['measure'] = 'Install and verify the model before measuring.'
        if not row.get('disk_fit', True):
            disabled['download'] = 'Insufficient disk space'
        if row['fit'] is not True:
            disabled['use'] = disabled['install'] = row['reason']
        if not row.get('disk_fit', True):
            disabled['install'] = 'Insufficient disk space'
        if row['fit'] is True and not row.get('ready'):
            disabled['use'] = 'Install and verify this artifact before using it.'
        action = ui.pick('Model actions', [('use', 'Use in workflow'), ('install', 'Install and verify'),
                         ('download', 'Download only'), ('measure', 'Measure speed'), ('back', 'Back')], disabled=disabled)
        if action == 'use':
            return {'model': row['model'], 'provider': row['provider']}
        if action in ('install', 'download', 'measure'):
            if action != 'measure' and ui.pick('Review download', [('yes', f"Download up to {row['bytes']/1e9:.1f} GB to {model_directory(choice) if row['provider'] == 'lmstudio' else Path.home()/'.ollama/models'}"), ('no', 'Back')]) != 'yes':
                continue
            ui.restore()
            try:
                with helper('t_local_worker')['reservation']():
                    result = (measure(choice, context) if action == 'measure' else install(choice, action == 'download', context)) if row['provider'] == 'lmstudio' else ollama_action(choice, action, context=context)
                ui.done('complete', json.dumps(result))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                ui.done('unavailable', str(exc))
            finally:
                ui.raw()


def workflow_menu(ui, value=None):
    current = policy(value)
    while True:
        choice = ui.pick('Inference & routing', [('mode', 'Routing'), ('model', 'Local model'),
                         ('context_tokens', 'Context'), ('timeout_seconds', 'Task budget'), ('back', 'Back')], values={k: str(v) for k,v in current.items()})
        if choice in (None, 'back'):
            return current
        if choice == 'model':
            selection = select_model(ui, current['model'], current['context_tokens'])
            if selection:
                current.update(selection)
        elif choice == 'mode':
            selected = ui.pick('Routing', [(v, {'automatic':'Automatic hybrid · approve local worker tools', 'local-only':'Local only · no cloud fallback',
                                             'manual':'Manual delegation', 'disabled':'Disabled'}[v]) for v in MODES])
            if selected:
                current['mode'] = selected
        else:
            values = (8192, 16384, 32768, 65536) if choice == 'context_tokens' else (60, 300, 900, 1800)
            selected = ui.pick(choice.replace('_', ' ').title(), [(str(v),str(v)) for v in values])
            if selected:
                current[choice] = int(selected)


def ollama_action(name, action, download_only=False, context=32768):
    w = helper('t_local_worker')
    with w['ollama_server'](context) as (base, env):
        if action in ('install', 'download'):
            subprocess.run([w['executable']('ollama'), 'pull', name], env=env, check=True)
        info = w['check_model'](base, name)
        if action != 'measure':
            return {'model': name, 'provider': 'ollama', 'downloaded': True, 'ready': True}
        readings = []
        for iteration in range(4):
            value = request(base + '/api/generate', {'model': name, 'stream': False,
                'prompt': f'Case {iteration}: Write a Python binary search function with docstring and examples.',
                'options': {'num_ctx': context, 'num_predict': 256, 'temperature': 0}}, timeout=300)
            tokens, duration = value.get('eval_count', 0), value.get('eval_duration', 0)
            if not tokens or not duration:
                raise ValueError('runtime returned no measured decode timing')
            if iteration:
                readings.append(tokens / (duration / 1e9))
        measurement = {'metric': 'decode tokens / second', 'median': statistics.median(readings),
            'min': min(readings), 'max': max(readings), 'measured_at': time.time(), 'context_tokens': context,
            'model': name, 'revision': ollama_revision(name), 'metadata': info, 'hardware': helper('t_local_models')['hardware'](), 'workload_version': 1}
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
        with helper('t_local_worker')['reservation']():
            result = (measure(name, context) if action == 'measure' else install(name, getattr(args, 'download_only', False), context)) if provider == 'lmstudio' else ollama_action(name, action, getattr(args, 'download_only', False), context)
    if action == 'show' and not getattr(args, 'json', False):
        print('\n'.join(detail_lines(result)))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


@contextmanager
def provider_runtime(chosen):
    if chosen['provider'] == 'lmstudio':
        with runtime(chosen['model'], chosen['context_tokens'], None) as value:
            yield value
    else:
        w = helper('t_local_worker')
        with w['ollama_server'](chosen['context_tokens']) as (base, _):
            w['check_model'](base, chosen['model'])
            tags = request(base + '/api/tags')['models']
            info = next((r for r in tags if r['name'] == chosen['model']), None)
            if not info or not info.get('digest'):
                raise ValueError('Ollama did not confirm the exact local model digest')
            yield base, chosen['model'], {'revision': info['digest']}


def lead(argv, cwd=None):
    """Dedicated native CLI launch: no cloud auth, models or providers inherited."""
    chosen = effective(cwd or os.getcwd())
    if not chosen or chosen['mode'] != 'local-only':
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
    with w['reservation'](), provider_runtime(chosen) as (base, alias, info):
        home = w['state_root']() / ('lead-' + uuid.uuid4().hex)
        env = w['environment'](home)
        env.update(CODEX_OSS_BASE_URL=base + '/v1')
        codex_home = Path(env['CODEX_HOME'])
        workflow = helper('t_workflow')
        document, _ = workflow['load']()
        (codex_home / 'AGENTS.md').write_text(workflow['render'](document) + '\n' + instructions(chosen) + '\nDo work in this lead; the single local inference slot is reserved for this session.\n')
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
        print(f"Local only · {chosen['model']} · {chosen['provider']} · revision {info['revision'][:12]}", file=sys.stderr)
        return subprocess.call([argv[0], *forced, *filtered], env=env)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['--launch-prefix']:
            try:
                selected = effective(sys.argv[2])
                required = selected and selected['mode'] == 'local-only'
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
