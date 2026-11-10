#!/usr/bin/env python3
"""Preview actual eval commands with temporary sample configs and run results."""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('view', choices=('start', 'runs', 'config', 'benchmarks'), nargs='?', default='start')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='t-eval-preview-') as temp:
        original = dict(os.environ)
        try:
            os.environ.update(HOME=temp, CODEX_HOME=str(Path(temp)/'.codex'),
                              XDG_CONFIG_HOME=str(Path(temp)/'config'), XDG_STATE_HOME=str(Path(temp)/'state'),
                              XDG_CACHE_HOME=str(Path(temp)/'cache'), T_DEV_SOURCE_PIN='')
            m = runpy.run_path(str(ROOT/'libexec/t_eval.py'))
            for name, modes in (('quick-handoff', ['required-local']), ('quick-local', ['local-only'])):
                config = dict(m['defaults'](name), profile='quick', task_seconds=900, modes=modes)
                m['save_config'](config)
                journal = m['Journal'](m['create_run'](config))
                for i in range(3 if name == 'quick-local' else 1):
                    journal.finish_trial({'id':str(i), 'task':str(i), 'mode':modes[0], 'repetition':0,
                        'status':'completed','resolved':i!=1,'workflow_valid':True,'elapsed_seconds':60,
                        'cost_usd':0 if name=='quick-local' else .02,
                        'usage':{'local':{'requests':3,'output_tokens':900,'request_seconds':30},
                                 'frontier':{'requests':0 if name=='quick-local' else 1,'output_tokens':0 if name=='quick-local' else 120,'request_seconds':3}}})
                journal.checkpoint(status='completed' if name=='quick-local' else 'blocked',
                                   phase='finished' if name=='quick-local' else 'sample: runtime unavailable')
            print('SAMPLE DATA · temporary settings/results · starting is a dry run; no model calls', flush=True)
            command = {'start':['runs','start','--dry-run'], 'runs':['runs','list'],
                       'config':['config','list'], 'benchmarks':['benchmarks','list']}[args.view]
            env = {k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_')}
            return subprocess.call([str(ROOT/'bin/t'),'eval',*command], env=env)
        finally:
            os.environ.clear(); os.environ.update(original)


if __name__ == '__main__':
    raise SystemExit(main())
