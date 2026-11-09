"""Read-only model comparisons shared by pickers, catalogues and monitors."""
import json
from pathlib import Path
import unicodedata


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
        fit = row.get('fit')
        models.append(dict(key=name, model=name.split('/')[-1], provider=row.get('provider', 'ollama'),
                           size=f"{row['bytes']/1e9:.1f}" if row.get('bytes') is not None else '—',
                           fit='✓' if fit is True else '✕' if fit is False else '?',
                           ready='✓' if row.get('ready') else '○', selected=name == selected,
                           score=str(score.get('score', '—')),
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


def table(models, columns=92):
    """Keep each row on one line; drop the same optional columns for all rows."""
    cloud = bool(models) and all(m['fit'] == '—' for m in models)
    if columns < 24:
        specs = [('model', 'MODEL', max(1, columns-9)), ('fit', 'F', 1), ('ready', 'V', 1), ('score', 'SC', 4)]
        gap = 1
    elif columns >= 52:
        specs = [('mark', '', 1), ('model', 'MODEL', max(10, columns - (48 if cloud else 41))),
                 *(([('description', 'DETAILS', 18), ('provider', 'PROVIDER', 9)] if cloud else [('size', 'GB', 5), ('provider', 'PROVIDER', 9), ('fit', 'FIT', 3), ('ready', 'READY', 5)])),
                 ('score', 'SCORE', 5)]
        gap = 2
    else:
        gap = 2 if columns >= 32 else 1
        specs = [('mark', '', 1), ('model', 'MODEL', columns - (18 if gap == 2 else 12)),
                 ('fit', 'FIT' if gap == 2 else 'F', 3 if gap == 2 else 1), ('ready', 'V', 1), ('score', 'SCORE', 5)]
    specs = [(k, title, min(n, max([len(title)] + [width(clean(m.get(k, ''))) for m in models])) if k == 'model' else n)
             for k, title, n in specs]
    def line(record):
        parts = []
        for key, _, size in specs:
            value = clip(record.get(key, ''), size)
            pad = ' ' * max(0, size - width(value))
            parts.append(pad + value if key in ('size', 'score') else value + pad)
        return (' ' * gap).join(parts).rstrip()
    return (line({key: title for key, title, _ in specs}),
            {m['key']: line(dict(m, mark='★' if m.get('selected') else '')) for m in models})


def legend(models, suite=''):
    if models and all(m['fit'] == '—' for m in models):
        return ['★ selected · — not applicable / no sourced benchmark']
    versions = list(dict.fromkeys(m.get('evidence', '') for m in models if m.get('evidence')))
    return ['★ selected · F/FIT: ✓ fits, ✕ too large, ? unknown',
            'V/READY: ✓ verified, ○ unverified · — no score',
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
