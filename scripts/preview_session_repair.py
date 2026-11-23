#!/usr/bin/env python3
"""Preview the real repair rail with isolated settings and simulated actions."""
import argparse
import os
from pathlib import Path
import runpy
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('open', 'resume'), nargs='?', default='resume')
    parser.add_argument('--blocked', action='store_true', help='simulate ownership staying unresolved')
    args = parser.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('Run this preview in an interactive terminal.')
        return 1
    with tempfile.TemporaryDirectory(prefix='t-repair-preview-') as folder:
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
                    'XDG_DATA_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            path = Path(folder) / key
            path.mkdir()
            os.environ[key] = str(path)
        os.environ.pop('T_LOCAL_RC', None)
        os.environ['T_NO_UPDATE_CHECK'] = '1'
        api = runpy.run_path(str(ROOT / 'bin/t'), run_name='repair_preview')
        recovery = runpy.run_path(str(ROOT / 'libexec/t_open_recovery.py'))
        print('Sample data · no agent, session, or configuration changes.\n')
        print('$ t ' + args.command + ' api 3')
        if args.command == 'resume':
            history = recovery['HISTORY']
            target = object.__new__(history['HistoryCommand'])
            target.root, target.api = str(ROOT), api
            target.fresh_resume_offer = lambda *_: ('a9db407' + '0' * 33, None)
            attempts = []
            def resume(*_args, fresh=None):
                attempts.append(True)
                if fresh:
                    return 'Opened the selected sample conversation in a fresh worktree.'
                if len(attempts) == 1 or args.blocked:
                    error = history['WriterBusy']('Another writer has not published its worktree yet.')
                    error.fingerprint = None
                    raise history['UnknownWorkspaceWriter'](error, '~/code/.worktrees/api/api-3')
                return 'Resumed the exact selected sample conversation.'
            target.resume = resume
            target.resume_with_repair.__globals__['_CACHE_REQUEST'] = lambda *a, **k: {}
            try:
                print(target.resume_with_repair('api', {'sid': 'sample-conversation'}))
            except history['HistoryCancelled']:
                print('Cancelled. No further sample selections attempted.')
            except history['HistoryError'] as error:
                print('Repair failed: ' + str(error))
        else:
            ask = recovery['ask']
            if not ask(api, 'Could not open api 3', 'Try repairing this session.', 'Repair'):
                print('Cancelled.')
            elif not args.blocked:
                print('Repaired and opened sample api 3 with its selected agent.')
            elif ask(api, 'Force open', 'Sample ownership is still unresolved.', 'Force open'):
                print('Force open blocked: the writer must be identified or released first.')
            else:
                print('Cancelled.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
