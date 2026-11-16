#!/usr/bin/env python3
"""Validate explicit layout coverage and run its non-skippable rendered checks."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate(inventory, contract, root=ROOT):
    profiles = contract['profiles']
    selectors = set()
    for name, profile in profiles.items():
        tests = profile.get('tests', [])
        if not tests and not profile.get('exclusion'):
            raise ValueError(f'{name}: missing layout fixtures or exclusion')
        for node in tests:
            filename, function = node.split('::')
            path = (root / filename).resolve()
            if not path.is_relative_to(root.resolve()) or not filename.startswith('tests/test_'):
                raise ValueError(f'{name}: invalid test reference {node}')
            tree = ast.parse(path.read_text())
            if not any(isinstance(n, ast.FunctionDef) and n.name == function for n in tree.body):
                raise ValueError(f'{name}: missing layout fixture {node}')
            selectors.add(node)
    for command, entry in inventory['commands'].items():
        if entry.get('layout') not in profiles:
            raise ValueError(f'{command}: missing or unknown layout profile')
        if entry.get('renderer') not in profiles[entry['layout']].get('renderers', []):
            raise ValueError(f'{command}: layout profile does not match its renderer')
        if profiles[entry['layout']].get('exclusion') and entry.get('renderer') != 'external':
            raise ValueError(f'{command}: terminal renderer cannot be excluded')
        for mode, detail in entry.get('modes', {}).items():
            layout = detail.get('layout')
            if layout != 'inherit' and layout not in profiles:
                raise ValueError(f'{command}/{mode}: missing layout coverage')
            if layout == 'machine' and set(detail.get('patterns', [])) != {'machine'}:
                raise ValueError(f'{command}/{mode}: only machine-only modes may be excluded')
            if layout == 'external' and set(detail.get('patterns', [])) != {'passthrough'}:
                raise ValueError(f'{command}/{mode}: only passthrough-only modes may be excluded')
    for name, profile in contract['nested_screens'].items():
        if profile not in profiles or not profiles[profile].get('tests'):
            raise ValueError(f'{name}: nested screen lacks rendered coverage')
    if not selectors:
        raise ValueError('empty design suite')
    return sorted(selectors)


def selectors():
    return validate(json.loads((ROOT / 'ui/command-contract.json').read_text()),
                    json.loads((ROOT / 'ui/layout-contract.json').read_text()))


def audit(expected, collected, reports):
    if not collected or set(expected) != {node.split('[', 1)[0] for node in collected}:
        raise ValueError('required layout fixtures were missing or deselected')
    for node in collected:
        phases = reports.get(node, {})
        if phases != {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'}:
            raise ValueError(f'{node}: required layout check did not pass every phase: {phases}')


# Loaded only by the dedicated job, never by normal pytest or native UI runs.
def pytest_configure(config):
    config._design_reports = {}
    config._design_nodes = []


def pytest_collection_finish(session):
    session.config._design_nodes = [item.nodeid for item in session.items]


def pytest_sessionstart(session):
    session.config.pluginmanager.register(Reports(session.config), 'design-outcomes')


class Reports:
    def __init__(self, config):
        self.config = config

    def pytest_runtest_logreport(self, report):
        self.config._design_reports.setdefault(report.nodeid, {})[report.when] = report.outcome
        if report.failed or report.skipped:
            folder = ROOT / 'artifacts/design'
            folder.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(report.nodeid.encode()).hexdigest()[:16]
            (folder / (key + '.txt')).write_text(report.nodeid + '\n' + str(report.longrepr))


def pytest_sessionfinish(session, exitstatus):
    error = ''
    try:
        audit(selectors(), session.config._design_nodes, session.config._design_reports)
    except ValueError as exc:
        error = str(exc)
        session.exitstatus = 1
        print('\nDesign adherence: ' + error)
    folder = ROOT / 'artifacts/design'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'outcomes.json').write_text(json.dumps({
        'error': error, 'collected': session.config._design_nodes,
        'reports': session.config._design_reports}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['check', 'run'])
    args = parser.parse_args()
    nodes = selectors()
    if args.mode == 'check':
        print(f'Design layout inventory passed: {len(nodes)} required fixtures')
        return 0
    return subprocess.call([sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.design_check',
                            '--junitxml=artifacts/design/junit.xml', '--showlocals', *nodes], cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
