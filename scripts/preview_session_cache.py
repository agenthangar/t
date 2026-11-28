#!/usr/bin/env python3
"""Try session cache management or list rendering with disposable sample data."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def sandbox():
    with tempfile.TemporaryDirectory(prefix='t-cache-preview-', dir='/tmp') as temporary:
        env = {key: os.environ[key] for key in ('PATH', 'TERM', 'LANG', 'LC_ALL', 'COLUMNS', 'LINES',
                                                'COLORTERM', 'NO_COLOR') if key in os.environ}
        env.update(HOME=temporary, XDG_CONFIG_HOME=temporary + '/config',
                   XDG_CACHE_HOME=temporary + '/cache', XDG_STATE_HOME=temporary + '/state',
                   XDG_RUNTIME_DIR=temporary, TMPDIR=temporary, TMUX_TMPDIR=temporary,
                   ZDOTDIR=temporary, CODEX_HOME=temporary + '/codex', CLAUDE_CONFIG_DIR=temporary + '/claude',
                   T_LOCAL_RC=temporary + '/local.zsh', T_HOME=str(ROOT), T_NO_UPDATE_CHECK='1')
        original = os.getcwd()
        with patch.dict(os.environ, env, clear=True):
            os.chdir(temporary)
            try:
                yield temporary
            finally:
                os.chdir(original)


def manage():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Run this preview in an interactive terminal.', file=sys.stderr)
        return 2
    print('Sample settings. Open Background agents, then Session cache. Service actions are simulated.', flush=True)
    api = runpy.run_path(str(ROOT / 'bin/t'), run_name='preview')
    wizard = api['_config_wizard']
    background = api['_background_agents']()
    running = False
    sample_pid = 4100

    def status(agent):
        if agent == 'cache':
            return ('Installed', 'Running' if running else 'Stopped', 'Disabled')
        return ('Not installed', 'Stopped', 'Disabled')

    def command(action, target, reinstall, interval):
        nonlocal running, sample_pid
        if target != 'cache' or action not in ('start', 'stop', 'restart'):
            raise ValueError('Unknown sample service action')
        running = action != 'stop'
        if running:
            sample_pid += 1
        print('cache: ' + {'start': 'started', 'stop': 'stopped', 'restart': 'restarted'}[action])
        return 0

    def metrics(operation):
        return dict(pid=sample_pid, bytes_written_last_minute=4096) if running else {}

    def external_disabled(*args, **kwargs):
        raise OSError('External commands are disabled in this preview')

    backend = dict(background, status=status, command=command)
    with (patch.dict(wizard.__globals__, {'_background_agents': lambda: backend}),
          patch.dict(background['cache_details'].__globals__, {'cache_request': metrics}),
          patch.object(subprocess, 'Popen', external_disabled)):
        wizard(api['Config']())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manage', action='store_true', help='try the Session cache controls in t config')
    parser.add_argument('--scenario', choices=('warm', 'expired', 'offline', 'cold'), default='warm')
    parser.add_argument('--wait', action='store_true', help='preview explicit bounded refresh observation')
    args = parser.parse_args()
    with sandbox() as temporary:
        if args.manage:
            return manage()
        cache = runpy.run_path(str(ROOT / 'libexec/t_session_cache.py'))
        api = runpy.run_path(str(ROOT / 'bin/t'), run_name='preview')
        cfg = api['Config']()
        adapter = api['_session_discovery']()
        jobs = []
        memory = cache['_Memory'](collect=lambda spec: {
            'rows': 'sample\t/demo/api\tapi-2\tdetached\tactive\tReview retry behavior\tcodex\tgpt-6.1-sol\t2026-11-10T12:00:00Z\n',
            'resume': [], 'catalog': {'version': 1, 'repositories': [], 'conversations': []}},
            submit=lambda *job: jobs.append(job))
        memory.restore({})
        if args.scenario != 'cold':
            with patch.dict(adapter['discover'].__globals__, {'request': memory.handle}):
                adapter['discover'](cfg)
            callback, *values = jobs.pop()
            callback(*values)
            for entry in memory.entries.values():
                if args.scenario in ('expired', 'offline'):
                    entry['checked'] -= cache['TTL'] + 1
                    entry['observed'] = entry['checked']
                    entry['retry'] = 0
                if args.scenario == 'offline':
                    entry['error'] = 'Sample host unavailable'
                    entry['retry'] = memory.clock() + 3600
                    memory.failures[entry['spec']['target']] = dict(
                        error=entry['error'], retry_at=entry['retry'], kind='transport')
        # Subsequent refreshes finish asynchronously using only the synthetic
        # collector, so --wait exercises completion without real discovery.
        if args.scenario == 'offline':
            def unavailable(spec):
                raise cache['DiscoveryError']('Sample host unavailable', 'transport')
            memory.collect = unavailable
        timers = []
        def refresh_later(callback, *values):
            timer = threading.Timer(.5, callback, args=values)
            timer.daemon = True
            timers.append(timer)
            timer.start()
        memory.submit = refresh_later
        address = cache['_private'](cache['_runtime']()) / 'service.sock'
        try:
            with cache['_Server'](str(address), cache['_Handler']) as server:
                server.make_memory = lambda identity: memory
                server.configure()
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    print(f'Sample data ({args.scenario}). Running t session list --local' +
                          (' --wait' if args.wait else ''), flush=True)
                    return subprocess.run([sys.executable, str(ROOT / 'bin/t'), 'session', 'list', '--local'] +
                                          (['--wait'] if args.wait else []),
                                          cwd=temporary, env=dict(os.environ), check=False).returncode
                finally:
                    server.shutdown()
                    thread.join()
                    for timer in timers:
                        timer.cancel()
                        timer.join()
        finally:
            address.unlink(missing_ok=True)
            address.parent.rmdir()


if __name__ == '__main__':
    raise SystemExit(main())
