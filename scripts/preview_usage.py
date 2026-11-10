#!/usr/bin/env python3
"""Isolated working-command preview. Run from a terminal; no inference or config writes."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='t-usage-preview-') as folder:
        for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'CODEX_HOME'):
            os.environ[key] = str(Path(folder) / key)
        loader = importlib.machinery.SourceFileLoader('t_usage_preview_cli', str(ROOT / 'bin/t'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        api = importlib.util.module_from_spec(spec); loader.exec_module(api)
        rows = []
        for slot, title, local, cloud, lead, models, state in (
            ('t-12', 'Parser fix', 69, 31, 'gpt-6-astra', ['gpt-6-astra', 'Qwen3.8-27B'], 'attached'),
            ('t-13', 'Config cleanup #381 merged, still in progress', 67, 33, 'gpt-6-astra', ['gpt-6-astra', 'Qwen3.8-27B'], 'attached'),
            ('t-14', 'Offline continuation', 100, 0, 'Qwen3.8-27B', ['Qwen3.8-27B'], 'detached')):
            rows.append(dict(slot=slot, summary=title, agent='codex', state=state, context='active',
                             model=lead, last_turn_started_at='2026-11-10T17:24:25Z',
                             usage={'lead_model': lead, 'models':[{'model':m} for m in models], 'local_cloud_percent':[local,cloud]}))
        print('SAMPLE DATA · actual t ls renderer · isolated state\n$ t ls')
        api._render_local(rows, '', api.Style())
        print('\nCommands: t system usage show · t eval runs list')
        print('LOC/CLD TOKEN% describes observed tokens; no real inference ran.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
