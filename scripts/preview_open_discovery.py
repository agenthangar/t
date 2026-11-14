#!/usr/bin/env python3
"""Preview t open discovery with sample rows and no real sessions or connections."""
import argparse
import importlib.util
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['complete', 'timeout', 'offline', 'empty'], default='complete')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('preview_discovery', ROOT / 'libexec/t_session_discovery.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    api = runpy.run_path(str(ROOT / 'bin/t'), run_name='preview')
    rows = ('old\t/demo/api\tapi-2\tdetached\tactive\tExisting sample task\tcodex\n'
            'new\t/demo/api\tapi-10\tattached\tactive\tNew attached sample\tcodex\n')
    started = time.monotonic()
    retry = False
    def discover(*_args, **_kwargs):
        pending = not retry and (args.scenario == 'timeout' or
                                  args.scenario == 'complete' and time.monotonic() - started < .7)
        current = rows.splitlines()[0] + '\n' if pending else rows
        if args.scenario == 'empty':
            current = ''
        hosts = [dict(host='local', target='local', checked=time.time(), refreshing=pending,
                      error='', value={'rows': current}, retry_at=0)]
        if args.scenario == 'offline':
            hosts.append(dict(host='mini', target='sample.invalid', checked=0, refreshing=False,
                              error='Sample host unavailable', value=None, retry_at=time.time() + 3600))
        return dict(hosts=hosts, loading=False)
    adapter.discover = discover
    adapter.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('Preview attempted a real cache action'))
    observe = adapter.wait_for_refresh
    adapter.wait_for_refresh = lambda *a, **kw: observe(*a, timeout=1, **kw)
    def choose(message, available):
        nonlocal retry
        result = api['_open_discovery_choice'](message, available)
        if result == 'retry':
            retry = True
        return result
    print('SAMPLE ONLY · t open · refresh wait shortened to one second')
    print('Ctrl-F shows the attached sample after refresh. Esc exits without opening anything.\n')
    try:
        view, notice = adapter.open_snapshot(SimpleNamespace(hosts={}), choose)
    except KeyboardInterrupt:
        return 130
    adapter.announce(view)
    source = adapter.rows(view)
    if not source:
        print('Verified empty sample: the existing fresh-session path would continue. No session launched.')
        return 0
    with tempfile.TemporaryDirectory(prefix='t-discovery-preview-') as directory:
        env = dict(os.environ, HOME=directory, XDG_CONFIG_HOME=directory, XDG_CACHE_HOME=directory,
                   XDG_STATE_HOME=directory, T_HOME=str(ROOT), T_PICKER_SOURCE_ROWS=source,
                   T_PICKER_SESSION_FILTERS='open', T_PICKER_NOTICE=notice)
        env.pop('T_PICKER_KILL_CALLBACK', None)
        formatted = subprocess.run(['python3', str(ROOT / 'bin/t'), '__session-picker-rows', '--remote',
                                    '--compact', '--header', '--last-turn'], input=source,
                                   env=env, text=True, capture_output=True, check=True).stdout
        columns, _, formatted = formatted.partition('\n')
        env['T_PICKER_COLUMNS'] = columns
        root = Path(directory)
        (root / 'all').write_text(formatted)
        (root / 'default').write_text(''.join(line + '\n' for line in formatted.splitlines()
            if line.split('\t')[2] != 'attached'))
        script = '. "$T_HOME/ui/fzf.sh"; _t_fzf_filtered "$(cat "$HOME/default")" "$(cat "$HOME/all")" open unattached "" "" --prompt="sample open > " --with-nth=5,4'
        result = subprocess.run(['sh', '-c', script], env=env, text=True, stdout=subprocess.PIPE)
        if result.stdout:
            print('Sample selection only: ' + ', '.join(row.split('\t')[1] for row in result.stdout.splitlines()))
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
