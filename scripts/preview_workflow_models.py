#!/usr/bin/env python3
"""Worktree-only UI preview with sample data; no real configuration or inference."""
import argparse
import copy
import runpy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plain', action='store_true', help='Print the sample comparison without opening a picker')
    args = parser.parse_args()
    api = runpy.run_path(str(ROOT / 'bin/t'))
    local = runpy.run_path(str(ROOT / 'libexec/t_local_inference.py'))
    display = runpy.run_path(str(ROOT / 'libexec/t_model_display.py'))
    rows = [dict(row, provider='lmstudio', fit=True if i == 0 else None,
                 ready=i == 0, installed=i == 0) for i, row in enumerate(local['artifacts']())]
    models = display['local_models'](rows, 'swe-bench-pro', local['DEFAULT_MODEL'])
    print('Sample data · no configuration writes, provider calls or model downloads')
    if args.plain:
        print('\n'.join(display['lines'](models, 'swe-bench-pro')))
        return
    ui = api['_RailUI']()
    selected = copy.deepcopy(local['DEFAULT'])
    # The actual inference editor uses in-memory sample model choices only.
    def select_model(ui, current, context):
        choice = ui.pick('Local model', [(m['key'], m['model']) for m in models] + [('back', 'Back')],
                         model_table=models, benchmark_suite='swe-bench-pro')
        return {'model': choice, 'provider': 'lmstudio'} if choice and choice != 'back' else None
    local['workflow_menu'].__globals__['select_model'] = select_model
    cfg = SimpleNamespace(models={'codex':'sample-reasoning'}, efforts={}, fast={})
    catalog = {'codex': [dict(id=key, label=key, resolved=key, efforts=['low','medium','high'],
                            description=description, fast=False, ultrafast=False)
                         for key, description in [('sample-reasoning','Reasoning sample'),('sample-coding','Coding sample')]]}
    # Refresh must remain isolated too: restore the same sample catalogue.
    sample_catalog = copy.deepcopy(catalog['codex'])
    api['_config_model_menu'].__globals__['_config_live_models'] = lambda agent: []
    api['_config_model_menu'].__globals__['_config_catalog'] = lambda agent, rows: copy.deepcopy(sample_catalog)
    try:
        while True:
            choice = ui.pick('Workflow · sample', [('lead','Lead model'), ('local','Local model'),
                                                   ('inference','Inference & routing'), ('back','Exit preview')])
            if choice in (None, 'back'):
                break
            if choice == 'lead':
                api['_config_model_menu'](ui,cfg,'codex',catalog,role='Lead',quiet=True)
            elif choice == 'local':
                select_model(ui, selected['model'], selected['context_tokens'])
            else:
                selected = local['workflow_menu'](ui,selected)
    finally:
        ui.restore()


if __name__ == '__main__':
    main()
