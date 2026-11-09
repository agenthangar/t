#!/usr/bin/env python3
"""Worktree-only recovery picker preview; no agents, network, or configuration writes."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('Run this preview in an interactive terminal.', file=sys.stderr)
        return 1
    recovery = runpy.run_path(str(ROOT/'libexec/t_offline.py'))
    app = recovery['ui_helpers']()
    # This lookup only supplies sample models to the cloud picker; it never starts a CLI.
    recovery['choose_cloud'].__globals__['ui_helpers'] = lambda: dict(app,
        _config_live_models=lambda tool: [{'model': 'sample-cloud-model'}])
    chosen = {'provider': 'lmstudio', 'model': 'sample/local-27b', 'context_tokens': 32768}
    samples = [dict(model=chosen['model'], provider='lmstudio', fit=True, ready=True),
               dict(model='sample/local-8b', provider='ollama', fit=True, ready=True),
               dict(model='sample/local-70b', provider='ollama', fit=False, ready=False,
                    reason='Sample model not installed; exceeds sample memory budget.')]
    try:
        with recovery['recovery_ui']() as ui:
            selected = recovery['choose_local'](ui, {'catalogue': lambda **kw: {'models': samples}},
                                                chosen, 'SAMPLE DATA · Simulated cloud connection loss')
        if selected is None:
            print('Sample task paused. No model started.')
            return 0
        print('Sample native session resumed · Local '+selected['model'])
        print('The picker is dismissed. No model is running and no configuration changed.')
        print('Type r to simulate connectivity returning, f for a failed local start, or Return to exit.')
        action = input('Preview event: ').strip().lower()
        if action in ('r', 'f'):
            with recovery['recovery_ui']() as ui:
                if action == 'r':
                    result = recovery['choose_cloud'](ui, 'sample-cloud-model')
                else:
                    result = recovery['choose_local'](ui, {'catalogue': lambda **kw: {'models': samples}},
                        chosen, 'SAMPLE ERROR · Local runtime unavailable. Checkpoint retained.', failed=True)
            print('Sample selection: '+str(result or 'Keep current mode / pause'))
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\nPreview cancelled.')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
