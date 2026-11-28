#!/usr/bin/env python3
"""Run the shared smoke selection on the configured snapshot runner; check it before pushing.

Commit first, then run with a Python that has requirements-dev.txt installed.
Evidence lives in this worktree's Git directory, never in published source.
"""
import argparse
import json
import re
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()


def evidence_path():
    return Path(git('rev-parse', '--path-format=absolute', '--git-path', 't-smoke.json'))


def snapshot():
    helper = runpy.run_path(str(ROOT / 'libexec/t_runner_job.py'))
    with tempfile.TemporaryDirectory(prefix='t-smoke-identity-') as directory:
        return helper['create_snapshot'](ROOT, Path(directory) / 'source.tar',
                                         Path(directory) / 'manifest.json')


def runner_config():
    cli = runpy.run_path(str(ROOT / 'bin/t'))
    cfg = cli['Config']()
    canonical = cli['_t_canonical_root'](str(ROOT))
    if canonical not in {str(Path(path).resolve()) for path in cfg.repos.values()}:
        return {'default_targets': ['local'], 'targets': {'local': {'type': 'local'}}}
    return cli['_runner_policy'](cfg, argparse.Namespace(), canonical)[1]


def check_evidence(receipt, current, expected=None):
    expected = runner_config()['targets'] if expected is None else expected
    tested = receipt.get('snapshot', {})
    targets = receipt.get('targets', {})
    command = receipt.get('command', [])
    local = set(expected) == {'local'}
    valid_command = (len(command) == 2 and command[1] == 'scripts/smoke.py'
                     and (re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', Path(command[0]).name)
                          if local else command[0] == 'python3'))
    if (receipt.get('status') != 'succeeded' or receipt.get('exit_code') != 0
            or not expected or set(expected) != set(targets) or not valid_command
            or not tested.get('id') or tested.get('source_head') != current['source_head']
            or tested.get('files') != current['files']):
        raise ValueError('missing, failed or stale smoke evidence; rerun scripts/local_smoke.py')
    for name, spec in expected.items():
        target = targets[name]
        identity = ('type', 'host', 'repository', 'label')
        if (any(target.get(k) != spec.get(k) for k in identity)
                or target.get('snapshot_id') != tested['id']
                or target.get('status') != 'succeeded' or target.get('exit_code') != 0):
            raise ValueError('failed or stale smoke target evidence; rerun scripts/local_smoke.py')


def check_push(lines):
    updates = [line.split() for line in lines if line.strip()]
    branches = [row for row in updates if row[2].startswith('refs/heads/') and set(row[1]) != {'0'}]
    if not branches:
        return
    head = git('rev-parse', 'HEAD')
    if any(row[1] != head for row in branches):
        raise ValueError('push each branch from its tested worktree at HEAD')
    # A snapshot of dirty edits cannot authorize pushing the older committed tree.
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('commit tracked edits before running local smoke and pushing')
    check_evidence(json.loads(evidence_path().read_text()), snapshot())
    print('Smoke evidence matches pushed commit ' + head)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ['check-push']:
        check_push(sys.stdin)
        return 0
    if args:
        raise ValueError('usage: python scripts/local_smoke.py [check-push]')
    # Invalidate the previous receipt before any provisioning or test failure.
    evidence_path().unlink(missing_ok=True)
    runner = runpy.run_path(str(ROOT / 'libexec/t_runners.py'))
    config = runner_config()
    targets = config['default_targets']
    python = sys.executable if targets == ['local'] else 'python3'
    receipt = runner['start'](
        ROOT, config, [python, 'scripts/smoke.py'], targets=targets,
        artifacts=['artifacts/smoke'], timeout=config.get('timeout_seconds', 300),
        progress=lambda value: print('Smoke run: ' + value['id'], flush=True))
    evidence_path().write_text(json.dumps(receipt) + '\n')
    print(json.dumps({key: receipt.get(key) for key in ('id', 'status', 'exit_code')}, indent=2))
    if receipt.get('status') != 'succeeded':
        print('Inspect retained evidence with: t runners runs inspect ' + receipt['id'])
        return 1
    check_evidence(receipt, snapshot(), config['targets'])
    print('Tested commit: ' + receipt['snapshot']['source_head'])
    print('Tested snapshot: ' + receipt['snapshot']['id'])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('Smoke gate: ' + str(error), file=sys.stderr)
        sys.exit(1)
