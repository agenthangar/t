#!/usr/bin/env python3
"""Preview real cached session-list rendering with isolated, synthetic data.

No SSH, tmux, agent launch, real configuration, or persistent background process.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('warm', 'expired', 'offline', 'cold'), default='warm')
    parser.add_argument('--wait', action='store_true', help='preview explicit bounded refresh observation')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='t-cache-preview-', dir='/tmp') as temporary:
        previous = dict(os.environ)
        os.environ.update(HOME=temporary, XDG_CONFIG_HOME=temporary + '/config',
                          XDG_CACHE_HOME=temporary + '/cache', XDG_STATE_HOME=temporary + '/state',
                          CODEX_HOME=temporary + '/codex', CLAUDE_CONFIG_DIR=temporary + '/claude',
                          T_LOCAL_RC=temporary + '/local.zsh', T_HOME=str(ROOT), T_NO_UPDATE_CHECK='1')
        os.environ.pop('T_DISCOVERY_COLLECTOR', None)
        for key in list(os.environ):
            if key.startswith('COV_CORE_'):
                os.environ.pop(key)
        cache = runpy.run_path(str(ROOT / 'libexec/t_session_cache.py'))
        api = runpy.run_path(str(ROOT / 'bin/t'), run_name='preview')
        cfg = api['Config']()
        adapter = api['_session_discovery']()
        jobs = []
        memory = cache['_Memory'](collect=lambda spec: {
            'rows': 'sample\t/demo/api\tapi-2\tdetached\tactive\tReview retry behavior\tcodex\tgpt-6.1-sol\t2026-11-10T12:00:00Z\n',
            'resume': []}, submit=lambda *job: jobs.append(job))
        memory.restore({})
        original = adapter['discover'].__globals__['request']
        adapter['discover'].__globals__['request'] = memory.handle
        adapter['discover'](cfg)
        adapter['discover'].__globals__['request'] = original
        if args.scenario != 'cold':
            callback, *values = jobs.pop()
            callback(*values)
            for entry in memory.entries.values():
                if args.scenario in ('expired', 'offline'):
                    entry['checked'] -= 3601
                    entry['retry'] = 0
                if args.scenario == 'offline':
                    entry['error'] = 'Sample host unavailable'
                    entry['retry'] = memory.clock() + 3600
        # Subsequent refreshes finish asynchronously using only the synthetic
        # collector, so --wait exercises completion without real discovery.
        def refresh_later(callback, *values):
            timer = threading.Timer(.5, callback, args=values)
            timer.daemon = True
            timer.start()
        memory.submit = refresh_later
        for callback, *values in jobs:
            refresh_later(callback, *values)
        address = cache['_private'](cache['_runtime']()) / 'service.sock'
        try:
            with cache['_Server'](str(address), cache['_Handler']) as server:
                server.memory = memory
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    print('Synthetic preview · t session list --local · ' + args.scenario, flush=True)
                    return subprocess.run([sys.executable, str(ROOT / 'bin/t'), 'session', 'list', '--local'] +
                                          (['--wait'] if args.wait else []),
                                          cwd=temporary, env=dict(os.environ), check=False).returncode
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            address.unlink(missing_ok=True)
            address.parent.rmdir()
            os.environ.clear()
            os.environ.update(previous)


if __name__ == '__main__':
    raise SystemExit(main())
