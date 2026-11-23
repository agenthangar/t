"""Read-only model comparisons shared by pickers, catalogues and monitors."""
import json
from pathlib import Path
import unicodedata


MODEL_STATES = {
    'ready': dict(icon='●', label='Ready', color='green', priority=3),
    'verified': dict(icon='◇', label='Verified', color='purple', priority=2),
    'downloaded': dict(icon='↓', label='Downloaded', color='amber', priority=1),
    'available': dict(icon='○', label='Available', color='hint', priority=0),
}
MEMORY_STATES = {
    True: dict(icon='✓', label='Fits', color='green'),
    False: dict(icon='✕', label='Blocked', color='red'),
    None: dict(icon='?', label='Unknown fit', color='hint'),
}
MARKER_COLORS = {'green': 92, 'purple': 95, 'amber': 33, 'red': 91, 'hint': 90}
MARKERS = {item['icon']: MARKER_COLORS[item['color']]
           for item in [*MODEL_STATES.values(), *MEMORY_STATES.values()]}
BENCHMARK_COLUMNS = {
    'swe-bench-pro': 'SWE Pro', 'deepswe': 'DeepSWE', 'terminal-bench': 'Terminal',
    'livebench-coding': 'LiveBench', 'gpqa': 'GPQA', 'swe-bench-verified-bash-only': 'SWE Verified',
}


def model_state(row):
    if row.get('ready'):
        return 'ready'
    if row.get('installed'):
        return 'verified' if row.get('verified') else 'downloaded'
    return 'available'


def clean(value):
    return ''.join(c for c in str(value) if c.isprintable())[:500]


def width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in value)


def clip(value, limit):
    value = clean(value)
    if width(value) <= limit:
        return value
    out = ''
    for c in value:
        if width(out + c) > limit - 1:
            break
        out += c
    return out + '…'


def local_models(rows, suite, selected=''):
    models = []
    for row in rows:
        score = row.get('benchmarks', {}).get(suite) or row.get('benchmark') or {}
        name = row['model']
        fit = None if row.get('reason') == 'accelerator capacity unknown' else row.get('fit')
        models.append(dict(key=name, model=name.split('/')[-1], provider=row.get('provider', 'ollama'),
                           size=f"{row['bytes']/1e9:.1f}" if row.get('bytes') is not None else '—',
                           fit=MEMORY_STATES[fit]['icon'], ready=MODEL_STATES[model_state(row)]['icon'], selected=name == selected,
                           score=str(score.get('score', '—')), suite=suite,
                           **{key: str(benchmark['score']) for key, benchmark in row.get('benchmarks', {}).items()
                              if key in BENCHMARK_COLUMNS and 'score' in benchmark},
                           evidence=' · '.join(clean(score[k]) for k in ('version', 'kind') if score.get(k))))
    return models


def native_models(rows, catalog, provider, selected=''):
    lookup = {r['id']: r for r in catalog}
    result = []
    for key, label in rows:
        if not key or key.startswith('__'):
            continue
        info = lookup.get(key, {})
        description = info.get('description') or ('Effort: ' + '/'.join(info['efforts']) if info.get('efforts') else 'Capabilities unavailable')
        result.append(dict(key=key, model=label, description=description, provider=provider,
                           size='—', fit='—', ready='—', score='—', selected=key == selected))
    return result


def table(models, columns=92, *, color=False, focus=None):
    """Keep each row on one line; drop the same optional columns for all rows."""
    cloud = bool(models) and all(m['fit'] == '—' for m in models)
    gap = 2 if columns >= 32 else 1
    mark = [('mark', '', 1)] if columns >= 24 else []
    if cloud and columns >= 52:
        specs = [*mark, ('model', 'MODEL', max(10, columns - 48)),
                 ('description', 'DETAILS', 18), ('provider', 'PROVIDER', 9), ('score', 'SCORE', 5)]
    elif columns >= 52:
        suite = next((m['suite'] for m in models if m.get('suite')), '')
        heading = BENCHMARK_COLUMNS.get(suite, 'Score').upper()
        score = ('score', heading, max(5, len(heading)))
        rest = [('size', 'GB', 5), ('provider', 'RUNTIME', 9), ('fit', 'MEM', 3), score]
        model_width = columns - len(mark) - 2 - sum(size + gap for _, _, size in rest) - 2 * gap
        for key, title in BENCHMARK_COLUMNS.items():
            if key == suite or not any(key in model for model in models):
                continue
            size = max(5, len(title))
            if model_width - size - gap >= 12:
                rest.append((key, title.upper(), size))
                model_width -= size + gap
        specs = [*mark, ('ready', 'ST', 2), ('model', 'MODEL', model_width), *rest]
    else:
        model_width = columns - (18 if gap == 2 else 12 if mark else 9)
        specs = [*mark, ('ready', 'S', 1), ('model', 'MODEL', max(1, model_width)),
                 ('fit', 'FIT' if gap == 2 else 'F', 3 if gap == 2 else 1), ('score', 'SC' if columns < 24 else 'SCORE', 4 if columns < 24 else 5)]
    specs = [(k, title, min(n, max([len(title)] + [width(clean(m.get(k, ''))) for m in models])) if k == 'model' else n)
             for k, title, n in specs]
    def line(record, markers=False):
        parts = []
        for key, _, size in specs:
            value = clip(record.get(key, ''), size)
            pad = ' ' * max(0, size - width(value))
            if markers and key in ('fit', 'ready') and value in MARKERS:
                normal = 97 if record['key'] == focus else 39
                value = f'\033[{MARKERS[value]}m{value}\033[{normal}m'
            parts.append(pad + value if key in ('size', 'score') or key in BENCHMARK_COLUMNS else value + pad)
        return (' ' * gap).join(parts).rstrip()
    return (line({key: title for key, title, _ in specs}),
            {m['key']: line(dict(m, mark='★' if m.get('selected') else ''), color) for m in models})


def marker_legend(label, entries, columns, color=False):
    parts = [label + ':']
    groups = []
    for entry in entries.values():
        icon, name = entry['icon'], entry['label'].lower()
        item = icon + ' ' + name
        trial = ' · '.join([*parts, item])
        if width(trial) > columns and parts:
            groups.append(parts)
            parts = []
        parts.append(item)
    if parts:
        groups.append(parts)
    if color:
        groups = [[f'\033[{MARKERS[item[0]]}m{item[0]}\033[39m{item[1:]}'
                   if item[0] in MARKERS else item for item in group] for group in groups]
    return [' · '.join(group) for group in groups]


def legend(models, suite='', *, columns=120, color=False):
    if models and all(m['fit'] == '—' for m in models):
        return ['★ selected · — not applicable / no sourced benchmark']
    versions = list(dict.fromkeys(m.get('evidence', '') for m in models if m.get('evidence')))
    return ['★ configured · — no score',
            *marker_legend('State', MODEL_STATES, columns, color),
            *marker_legend('Memory', MEMORY_STATES, columns, color),
            'Score: ' + (suite or 'unavailable') + ' · base models; harnesses differ',
            *versions]


def lines(models, suite='', columns=100):
    header, rows = table(models, columns)
    return [header, *rows.values(), '', *legend(models, suite)]


def benchmark_summaries():
    """Bundled public evidence only; monitoring never queries or starts a provider."""
    data = json.loads(Path(__file__).with_name('mlx-models.json').read_text())
    result = {}
    for row in data['models']:
        scores = row.get('benchmarks', {})
        suite = 'swe-bench-pro' if 'swe-bench-pro' in scores else next(iter(scores), '')
        score = scores.get(suite)
        result[row['model']] = (suite + ' ' + str(score['score']) + ' · ' + score['version'] + ' · base model' if score else 'Benchmark unavailable')
    return result
