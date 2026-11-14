#!/usr/bin/env python3
"""Preview the shared pickers using synthetic data; selection prints identities."""
import argparse
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('open', 'list', 'resume', 'close', 'issues', 'single', 'multi', 'history', 'inline-single', 'inline-checklist', 'review')


def fixture():
    """Include duplicate slots across hosts and every attachment/interface state."""
    rows = [
        ('local', 'a', 'api-2', 'detached', 'active', 'Fix login · #42 merged, still in progress', 'codex', 'gpt-6.1-sol'),
        ('mini', 'b', 'api-2', 'detached', 'active', 'Review API ownership on remote host', 'claude', 'opus'),
        ('local', 'c', 'web-4', 'app', 'none', 'Polish dashboard — 界面 and keyboard navigation', 'codex', 'gpt-6-astra'),
        ('studio', 'd', 'web-7', 'app', 'active', 'Loaded desktop conversation', 'codex', 'gpt-6-astra'),
        ('local', 'e', 'api-5', 'attached', 'active', 'Attached CLI session', 'claude', 'sonnet'),
        ('mini', 'f', 'tools-3', 'detached', 'idle', 'Investigate a very long task title with unicode café and 日本語 for clipping', 'codex', 'gpt-6.1-sol'),
    ]
    return ''.join('\t'.join((host, sid, '/demo/' + slot.rpartition('-')[0], slot,
                             state, context, task, agent, model,
                             '-' if sid == 'f' else '2026-11-07T' + {'a':'22:42','b':'21:18','c':'19:05','d':'18:00','e':'17:00'}[sid] + ':00Z')) + '\n'
                   for host, sid, slot, state, context, task, agent, model in rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=PROFILES, default='open')
    args = parser.parse_args(argv)
    if args.profile.startswith('inline-') or args.profile == 'review':
        import runpy
        namespace = runpy.run_path(str(ROOT / 'bin/t'), run_name='preview_fixture')
        ui = namespace['_RailUI']()
        ui.raw()
        try:
            if args.profile == 'review':
                ui.page('Review changes', [ui.section('Agents'), *ui.row('Codex', 'gpt-6.1-sol'),
                        ui.section('Repositories'), *ui.row('API 界面', '/demo/api'),
                        *ui.row('Remote host', 'mini · example.invalid')], {'enter', 'esc', 'q'},
                        'enter apply · esc back · q cancel', ('enter', 'Apply changes', 'fixture only'))
            else:
                ui.pick('Choose agents', [('codex', 'Codex'), ('claude', 'Claude'), ('other', 'A long 日本語 agent label')],
                        multi=args.profile == 'inline-checklist', marked={0},
                        values={'codex': 'gpt-6.1-sol', 'claude': 'Opus', 'other': 'Available'},
                        primary=('enter', 'Continue', 'fixture only'))
        finally:
            ui.restore()
        return 0
    source = fixture()
    with tempfile.TemporaryDirectory(prefix='t-open-preview-') as temp:
        directory = Path(temp)
        env = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=temp, XDG_CACHE_HOME=temp,
                   T_HOME=str(ROOT), COLUMNS=str(shutil.get_terminal_size().columns),
                   T_PICKER_SESSION_FILTERS='open', T_PICKER_SOURCE_ROWS=source)
        if args.profile == 'history':
            # Exercise the actual history picker with no discovery or launch path.
            script = """import json, os, runpy
from pathlib import Path
from types import SimpleNamespace
root = Path(os.environ['T_HOME'])
history = runpy.run_path(str(root / 'libexec/t_history.py'))
records = []
for index, line in enumerate(os.environ['T_PICKER_SOURCE_ROWS'].splitlines()):
    host, sid, cwd, slot, state, context, task, agent, model, turn = line.split('\\t')
    records.append(dict(repo='demo/api', sid='fixture-' + sid, agent=agent,
        title=task, summary=task, model=model, interface='cli', last_turn=turn,
        updated=index, associations=[dict(workspace='/demo/api/2', slot='2', host=host)],
        sources=[], prs=[], mentions=[]))
app = history['HistoryCommand'](SimpleNamespace(repos={}, hosts={}), {'_t_source_root': lambda: str(root)})
try:
    while True:
        selected, key, query = app.pick(records, 'api 2 · SAMPLE ONLY', limit=3, query=locals().get('query', ''), multi=True)
        if key != 'ctrl-r':
            break
    print(json.dumps({'sample_selection': [row['sid'] for row in selected or []], 'action': key, 'launched': False}))
finally:
    app.store.close()
"""
            env.update(XDG_STATE_HOME=temp, XDG_DATA_HOME=temp, CODEX_HOME=temp,
                       T_RECOVERY_DISABLE='1')
            return subprocess.run([sys.executable, '-c', script], env=env, check=False).returncode
        if args.profile in ('single', 'multi', 'issues'):
            rows = 'a\tFix login · 界面 navigation\nb\tImprove repository discovery\nc\tDocument remote hosts\n'
            (directory / 'all').write_text(rows)
            env.pop('T_PICKER_SOURCE_ROWS', None)
            env.pop('T_PICKER_SESSION_FILTERS', None)
            if args.profile in ('single', 'multi'):
                script = '. "$T_HOME/ui/fzf.sh"; _t_fzf_' + args.profile + ' "choose" "Synthetic fixture · no actions" --delimiter="$(printf \"\\t\")" --with-nth=2 < "$HOME/all"'
            else:
                script = '. "$T_HOME/ui/fzf.sh"; _t_fzf_filtered "$(cat "$HOME/all")" "$(cat "$HOME/all")" "open issue" "Open issues" "Fixture repository" "" --with-nth=2'
            return subprocess.run(['sh', '-c', script], env=env, check=False).returncode
        result = subprocess.run([sys.executable, str(ROOT / 'bin/t'), '__session-picker-rows',
                                 '--remote', '--compact', '--header', '--last-turn'], input=source,
                                env=env, text=True, capture_output=True, check=True)
        columns, _, formatted = result.stdout.partition('\n')
        if args.profile == 'resume':
            columns = 'SESSION  LAST TURN (UTC)           WORKING ON'
            formatted = ''.join('\t'.join((repo, slot, sid, '/demo/' + repo, '2026-11-03',
                task, '-', '-', host, agent, turn, 'demo/' + repo, state,
                repo + '-' + slot + '  ' + (turn if turn != '-' else 'unknown') + '  [' + state + '] ' + task)) + '\n'
                for repo, slot, sid, task, host, agent, turn, state in (
                    ('api', '2', 'fixture-a', 'Fix login', 'local', 'codex', '2026-11-03T16:00:00Z', 'OPEN'),
                    ('web', '4', 'fixture-b', 'Polish dashboard', 'mini', 'claude', '2026-11-02T16:00:00Z', 'MERGED'),
                    ('tools', '3', 'fixture-c', 'Review CLI changes', 'local', 'codex', '-', 'NONE')))
            env['T_PICKER_SOURCE_ROWS'] = formatted
        env['T_PICKER_COLUMNS'] = columns
        hidden = {(fields[0], fields[3]) for fields in
                  (line.split('\t') for line in source.splitlines())
                  if fields[4] == 'attached' or fields[4:6] == ['app', 'active']}
        visible = [line for line in formatted.splitlines()
                   if tuple(line.split('\t')[:2]) not in hidden]
        if args.profile == 'list':
            visible = formatted.splitlines()
            env['T_PICKER_SESSION_FILTERS'] = 'list'
        (directory / 'all').write_text(formatted)
        (directory / 'default').write_text('\n'.join(visible) + '\n')
        if args.profile in ('resume', 'close'):
            env['T_PICKER_SESSION_FILTERS'] = 'resume' if args.profile == 'resume' else 'kill'
            if args.profile == 'close':
                env['T_PICKER_WITH_ATTACHED'] = formatted
                env['T_PICKER_WITH_PENDING'] = formatted
        # The shared profile owns keys, nested filters, theme and cancellation.
        script = '. "$T_HOME/ui/fzf.sh"\n_t_fzf_filtered "$(cat "$HOME/default")" "$(cat "$HOME/all")" open unattached "" "" --prompt="open preview > " --with-nth=5,4'
        if args.profile == 'list':
            script = script.replace('open unattached', 'open all').replace('open preview', 'list preview')
        elif args.profile != 'open':
            script = script.replace(' open unattached ', ' ' + ('resume' if args.profile == 'resume' else 'kill') + ' defaults ')
        if args.profile == 'resume':
            script = script.replace('--with-nth=5,4', '--with-nth=-1')
        script = script.replace('open preview', args.profile + ' preview')
        selected = subprocess.run(['sh', '-c', script], env=env, text=True,
                                  stdout=subprocess.PIPE, check=False)
        if selected.returncode:
            return selected.returncode
        for row in selected.stdout.splitlines():
            print('fixture:', '/'.join(row.split('\t')[:3]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
