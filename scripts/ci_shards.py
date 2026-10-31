#!/usr/bin/env python3
"""CI-only whole-file scheduling, audit manifests and coverage reconciliation.

Load explicitly with ``pytest -p scripts.ci_shards``; normal pytest is unchanged.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import time

ROOT = Path(__file__).resolve().parent.parent
WEIGHTS = ROOT / 'tests/ci-file-durations.json'
LEGACY = ['tests/test_picker_ui.py', 'tests/test_history_picker.py',
          'tests/test_history_details.py', 'tests/test_share_ui.py']


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def assignments(files, weights, count):
    """Longest estimated file first; ties use filename then lowest shard index."""
    if count < 1:
        raise ValueError('shard count must be positive')
    if any(not isinstance(v, (int, float)) or not 0 < v < float('inf')
           for v in weights.values()):
        raise ValueError('weights must be finite positive seconds')
    fallback = statistics.median(weights.values()) if weights else 1.0
    loads = [0.0] * count
    groups = [[] for _ in range(count)]
    for name in sorted(set(files), key=lambda name: (-weights.get(name, fallback), name)):
        index = min(range(count), key=lambda index: (loads[index], index))
        groups[index].append(name)
        loads[index] += weights.get(name, fallback)
    return [sorted(group) for group in groups]


def pytest_addoption(parser):
    parser.addoption('--ci-shard', type=int, default=None)
    parser.addoption('--ci-shards', type=int, default=4)
    parser.addoption('--ci-legacy', action='store_true')
    parser.addoption('--ci-manifest', default=None)


def pytest_configure(config):
    config._ci_audit = {'started': time.monotonic(), 'outcomes': {}, 'durations': {}}


def pytest_collection_modifyitems(config, items):
    state = config._ci_audit
    full = [item.nodeid for item in items]
    if len(full) != len(set(full)):
        raise ValueError('duplicate collected node IDs')
    state['full'] = sorted(full)
    shard = config.getoption('--ci-shard')
    count = config.getoption('--ci-shards')
    legacy = config.getoption('--ci-legacy')
    if legacy and shard is not None:
        raise ValueError('legacy and shard selection are exclusive')
    files = sorted({item.nodeid.split('::')[0] for item in items})
    if shard is not None:
        if not 0 <= shard < count:
            raise ValueError('invalid shard index')
        selected_files = assignments(files, read_json(WEIGHTS), count)[shard]
    elif legacy:
        if not set(LEGACY) <= set(files):
            raise ValueError('missing legacy compatibility files')
        selected_files = LEGACY
    else:
        selected_files = files
    selected = [item for item in items if item.nodeid.split('::')[0] in selected_files]
    deselected = [item for item in items if item.nodeid.split('::')[0] not in selected_files]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
    state.update(selected=sorted(item.nodeid for item in items), files=selected_files,
                 shard=shard, count=count, legacy=legacy)


# The report hook has no config argument; a per-process plugin object keeps state.
def pytest_sessionstart(session):
    global _state
    _state = session.config._ci_audit


def pytest_runtest_logreport(report):
    outcomes = _state['outcomes']
    _state['durations'][report.nodeid] = _state['durations'].get(report.nodeid, 0) + report.duration
    if report.failed:
        outcomes[report.nodeid] = 'failed'
    elif report.skipped and outcomes.get(report.nodeid) != 'failed':
        outcomes[report.nodeid] = 'skipped'
    elif report.when == 'call' and report.passed:
        outcomes.setdefault(report.nodeid, 'passed')


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption('--ci-manifest')
    if path:
        state = session.config._ci_audit.copy()
        state['elapsed'] = time.monotonic() - state.pop('started')
        state.update(exitstatus=int(exitstatus), commit=os.environ.get('GITHUB_SHA', 'local'))
        write_json(path, state)


def checked_manifest(path):
    data = read_json(path)
    for key in ('full', 'selected'):
        if not data[key] or len(data[key]) != len(set(data[key])):
            raise ValueError(f'{path}: empty or duplicate {key}')
    if not set(data['selected']) <= set(data['full']):
        raise ValueError(f'{path}: selection outside collection')
    if set(data['outcomes']) != set(data['selected']):
        raise ValueError(f'{path}: missing or unexpected test outcomes')
    if data['exitstatus'] != 0 or not set(data['outcomes'].values()) <= {'passed', 'skipped'}:
        raise ValueError(f'{path}: unsuccessful test run')
    return data


def reconcile(manifests, count):
    if len(manifests) != count or {m['shard'] for m in manifests} != set(range(count)):
        raise ValueError('missing or duplicate shards')
    first = manifests[0]
    union = set()
    for manifest in manifests:
        if (manifest['full'] != first['full'] or manifest['commit'] != first['commit']
                or manifest['count'] != count or manifest['legacy']):
            raise ValueError('shard collection, commit or configuration differs')
        selected = set(manifest['selected'])
        if union & selected:
            raise ValueError('duplicate selected test across shards')
        union |= selected
    if union != set(first['full']):
        raise ValueError('incomplete shard union')
    return {node: outcome for m in manifests for node, outcome in m['outcomes'].items()}


def normalized_coverage(path):
    from coverage import CoverageData
    data = CoverageData(basename=str(path))
    data.read()
    result = {}
    for filename in data.measured_files():
        # CI checkouts may have different absolute prefixes on different runners.
        name = filename.replace('\\', '/')
        if '/libexec/' in name:
            name = 'libexec/' + name.rsplit('/libexec/', 1)[1]
        elif name.endswith('/bin/t'):
            name = 'bin/t'
        elif name != 'bin/t' and not name.startswith('libexec/'):
            raise ValueError(f'unexpected measured source: {filename}')
        if not (ROOT / name).is_file() or name in result:
            raise ValueError(f'missing or duplicate measured source: {name}')
        result[name] = sorted(data.lines(filename) or [])
    if not result:
        raise ValueError(f'empty coverage: {path}')
    return result


def summarize(manifests, percentage):
    lines = ['## Linux shard results', '', '| Shard | Seconds | Tests |', '| --- | ---: | ---: |']
    for manifest in sorted(manifests, key=lambda m: m['shard']):
        lines.append(f"| {manifest['shard']} | {manifest['elapsed']:.1f} | {len(manifest['selected'])} |")
    lines += ['', f'Combined coverage: **{percentage:.2f}%**. '
              f"Exact-once inventory: **{len(manifests[0]['full'])} tests**.", '', 'Slowest tests:', '']
    slow = sorted(((seconds, node) for m in manifests for node, seconds in m['durations'].items()), reverse=True)[:30]
    lines += [f'- {seconds:.2f}s `{node}`' for seconds, node in slow]
    output = '\n'.join(lines) + '\n'
    print(output)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(output)


def combine(directory, count, output):
    from coverage import Coverage, CoverageData
    directory = Path(directory)
    folders = sorted(directory.glob('shard-*'))
    if len(folders) != count or any(not (folder / 'manifest.json').is_file() for folder in folders):
        raise ValueError('missing, duplicate or incomplete shard artifacts')
    paths = [folder / 'manifest.json' for folder in folders]
    manifests = [checked_manifest(path) for path in paths]
    outcomes = reconcile(manifests, count)
    measured = {}
    for path in paths:
        coverage_paths = list(path.parent.glob('.coverage*'))
        if len(coverage_paths) != 1 or not coverage_paths[0].is_file():
            raise ValueError(f'{path}: missing or duplicate coverage data')
        for name, lines in normalized_coverage(coverage_paths[0]).items():
            measured.setdefault(name, set()).update(lines)
    Path(output).mkdir(parents=True, exist_ok=True)
    combined = CoverageData(basename=str(Path(output) / '.coverage'))
    combined.add_lines({str(ROOT / name): lines for name, lines in measured.items()})
    combined.write()
    cov = Coverage(data_file=str(Path(output) / '.coverage'), config_file=str(ROOT / 'pyproject.toml'))
    cov.load()
    percentage = cov.report(show_missing=True)
    cov.xml_report(outfile=str(Path(output) / 'coverage.xml'))
    write_json(Path(output) / 'audit.json', dict(commit=manifests[0]['commit'], full=manifests[0]['full'],
               outcomes=outcomes, measured={name: sorted(lines) for name, lines in measured.items()}))
    summarize(manifests, percentage)
    if percentage < 97:
        raise ValueError(f'combined coverage {percentage:.3f}% is below 97%')


def compare(audit_path, reference_dir):
    audit = read_json(audit_path)
    reference_dir = Path(reference_dir)
    ref = checked_manifest(reference_dir / 'manifest.json')
    if ref['shard'] is not None or ref['legacy'] or ref['selected'] != ref['full']:
        raise ValueError('reference is not a full unsharded run')
    for key in ('commit', 'full', 'outcomes'):
        if audit[key] != ref[key]:
            raise ValueError(f'reference differs: {key}')
    paths = list(reference_dir.glob('.coverage*'))
    if len(paths) != 1:
        raise ValueError('missing or duplicate reference coverage')
    measured = normalized_coverage(paths[0])
    if measured != audit['measured']:
        names = sorted(set(measured) | set(audit['measured']))
        details = []
        for name in names:
            actual = set(audit['measured'].get(name, []))
            expected = set(measured.get(name, []))
            if name not in measured or name not in audit['measured'] or actual != expected:
                details.append(f'{name}: only shards={sorted(actual - expected)}, '
                               f'only reference={sorted(expected - actual)}')
        raise ValueError('reference measured sources or executed lines differ: ' + '; '.join(details))
    print('Reference matches exact inventory, outcomes, measured sources and executed lines.')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('## Reference verification\n\nInventory, outcomes, sources and executed lines match.\n')


def refresh(directory, output):
    totals = {}
    for path in Path(directory).rglob('manifest.json'):
        manifest = checked_manifest(path)
        if manifest['shard'] is None:
            continue
        per_file = {}
        for node, seconds in manifest['durations'].items():
            name = node.split('::')[0]
            per_file[name] = per_file.get(name, 0) + seconds
        for name, seconds in per_file.items():
            totals.setdefault(name, []).append(seconds)
    if not totals:
        raise ValueError('no successful shard timings found')
    write_json(output, {name: round(max(statistics.median(values), 0.001), 3)
                        for name, values in totals.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('combine')
    command.add_argument('directory')
    command.add_argument('--count', type=int, default=4)
    command.add_argument('--output', default='combined')
    command = commands.add_parser('compare')
    command.add_argument('audit')
    command.add_argument('reference')
    command = commands.add_parser('refresh')
    command.add_argument('directory')
    command.add_argument('--output', default=str(WEIGHTS))
    args = parser.parse_args()
    if args.command == 'combine':
        combine(args.directory, args.count, args.output)
    elif args.command == 'compare':
        compare(args.audit, args.reference)
    else:
        refresh(args.directory, args.output)


if __name__ == '__main__':
    main()
