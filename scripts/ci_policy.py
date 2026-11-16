"""Routine CI uses bounded smoke checks; only explicit full runs gate releases."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def check_results(results, *, full, macos=False, reference=False):
    optional = {'smoke'} if full else {'pytest', 'fzf-legacy', 'coverage'}
    if not macos:
        optional.add('macos')
    if not reference:
        optional.update(('reference', 'verify-shards'))
    required = {'scope', 'cli-contract', 'design-adherence', 'shell', 'pytest', 'smoke', 'fzf-legacy',
                'coverage', 'reference', 'verify-shards', 'macos', 'playground',
                'gitleaks', 'pip-audit', 'codeql'}
    if set(results) != required:
        raise ValueError('missing or unexpected CI job results')
    for name, job in results.items():
        expected = 'skipped' if name in optional else 'success'
        if job['result'] != expected:
            raise ValueError(f"{name}: {job['result']} (expected {expected})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['scope', 'gate'])
    args = parser.parse_args()
    if args.command == 'scope':
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        full = os.environ.get('FORCE_FULL') == 'true'
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'sha={sha}\nfull={str(full).lower()}\n')
        print(f"{'Full Linux validation' if full else 'Essential smoke checks'}: {sha}")
    else:
        full = os.environ['EXPECT_FULL'] == 'true'
        check_results(json.loads(os.environ['NEEDS_JSON']), full=full,
                      macos=os.environ['EXPECT_MACOS'] == 'true',
                      reference=os.environ['EXPECT_REFERENCE'] == 'true')
        # Only a successful full gate produces release evidence. The artifact is
        # consumed in this same workflow attempt, never from a PR or older run.
        if full:
            Path('validation').mkdir(exist_ok=True)
            Path('validation/validation.json').write_text(json.dumps({
                'sha': os.environ['TESTED_SHA'], 'full': True,
                'run_id': os.environ['GITHUB_RUN_ID'],
                'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
            }) + '\n')


if __name__ == '__main__':
    main()
