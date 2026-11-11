#!/usr/bin/env python3
"""Exercise the real launch hook while a disposable history database is locked."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not os.isatty(0):
        print('Run this preview in an interactive terminal.')
        return 1
    print('Startup preview · sample repository and history only')
    print('The real SessionStart hook will run while its history database is locked.')
    print('No agent, tmux session, network request or personal configuration is used.\n')
    with tempfile.TemporaryDirectory(prefix='t-startup-preview-') as temporary:
        root = Path(temporary)
        for key in ('HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME',
                    'XDG_DATA_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            folder = root / key
            folder.mkdir()
            os.environ[key] = str(folder)
        for key in list(os.environ):
            if key.startswith('COV_CORE_') or key in ('TMUX', 'TMUX_PANE', 'CLAUDE_CODE_SESSION_ID'):
                os.environ.pop(key)
        os.environ.update(T_HOME=str(ROOT), T_LOCAL_RC=str(root / 'empty-config'),
                          T_DISCOVERY_COLLECTOR='1', T_RECOVERY_DISABLE='1')
        repo = root / 'sample'
        repo.mkdir()
        for args in (['init', '-q'], ['config', 'user.name', 'Preview'],
                     ['config', 'user.email', 'preview@example.invalid'],
                     ['commit', '-q', '--allow-empty', '-m', 'Sample launch base']):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        history = runpy.run_path(str(ROOT / 'libexec/t_history.py'))
        store = history['Store']()
        try:
            store.db.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            result = subprocess.run([str(ROOT / 'bin/t-session-start'), '--agent', 'codex'],
                                    input=json.dumps({'session_id': 'sample-startup', 'cwd': str(repo)}),
                                    text=True, capture_output=True, timeout=10)
            elapsed = time.monotonic() - started
            stamp = Path(os.environ['XDG_CACHE_HOME']) / 'claude-sessions/opened/sample-startup'
            if result.returncode or not stamp.exists():
                raise RuntimeError('Hook did not publish the sample session identity: ' + result.stderr)
            print(f'Hook returned in {elapsed:.2f}s; sample identity is registered.')
            print('History is still locked. Startup has returned control anyway.')
            input('\nPress Return to release optional history work (Ctrl-C cancels the preview). ')
            store.db.rollback()
            deadline = time.monotonic() + 10
            while not store.records() and time.monotonic() < deadline:
                time.sleep(.05)
            if not store.records():
                raise RuntimeError('Deferred history did not finish within 10 seconds.')
            print('Background history completed with the original launch revision.')
            return 0
        finally:
            store.db.rollback()
            store.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nPreview cancelled; disposable history lock released.')
        raise SystemExit(130)
