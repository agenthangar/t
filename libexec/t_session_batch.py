"""Run independent session preparations concurrently and retain each result."""
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def run_jobs(commands):
    results = [None] * len(commands)
    running = {}
    env = {**os.environ, 'T_RESUME_BATCH': '1', 'T_DEV_CONTEXT': str(ROOT),
           'T_DEV_CONTEXT_OWNER': str(os.getpid())}
    with tempfile.TemporaryDirectory(prefix='t-batch-output-') as name:
        directory = Path(name)
        streams = []
        try:
            for index, command in enumerate(commands):
                output = (directory/f'{index}.out').open('w+')
                errors = (directory/f'{index}.err').open('w+')
                streams.extend((output, errors))
                try:
                    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=errors, text=True, start_new_session=True)
                    running[index] = (process, output, errors)
                except OSError as exc:
                    results[index] = {'code': 1, 'output': '', 'error': str(exc)}
            completed = -1
            while running:
                for index, (process, output, errors) in list(running.items()):
                    code = process.poll()
                    if code is None:
                        continue
                    output.seek(0)
                    errors.seek(0)
                    results[index] = {'code': code, 'output': output.read(), 'error': errors.read()}
                    del running[index]
                count = len(commands)-len(running)
                if sys.stderr.isatty() and count != completed:
                    print(f'\rResuming {count}/{len(commands)}', end='', file=sys.stderr, flush=True)
                    completed = count
                if running:
                    time.sleep(.05)
        except KeyboardInterrupt:
            for process, _, _ in running.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
            for index, (process, output, errors) in running.items():
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                output.seek(0)
                results[index] = {'code': 130, 'output': output.read(), 'error': 'Resume cancelled'}
        finally:
            for stream in streams:
                stream.close()
            if sys.stderr.isatty() and commands:
                print(file=sys.stderr)
    return results


def prepare(requests, kind):
    with tempfile.TemporaryDirectory(prefix='t-session-batch-') as name:
        directory = Path(name)
        commands = []
        for index, request in enumerate(requests):
            path = directory/f'{index}.json'
            path.write_text(json.dumps(request))
            path.chmod(0o600)
            commands.append([sys.executable, str(Path(__file__)), kind, str(path)])
        return run_jobs(commands)


def worker(kind, path):
    request = json.loads(path.read_text())
    api = runpy.run_path(str(ROOT/'bin/t'))
    cfg = api['Config']()
    sys.path.insert(0, str(ROOT/'libexec'))
    if kind == 'history':
        from t_history import HistoryCommand
        owner = HistoryCommand(cfg, api).resume_with_repair(request['alias'], request['row'], return_owner=True)
        print(json.dumps(owner))
    else:
        from t_recovery_picker import selected_command
        print(json.dumps(selected_command(request, cfg, api)))


if __name__ == '__main__':
    try:
        kind, name = sys.argv[1:]
        directory = Path(name)
        if kind == 'scripts':
            scripts = sorted((p for p in directory.glob('*.zsh') if p.stem.isdigit()), key=lambda p: int(p.stem))
            results = run_jobs([['zsh', str(path)] for path in scripts])
            for path, result in zip(scripts, results):
                path.with_suffix('.out').write_text(result['output'])
                path.with_suffix('.err').write_text(result['error'])
                path.with_suffix('.status').write_text(str(result['code']))
            sys.exit(130 if any(r['code'] == 130 for r in results) else 0)
        worker(kind, directory)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
