#!/usr/bin/env python3
"""Build the offline HTML reference for the resume and model-library pickers."""
import argparse
from itertools import product
import json
import os
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / 'ui/previews'
sys.path.insert(0, str(ROOT / 'libexec'))

import t_local_models
from t_model_display import MEMORY_STATES, MODEL_STATES
import t_picker_filters as resume
import t_picker_theme as theme
from t_session_sort import SAVED_SORTS

STATES = {'all': '', 'ready': 'Ready', 'verified': 'Verified', 'downloaded': 'Downloaded',
          'available': 'Available', 'not ready': 'Not ready'}
RUNTIMES = {'all': '', 'lmstudio': 'LM Studio', 'mlx-vlm': 'mlx-vlm', 'ollama': 'Ollama'}
MEMORY = {'all': '', 'not blocked': '✓ ?', 'fits': '✓', 'blocked': '✕', 'unknown': '?'}
BENCHMARKS = {
    'swe-bench-pro': 'SWE-bench Pro', 'deepswe': 'DeepSWE', 'terminal-bench': 'Terminal-Bench',
    'livebench-coding': 'LiveBench Coding', 'gpqa': 'GPQA', 'swe-bench-verified-bash-only': 'SWE-bench Verified',
}
KEYS = {
    'Alt+F': 'expand-filters', 'Control+F': 'toggle-filters', 'Control+T': 'state',
    'Control+R': 'runtime', 'Control+G': 'fit', 'Control+X': 'reset-filters',
    'Control+Y': 'next-sort', 'Control+S': 'toggle-sort',
}


def read_catalog(name):
    return json.loads((ROOT / f'libexec/{name}-models.json').read_text())


def sample_catalog():
    """Read bundled public metadata. Fit, files and readiness are examples."""
    ollama = read_catalog('local')
    benchmark = max(ollama['benchmarks'], key=lambda row: row['release'])
    rows = []
    for item in ollama['models']:
        name, size = item['model'], item['bytes']
        score, _ = t_local_models.score_for(name, benchmark['scores'])
        benchmarks = {} if score is None else {'livebench-coding': {
            'score': score, 'version': benchmark['release'], 'kind': 'Base-model benchmark',
        }}
        rows.append(dict(model=name, provider='ollama', bytes=size, fit=size <= 85 * 10**9,
                         installed=False, ready=False, benchmarks=benchmarks))
    for item in read_catalog('mlx')['models']:
        name = item['model']
        for provider in ('lmstudio', 'mlx-vlm'):
            flash = 'Flash-Next' in name
            rows.append(dict(model=name, provider=provider, bytes=item['bytes'],
                             fit=None if flash and provider == 'lmstudio' else True,
                             ready=(provider == 'mlx-vlm' if flash else provider == 'lmstudio'),
                             installed=True, benchmarks=item.get('benchmarks', {})))
    return rows


def breakpoints(render):
    """Store only the widths at which terminal rows change."""
    tables = {}
    for compact in (False, True):
        previous = None
        changes = []
        for width in range(16, 161):
            rows = render(width, compact)
            if rows != previous:
                changes.append([width, rows])
                previous = rows
        tables[str(int(compact))] = changes
    return tables


def common_controls():
    browser_names = {'filter': 'refresh-gap', 'sort': 'filter-gap', 'legend': 'sort-gap',
                     'actions': 'legend-gap', 'columns': 'results-gap'}
    return {
        'gapPriority': [browser_names[name] for name in theme.PICKER_BREAK_PRIORITY],
        'contentPriority': [name for name in theme.PICKER_CONTENT_PRIORITY
                            if name in ('legend', 'filter', 'sort', 'filter-options', 'sort-options')],
    }


def model_controls(catalog):
    filters = {}
    for state, runtime, fit in product(STATES, RUNTIMES, MEMORY):
        values = [f'{key} {value}' for key, value in (
            ('Ctrl-T State:', STATES[state]), ('Ctrl-R Runtime:', RUNTIMES[runtime]),
            ('Ctrl-G Memory:', MEMORY[fit])) if value]
        filters['|'.join((state, runtime, fit))] = breakpoints(
            lambda w, c: theme.picker_filter_summary(values, True, w, c, True, 'darwin'))
    filters['paused'] = breakpoints(lambda w, c: theme.picker_filter_summary([], False, w, c, True, 'darwin'))
    benchmark_keys = {suite for row in catalog for suite in row['benchmarks']}
    benchmarks = {key: name for key, name in BENCHMARKS.items() if key in benchmark_keys}
    sorts = {'name': '', 'size': 'Size ↑', 'readiness': 'Readiness ↓',
             **{key: name + ' ↓' for key, name in benchmarks.items()}}
    facets = ['Ctrl-T state', 'Ctrl-R runtime', 'Ctrl-G memory']
    return {
        **common_controls(), 'keys': KEYS, 'filter': filters, 'benchmarks': list(benchmarks.items()),
        'modelStates': MODEL_STATES,
        'memoryStates': {key: MEMORY_STATES[value] for key, value in zip(('true', 'false', 'null'), (True, False, None))},
        'disclosure': [breakpoints(lambda w, c: theme.picker_filter_disclosure(state, w, c, True, 'darwin'))
                       for state in (False, True)],
        'filterOptions': [breakpoints(lambda w, c: theme.picker_filter_keys(facets, state, w, c, True, 'darwin'))
                          for state in (False, True)],
        'sort': {**{key: breakpoints(lambda w, c: theme.picker_sort_summary(value, True, w, c, True, 'darwin'))
                    for key, value in sorts.items()},
                 'paused': breakpoints(lambda w, c: theme.picker_sort_summary('', False, w, c, True, 'darwin'))},
        'sortOptions': [breakpoints(lambda w, c: theme.picker_sort_keys(state, w, c, True, 'darwin'))
                        for state in (False, True)],
    }


RESUME_CASES = [
    ('web', '3', 'codex', 'local', '↴ #14', 'MERGED', '2026-11-20', '2026-11-21', False, 'unfinished', False, 'Simplify the onboarding view'),
    ('api', '9', 'claude', 'mini', '○ #38', 'OPEN', '2026-11-21', '-', True, 'unfinished', False, 'Review API response formats'),
    ('api', '5', 'cursor', 'local', '◌ #21', 'DRAFT', '2026-11-19', '-', False, 'unknown', True, 'Update the cache documentation'),
    ('web', '1', 'codex', 'mini', '! #37', 'UNKNOWN', '2026-11-18', '-', False, 'unknown', False, 'Prepare a responsive navigation preview'),
    ('api', '2', 'codex', 'local', '-', 'NONE', '2026-11-17', '-', False, 'complete', False, 'Clean up the deployment checklist'),
]


def resume_fixture():
    headings = ['PRS', 'REPO', 'SLOT', 'AGENT', 'HOST', 'LAST TURN', 'TITLE']
    columns = '{:<9}  {:<4}  {:<4}  {:<5}  {:<5}  {:<16}  {}'
    lines = []
    for repo, slot, agent, host, pr, pr_state, day, closed, attached, work, preview, title in RESUME_CASES:
        turn = day + 'T12:00:00+00:00'
        closed = closed + 'T13:00:00+00:00' if closed != '-' else '-'
        display = columns.format(pr, repo, slot, resume.RESUME_AGENTS[agent][0], host, day + ' 12:00', title)
        lines.append('\t'.join((repo, slot, f'{repo}-{slot}', '/example', day, title,
                                host if attached else '-', 'fixture', host, agent, turn, '-', pr_state,
                                closed, '-', work,
                                'yes' if preview else 'no', display)))
    return columns.format(*headings), '\n'.join(lines)


def resume_controls():
    header, source = resume_fixture()
    records = resume.metadata(source, 'resume')
    rendered = resume.mark_resume(source, records)
    rows = {}
    for line, raw in zip(rendered.splitlines(), source.splitlines(), strict=True):
        fields = line.split('\t')
        plain = raw.split('\t')[-1]
        display = fields[-1]
        status = re.match(r'(?:(?:\x1b\[[0-9;]+m)*[^\x1b]){' +
                          str(resume.RESUME_STATUS_WIDTH) + r'}', display)[0]
        rows[fields[2]] = {**records[tuple(fields[:3])], 'attached': fields[6] != '-',
                           'status': status, 'workLabel': resume.WORK_STATUS[records[tuple(fields[:3])]['work']][1],
                           'display': display[len(status):], 'search': plain + ' ' + fields[9]}
    filters = {}
    choices = [dict.fromkeys(('agent', 'host', 'repo'), None)]
    for name in ('agent', 'host', 'repo'):
        choices = [dict(choice, **{name: value}) for choice, value in product(
            choices, [None, *sorted({row[name] for row in records.values()})])]
    for attached, merged, facets in product((False, True), (False, True), choices):
        key = '|'.join((str(int(attached)), str(int(merged)), *(value or '' for value in facets.values())))
        filters[key] = breakpoints(lambda w, c: resume.resume_controls(
            attached, facets, w, 'closed', True, True, merged, True, c, platform='darwin')['filter'])
    filters['paused'] = breakpoints(lambda w, c: theme.picker_filter_summary(
        [], False, w, c, True, 'darwin', ['Saved + attached']))
    return {
        **common_controls(), 'columns': f'{"STATUS":<{resume.RESUME_STATUS_WIDTH}}' + header,
        'statusWidth': resume.RESUME_STATUS_WIDTH, 'rows': rows,
        'order': {sort: [row.split('\t')[2] for row in resume.sort_resume(source.splitlines(), records, sort)]
                  for sort in SAVED_SORTS},
        'filter': filters, 'keys': {**KEYS, 'Control+G': 'agent', 'Control+R': 'repo',
                                    'Control+O': 'host', 'Control+D': 'merged'},
        'disclosure': [breakpoints(lambda w, c: theme.picker_filter_disclosure(state, w, c, True, 'darwin'))
                       for state in (False, True)],
        'filterOptions': [[breakpoints(lambda w, c: resume.resume_controls(
            attached, {}, w, 'closed', enabled, True, False, True, c,
            filters_expanded=True, platform='darwin')['filter-options']) for enabled in (False, True)]
                          for attached in (False, True)],
        'sort': {**{key: breakpoints(lambda w, c: theme.picker_sort_summary(
            resume.sort_label(key).capitalize(), True, w, c, True, 'darwin')) for key in SAVED_SORTS},
                 'paused': breakpoints(lambda w, c: theme.picker_sort_summary(
                     'Snapshot order', False, w, c, True, 'darwin'))},
        'sortOptions': [breakpoints(lambda w, c: theme.picker_sort_keys(state, w, c, True, 'darwin'))
                        for state in (False, True)],
        'legend': [breakpoints(lambda width, _: resume.resume_legend(width, False, True, attached=attached))
                   for attached in (False, True)],
        'actions': [breakpoints(lambda w, c: resume.resume_action_rows(w, c, True, attached, 'darwin'))
                    for attached in (False, True)],
    }


def data_script(identifier, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    return f'<script type="application/json" id="{identifier}">{payload}</script>'


def build():
    catalog = sample_catalog()
    source = (PREVIEW / 'picker.html').read_text()
    scripts = [
        data_script('sample-catalog', catalog),
        data_script('native-picker-data', model_controls(catalog)),
        data_script('resume-picker-data', resume_controls()),
        *(f'<script>\n{(PREVIEW / f"{name}.js").read_text()}\n</script>' for name in ('picker', 'models', 'resume')),
    ]
    return source.replace('<!-- picker scripts -->', '\n'.join(scripts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ.pop('NO_COLOR', None)
    os.environ.update(TERM='xterm-256color', T_PICKER_COLOR='1', T_PICKER_RESUME_LAST='1')
    html = build()
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    temporary.write_text(html)
    temporary.replace(args.output)


if __name__ == '__main__':
    main()
