"""Session display file-cache compatibility through the managed cache API."""
import json
import shlex
import runpy
import sys
import subprocess
import time
from pathlib import Path

request = runpy.run_path(str(Path(__file__).with_name('t_session_cache.py')))['request']


def record(path):
    return request('artifact', {'key': 'display-file:' + str(path)}).get('value') or {}


def read(path):
    return record(path).get('text', '')


def write(path, text):
    request('artifact', {'key': 'display-file:' + str(path),
                         'value': dict(text=text, checked=time.time())})


def files(prefix):
    values = request('artifacts', {'prefix': 'display-file:' + str(prefix)}).get('values', {})
    return {key.removeprefix('display-file:'): value for key, value in values.items()}


def evidence(kind, sid):
    return request('display-evidence', {'kind': kind, 'sid': sid})


def main():
    action, path, *values = sys.argv[1:]
    if action == 'opened-dump':
        values = request('display-evidence', {'kind': 'opened', 'all': True}).get('values', {})
        for sid, evidence in values.items():
            print('_OPENED_CACHE+=(' + shlex.quote(sid) + ' ' + str(int(evidence.get('checked', 0))) + ')')
    elif action == 'opened':
        print(evidence('opened', path).get('checked', 0))
    elif action == 'rollout':
        print(evidence('rollout', path).get('text', ''), end='')
    elif action == 'dump':
        status = request('status')
        if status.get('unavailable'):
            return 1
        for name, evidence in files(path).items():
            print('_PR_CACHE_TEXT+=(' + shlex.quote(name) + ' ' + shlex.quote(evidence.get('text', '')) + ')')
            print('_PR_CACHE_TIME+=(' + shlex.quote(name) + ' ' + str(int(evidence.get('checked', 0))) + ')')
    elif action == 'list':
        print('\n'.join(files(path)))
    elif action == 'write':
        write(path, values[0] if values else sys.stdin.read())
    elif action == 'refresh':
        if request('status').get('unavailable'):
            return 1
        result = subprocess.run(['gh', 'pr', 'view', path, '--json', 'state', '--jq', '.state'],
                                capture_output=True, text=True, timeout=15)
        write(values[0], result.stdout.strip() if not result.returncode else '?')
    elif action == 'exists':
        return int(not bool(record(path)))
    elif action == 'mtime':
        print(int(record(path).get('checked', 0)))
    elif action == 'read':
        print(read(path), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
