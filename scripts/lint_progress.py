#!/usr/bin/env python3
"""Require a progress policy for every Python blocking site and shell wait seam."""
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
BLOCKING = {'subprocess.run', 'subprocess.Popen', 'subprocess.call',
            'subprocess.check_call', 'subprocess.check_output', 'urlopen',
            'urllib.request.urlopen', 'time.sleep', 'select.select'}


def sites(source, phase_calls=()):
    result = []

    def walk(node, scope='', protected=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = f'{scope}.{node.name}' if scope else node.name
            protected = any(isinstance(d, ast.Call) and ast.unparse(d.func) == '_spinner'
                            for d in node.decorator_list)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            protected = protected or any(isinstance(i.context_expr, ast.Call)
                                        and ast.unparse(i.context_expr.func) == '_spinner'
                                        for i in node.items)
        if isinstance(node, ast.Call):
            call = ast.unparse(node.func)
            if call in BLOCKING or (scope, call) in phase_calls:
                result.append((scope, call, protected, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, scope, protected)
    walk(ast.parse(source))
    return result


def check(root=ROOT):
    contract = json.loads((root / 'ui/progress-contract.json').read_text())
    exceptions = contract['exceptions']
    phases = {}
    for path, scope, call in contract.get('phases', []):
        phases.setdefault(path, set()).add((scope, call))
    seen = set()
    seen_phases = set()
    errors = []
    for path in [root / 'bin/t', *sorted((root / 'libexec').glob('*.py'))]:
        relative = path.relative_to(root).as_posix()
        phase_calls = phases.get(relative, ())
        for scope, call, protected, line in sites(path.read_text(), phase_calls):
            key = f'{relative}:{scope}:{call}'
            if (scope, call) in phase_calls:
                seen_phases.add(key)
                if not protected:
                    errors.append(f'{key}:{line}: collection phase needs _spinner')
            if call not in BLOCKING:
                continue
            if protected:
                if key in exceptions:
                    errors.append(f'{key}: remove obsolete progress exception')
                continue
            seen.add(key)
            policy = exceptions.get(key, {})
            if policy.get('kind') not in {'passthrough', 'background', 'protocol', 'phase'} or not policy.get('reason'):
                errors.append(f'{key}:{line}: blocking work needs _spinner or a reviewed progress exception')
    for key in exceptions.keys() - seen:
        errors.append(f'{key}: stale progress exception')
    for path, phase_calls in phases.items():
        for scope, call in sorted(phase_calls):
            key = f'{path}:{scope}:{call}'
            if key not in seen_phases:
                errors.append(f'{key}: stale collection phase')
    for entry in contract['shell']:
        source = (root / entry['file']).read_text()
        for snippet in entry['required']:
            if snippet not in source:
                errors.append(f"{entry['file']}: missing progress seam {snippet}")
    return errors


def main():
    errors = check()
    for error in errors:
        print('Progress contract: ' + error, file=sys.stderr)
    if not errors:
        print('Long-running operation progress contract passed')
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
