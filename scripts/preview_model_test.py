#!/usr/bin/env python3
"""Exercise the real model-test UI with isolated sample owners and no runtime."""
from pathlib import Path
import runpy
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('Run this preview in an interactive terminal.', file=sys.stderr)
        return 1
    app = runpy.run_path(str(ROOT / 'bin/t'))
    preflight = runpy.run_path(str(ROOT / 'libexec/t_model_preflight.py'))
    runtime = runpy.run_path(str(ROOT / 'libexec/t_local_runtime_ui.py'))
    targets = [dict(socket='/sample/socket', pane='%' + str(n), sid='sample-' + str(n),
                    pid=n, pane_pid=str(n), agent='codex', session='dev-t-' + str(n)) for n in (24, 18)]
    owners = [dict(id=str(t['pid']), pid=t['pid'], identity='sample', model=model,
                   kind='worker', tmux=t['socket'], pane=t['pane']) for t, model in zip(targets,
                   ('Qwen3.8-27B-8bit', 'Qwen3.8-Flash-Next-MLX-4bit'))]
    state = {'interrupted': None}
    def rpc(action, **kwargs):
        if action == 'test-preflight':
            return dict(generation='sample', owners=owners, legacy=[], reserved=False)
        if action == 'test-interrupt':
            state['interrupted'] = time.monotonic()
        return dict(token='sample', ready=state['interrupted'] is not None and time.monotonic() - state['interrupted'] > 2)
    manager = dict(ensure=lambda **kw: {'test_preflight': True}, rpc=rpc, process_identity=lambda pid:'sample')
    recovery = dict(panes=lambda socket:targets, descendant=lambda pid,parent:str(pid)==str(parent),
                    owner=lambda socket,pane:pane, current=lambda target:True,
                    tmux=lambda *args:SimpleNamespace(returncode=0))
    preflight['prepare'].__globals__['helper'] = lambda name: manager if name == 't_local_manager' else recovery
    preflight['snapshot'].__globals__['legacy_owners'] = lambda *args:[]
    original = runtime['helper']
    runtime['run_model_action'].__globals__['helper'] = lambda name: preflight if name == 't_model_preflight' else original(name)
    runtime['run_model_action'].__globals__['manager'] = lambda:{'status':lambda:{'requests':[]}}
    def operation(cancel):
        if cancel.wait(3):
            raise ValueError('Sample test cancelled')
        return {'ready': True}
    print('SAMPLE DATA · Simulated sessions and model test. No real sessions or settings are touched.')
    ui = app['_RailUI']()
    try:
        ui.raw()
        runtime['run_model_action'](ui, operation, 'sample/Qwen3.8-Flash-Next-MLX-4bit', preflight=True)
    except KeyboardInterrupt:
        return 130
    finally:
        ui.restore()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
