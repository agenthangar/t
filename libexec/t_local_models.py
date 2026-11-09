"""Public metadata only: local model availability, comparable evals and estimated fit."""
import concurrent.futures
import csv
from datetime import datetime, timezone
from html import unescape
import io
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
TTL = 7 * 86400
LIMIT = 4 * 1024 * 1024
SOURCE = 'https://raw.githubusercontent.com/LiveBench/livebench.github.io/main/public/'
TREE = 'https://api.github.com/repos/LiveBench/livebench.github.io/git/trees/main?recursive=1'
LIBRARY = 'https://ollama.com/library'
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']


def cache_path():
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 't/local-models.json'


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 't-local-model-catalog', 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=5) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('metadata response exceeds 4 MiB')
    return data.decode('utf-8')


def plain(html):
    return ' '.join(unescape(re.sub('<[^>]+>', ' ', html)).split())


def families(html):
    result = []
    for block in re.findall(r'<li\b[^>]*>(.*?)</li>', html, re.S):
        match = re.search(r'href="/library/([a-z0-9][a-z0-9._-]*)"', block)
        if match and re.search(r'>\s*tools\s*</span>', block):
            result.append(match[1])
    if not result:
        raise ValueError('Ollama returned no tool-capable model families')
    return list(dict.fromkeys(result))[:64]


def variants(family, html):
    result = {}
    for tag, body in re.findall(r'<a\b[^>]*href="/library/' + re.escape(family) + r':([^"/]+)"[^>]*>(.*?)</a>', html, re.S):
        # Canonical parameter tags avoid aliases, quantization duplicates and cloud models.
        if not re.fullmatch(r'\d+(?:\.\d+)?b', tag):
            continue
        sizes = re.findall(r'(\d+(?:\.\d+)?)\s*(GB|MB)', plain(body))
        if sizes:
            size = max(float(n) * (10**9 if unit == 'GB' else 10**6) for n, unit in sizes)
            name = family + ':' + tag
            result[name] = {'model': name, 'bytes': int(size), 'source': LIBRARY + '/' + family + '/tags'}
    return list(result.values())


def evaluations(table, categories):
    columns = categories.get('Coding', [])
    if not columns or not all(isinstance(c, str) for c in columns):
        raise ValueError('LiveBench coding category is unavailable')
    result = {}
    for row in csv.DictReader(io.StringIO(table)):
        try:
            scores = [float(row[c]) for c in columns]
        except (KeyError, ValueError, TypeError):
            continue
        if all(math.isfinite(s) and 0 <= s <= 100 for s in scores):
            result[row['model']] = round(sum(scores) / len(scores), 3)
    if not result:
        raise ValueError('LiveBench returned no coding scores')
    return result


def load():
    for path in (cache_path(), ROOT / 'libexec/local-models.json'):
        try:
            data = json.loads(path.read_text())
            if data['schema'] == 1 and data['models'] and data['benchmarks'] and isinstance(data['fetched_at'], (int, float)):
                return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
    raise ValueError('no model catalog; run t config models refresh')


@_spinner('Refreshing local model availability and coding evaluations')
def refresh():
    # Each request has a size and time bound; cap fan-out and the entire scheduling window.
    deadline = time.monotonic() + 60
    catalog = families(fetch(LIBRARY))
    tree = json.loads(fetch(TREE))
    dates = sorted({m[1] for item in tree['tree']
                    if (m := re.fullmatch(r'public/table_(\d{4}_\d{2}_\d{2})\.csv', item['path']))}, reverse=True)[:12]
    if not dates:
        raise ValueError('LiveBench release discovery returned no tables')
    models, benchmarks, warnings = [], [], []

    def get_family(family):
        return variants(family, fetch(LIBRARY + '/' + family + '/tags'))

    def get_benchmark(date):
        url = SOURCE + 'table_' + date + '.csv'
        scores = evaluations(fetch(url), json.loads(fetch(SOURCE + 'categories_' + date + '.json')))
        return {'release': date.replace('_', '-'), 'source': url, 'scores': scores}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [(get_family, name, models) for name in catalog] + [(get_benchmark, date, benchmarks) for date in dates]
        def execute(job):
            fn, name, _ = job
            if time.monotonic() >= deadline:
                raise ValueError('refresh time budget reached')
            return fn(name)
        futures = [(pool.submit(execute, job), job) for job in jobs]
        for future, (fn, name, target) in futures:
            try:
                value = future.result()
                target.extend(value if fn == get_family else [value])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f'{name}: {exc}')
    # Do not replace a complete cached catalog with an incomplete refresh.
    if warnings or not models or not benchmarks:
        raise ValueError('catalog refresh incomplete; previous catalog kept: ' + '; '.join(warnings[:3]))
    data = {'schema': 1, 'fetched_at': time.time(), 'models': sorted(models, key=lambda r: r['model']),
            'benchmarks': sorted(benchmarks, key=lambda r: r['release'], reverse=True),
            'coverage': 'Up to 64 Ollama tool-capable families, canonical parameter tags; latest 12 LiveBench v1 releases.'}
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.local-models-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return data


def probe(command):
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=2)
        return proc.stdout.strip() if proc.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def hardware():
    ram, accelerator, vram = None, 'unknown', None
    if sys.platform == 'darwin':
        raw = probe(['sysctl', '-n', 'hw.memsize'])
        ram = int(raw) if raw.isdigit() else None
        chip = probe(['sysctl', '-n', 'machdep.cpu.brand_string'])
        if chip.startswith('Apple '):
            accelerator, vram = chip + ' (unified memory)', ram
    elif sys.platform.startswith('linux'):
        try:
            match = re.search(r'MemTotal:\s+(\d+) kB', Path('/proc/meminfo').read_text())
            ram = int(match[1]) * 1024 if match else None
        except OSError:
            pass
        if shutil.which('nvidia-smi'):
            raw = probe(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'])
            amounts = [int(s) * 1024**2 for s in raw.splitlines() if s.strip().isdigit()]
            if amounts:
                accelerator, vram = 'NVIDIA (largest single GPU)', max(amounts)
    directory = Path.home() / '.ollama/models'
    while not directory.exists():
        directory = directory.parent
    return {'ram_bytes': ram, 'accelerator': accelerator, 'vram_bytes': vram,
            'free_disk_bytes': shutil.disk_usage(directory).free, 'context_tokens': 32768}


def missing_bytes(model, size):
    # Reuse only complete local blobs; never start Ollama or trust a manifest alone.
    family, tag = model.split(':')
    root = Path.home() / '.ollama/models'
    try:
        manifest = json.loads((root / 'manifests/registry.ollama.ai/library' / family / tag).read_text())
        layers = [manifest['config'], *manifest['layers']]
        missing = 0
        for layer in layers:
            digest, length = layer['digest'], layer['size']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or not isinstance(length, int) or length < 0:
                raise ValueError('invalid local manifest')
            blob = root / 'blobs' / digest.replace(':', '-')
            if not blob.is_file() or blob.stat().st_size != length:
                missing += length
        return max(0, missing)
    except (OSError, ValueError, KeyError, TypeError):
        return size


def score_for(model, scores):
    identity = model.replace(':', '-')
    # Explicit aliases only. Preserve provider/thinking variant in the evidence.
    if model == 'qwen3:30b':
        identity = 'qwen3-30b-a3b'
    for suffix in ('', '-thinking', '-it', '-instruct', '-instruct-turbo'):
        if identity + suffix in scores:
            return scores[identity + suffix], identity + suffix
    return None, None


def report(data=None, machine=None, benchmark=None):
    data, machine = data or load(), machine or hardware()
    rows = []
    for item in data['models']:
        row = dict(item)
        weights = row['bytes']
        # Deliberately conservative, architecture-independent estimate; not a measured KV cache.
        needed = int(weights * 1.2 + 4 * GIB)
        ram, vram = machine['ram_bytes'], machine['vram_bytes']
        capacity = min(ram - max(4 * GIB, ram * .2), vram * .9) if ram and vram else None
        download = missing_bytes(row['model'], weights)
        reason = 'estimated fit' if capacity is not None and needed <= capacity else 'memory estimate exceeds budget' if capacity is not None else 'accelerator capacity unknown'
        if download + 2 * GIB > machine['free_disk_bytes']:
            reason = 'insufficient free disk'
        row.update(memory_bytes=needed, download_bytes=download, fit=reason == 'estimated fit', reason=reason)
        rows.append(row)
    releases = sorted(data['benchmarks'], key=lambda b: b['release'], reverse=True)
    selected = next((b for b in releases if b['release'] == benchmark), None) if benchmark else next(
        (b for b in releases if any(r['fit'] and score_for(r['model'], b['scores'])[0] is not None for r in rows)), releases[0])
    if selected is None:
        raise ValueError('unknown benchmark release; available: ' + ', '.join(b['release'] for b in releases))
    for row in rows:
        row['score'], row['evaluated_model'] = score_for(row['model'], selected['scores'])
    rows.sort(key=lambda r: (not r['fit'], r['score'] is None, -(r['score'] or 0), r['model']))
    recommended = next((r['model'] for r in rows if r['fit'] and r['score'] is not None), None)
    return {'hardware': machine, 'fetched_at': data['fetched_at'], 'stale': time.time() - data['fetched_at'] > TTL,
            'benchmark': {k: selected[k] for k in ('release', 'source')}, 'latest_release': releases[0]['release'],
            'recommended': recommended, 'models': rows, 'coverage': data['coverage'],
            'caveat': 'LiveBench v1 Coding, 0–100; base-model evidence, not local quantization or Codex worker measurements. '
                      'Memory includes 20% weight overhead, 4 GiB context allowance and separate OS headroom; fit is an estimate.'}


def label(row):
    score = f"{row['score']:.1f}" if row['score'] is not None else 'unranked'
    return f"{row['model']} · {score} · {row['memory_bytes'] / GIB:.1f} GiB · {row['reason']}"


def choose(dialog):
    data = load()
    if time.time() - data['fetched_at'] > TTL:
        with dialog.suspended():
            try:
                data = refresh()
            except (OSError, ValueError) as exc:
                dialog.notice(str(exc))
    while True:
        view = report(data)
        dialog.notice(f"LiveBench v1 Coding {view['benchmark']['release']} · {'stale' if view['stale'] else 'cached'} metadata. "
                      f"Best evaluated fit: {view['recommended'] or 'none'}. Estimates at 32K context; unranked means no matching eval.")
        display = runpy.run_path(str(ROOT / 'libexec/t_model_display.py'))
        comparison = display['local_models']([dict(r, bytes=r.get('download_bytes'),
            benchmark=dict(view['benchmark'], score=r['score']) if r.get('score') is not None else None)
            for r in view['models']], 'livebench-coding')
        choice = dialog.choose('Local model (download on apply)', [(r['model'], label(r)) for r in view['models']] +
                               [('refresh', 'Refresh availability and evaluations'), ('custom', 'Enter another Ollama model name')],
                               model_table=comparison, benchmark_suite='LiveBench Coding '+view['benchmark']['release'])
        if choice != 'refresh':
            row = next((r for r in view['models'] if r['model'] == choice), None)
            if row:
                dialog.notice(label(row) + f" · download up to {row['download_bytes'] / GIB:.1f} GiB. " +
                              f"Eval: {row['evaluated_model'] or 'not evaluated'}; " + view['caveat'])
            return choice
        with dialog.suspended():
            try:
                data = refresh()
            except (OSError, ValueError) as exc:
                dialog.notice(str(exc))


def run(args):
    try:
        data = refresh() if args.models_action == 'refresh' else load()
        view = report(data, benchmark=getattr(args, 'benchmark', None))
        if getattr(args, 'json', False):
            print(json.dumps(view, indent=2))
        else:
            machine = view['hardware']
            print(f"Local models · {machine['accelerator']} · {machine['context_tokens']} context tokens")
            print(f"Metadata: {datetime.fromtimestamp(view['fetched_at'], timezone.utc).date()} UTC · "
                  f"{'stale; run t config models refresh' if view['stale'] else 'cached'}")
            print(f"LiveBench v1 Coding {view['benchmark']['release']} (latest available: {view['latest_release']})")
            print('Best evaluated fit: ' + (view['recommended'] or 'none'))
            print(view['caveat'])
            print(f"RAM: {machine['ram_bytes'] / GIB:.1f} GiB" if machine['ram_bytes'] else 'RAM: unknown')
            print(f"Free model disk: {machine['free_disk_bytes'] / GIB:.1f} GiB (2 GiB reserved)")
            print('Model · coding score / 100 · estimated memory · suitability')
            for row in view['models']:
                print(label(row))
            print('Evaluation source: ' + view['benchmark']['source'])
            print('Select and review installation: t config local')
        return 0
    except (OSError, ValueError) as exc:
        print('t config models: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
