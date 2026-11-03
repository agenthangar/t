#!/usr/bin/env python3
"""Preview the real open picker using synthetic data; selection prints identities."""
import argparse
import os
from pathlib import Path
import subprocess
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
                             state, context, task, agent, model)) + '\n'
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
                   T_HOME=str(ROOT),
                   T_PICKER_SESSION_FILTERS='open', T_PICKER_SOURCE_ROWS=source)
        if args.profile in ('single', 'multi', 'issues', 'history'):
            rows = 'a\tFix login · 界面 navigation\nb\tImprove repository discovery\nc\tDocument remote hosts\n'
            (directory / 'all').write_text(rows)
            env.pop('T_PICKER_SOURCE_ROWS', None)
            env.pop('T_PICKER_SESSION_FILTERS', None)
            if args.profile in ('single', 'multi'):
                script = '. "$T_HOME/ui/fzf.sh"; _t_fzf_' + args.profile + ' "choose" "Synthetic fixture · no actions" --delimiter="$(printf \"\\t\")" --with-nth=2 < "$HOME/all"'
            elif args.profile == 'issues':
                script = '. "$T_HOME/ui/fzf.sh"; _t_fzf_filtered "$(cat "$HOME/all")" "$(cat "$HOME/all")" "open issue" "Open issues" "Fixture repository" "" --with-nth=2'
            else:
                (directory / 'page').write_text('Page 1 of 2 · 3 conversations\n')
                (directory / 'helper').write_text('#!/bin/sh\ncase $1 in next) echo "Page 2 of 2 · 3 conversations" > "$HOME/page";; previous) echo "Page 1 of 2 · 3 conversations" > "$HOME/page";; esac\n')
                script = """. "$T_HOME/ui/fzf.sh"; _t_fzf_paginated "$HOME/page" "$HOME/all" 'sh "$HOME/helper"' 'printf "Full synthetic conversation details\\n"' "" "history > " """
            return subprocess.run(['sh', '-c', script], env=env, check=False).returncode
        result = subprocess.run([sys.executable, str(ROOT / 'bin/t'), '__session-picker-rows',
                                 '--remote', '--compact', '--header'], input=source,
                                env=env, text=True, capture_output=True, check=True)
        columns, _, formatted = result.stdout.partition('\n')
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
            env.pop('T_PICKER_SOURCE_ROWS', None)
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
        selected = subprocess.run(['sh', '-c', script], env=env, text=True,
                                  stdout=subprocess.PIPE, check=False)
        if selected.returncode:
            return selected.returncode
        for row in selected.stdout.splitlines():
            print('fixture:', '/'.join(row.split('\t')[:3]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
