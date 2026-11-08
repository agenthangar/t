#!/usr/bin/env python3
"""Isolated, interactive preview of the real tmux activity renderer (sample data)."""
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.argv[1:] == ['--pane']:
        status = runpy.run_path(str(ROOT / 'libexec/t_local_status.py'))
        print('Local status preview · simulated activity · no model calls or configuration changes')
        print('Name and worktree appear at the left; model activity appears at the right.')
        print('Ctrl-C exits the disposable preview.')
        with status['Activity']('sample/Qwen3.8-27B-8bit') as activity:
            try:
                while True:
                    for state in ('loading', 'inferring', 'working', 'inferring', 'idle', 'failed'):
                        activity.update(state)
                        print(state, flush=True)
                        time.sleep(4)
            except KeyboardInterrupt:
                return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Run this preview in an interactive terminal.', file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix='t-status-preview-') as temp:
        socket = str(Path(temp) / 'tmux.sock')
        env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_') and k not in ('TMUX', 'TMUX_PANE')}
        env.update(HOME=temp, XDG_STATE_HOME=str(Path(temp) / 'state'))
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--pane'])
        try:
            subprocess.run(['tmux', '-S', socket, '-f', '/dev/null', 'new-session', '-d', '-s', 'Local inference', '-n', 'Local inference', command], env=env, check=True)
            return subprocess.call(['tmux', '-S', socket, 'attach-session', '-t', '=Local inference'], env=env)
        finally:
            subprocess.run(['tmux', '-S', socket, 'kill-server'], env=env, capture_output=True)


if __name__ == '__main__':
    raise SystemExit(main())
