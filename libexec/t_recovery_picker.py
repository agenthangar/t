"""Recovery's exact-conversation picker, using the shared fzf profile."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t_picker_filters import RESUME_AGENTS, pr_color, resume_action_rows
from t_picker_theme import control_rows, paint, picker_section_rows, picker_summary
from t_picker_viewport import project, search
from t_session_details import display_width, clip_columns
from t_session_sort import RESUME_SORTS, natural, sort_label

ROOT = Path(__file__).resolve().parents[1]
STATES = {'all': '', 'open': 'Open pane', 'unknown': 'Status unknown', 'restoring': 'Restoring'}
KEYS = {'repo': 'Ctrl-R Repo', 'agent': 'Ctrl-G Agent', 'host': 'Ctrl-O Host'}
MARKERS = {'Open pane': ('●', 'primary'), 'Status unknown': ('?', 'warning'), 'Restoring': ('↻', 'heading')}
COLUMNS = f'{"STATUS":<8}{"PRS":<8}{"REPO":<12}{"SLOT":<6}{"AGENT":<7}{"HOST":<9}{"LAST TURN":<18}TITLE'


def recovery():
    return runpy.run_path(str(ROOT/'libexec/t_auto_recovery.py'))


def clean(value):
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value or '')).replace('·', ' ').replace('—', '-')


def save(path, value):
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(value))
    pending.replace(path)


def catalogue():
    auto = recovery()
    owners, panes = auto['snapshot'](), auto['holding_panes']()
    records = []
    store = auto['helper']('t_history')['Store']()
    try:
        saved = {(r['agent'], r['sid']): r for r in store.records()}
    finally:
        store.close()
    for row in auto['status']()['sessions']:
        if not row.get('open'):
            continue
        auto['key'](row['agent'], row['sid'])
        matches = [o for o in owners if o['sid'] == row['sid'] and o['agent'] == row['agent']]
        holder = auto['find_holding'](row, panes)
        live = len(matches) == 1 and matches[0].get('cwd') == row['cwd']
        status = 'Restoring' if row.get('restoring') else 'Open pane' if live or holder else 'Status unknown'
        name = row.get('session', '').removeprefix('dev-')
        repo, _, slot = name.rpartition('-')
        detail = auto['holding_metadata'](row)
        records.append({**row, 'repo': repo or name, 'slot': slot, 'host': 'local',
                        'status': status, 'title': clean(detail.get('title') or saved.get((row['agent'], row['sid']), {}).get('title')),
                        'turn': clean(detail.get('last_turn')) or '-', 'prs': saved.get((row['agent'], row['sid']), {}).get('prs', []),
                        'holder': holder, 'owner': matches[0] if live else None})
    return records


def row_line(row, color):
    marker, role = MARKERS[row['status']]
    agent, agent_role = RESUME_AGENTS.get(row['agent'], ('<>', 'key'))
    prs = row.get('prs') or []
    pr = ' '.join(('↴' if p.get('state') == 'MERGED' else '○') + ' #' + str(p['number']) for p in prs) or '-'
    style = lambda text, name: paint(text, name) if color else text
    label = (style(marker, role) + ' '*7 + clip_columns(pr, 7).ljust(8) + clip_columns(clean(row['repo']), 11).ljust(12)
             + clip_columns(clean(row['slot']), 5).ljust(6) + style(agent.ljust(7), agent_role)
             + 'local'.ljust(9) + clip_columns(row['turn'], 17).ljust(18) + row['title'])
    # The first three fields remain stable across filter, sort, pan and refresh.
    return '\t'.join([row['agent'], row['sid'], row['generation'], row['repo'], row['slot'],
                      row['cwd'], row['status'], 'local', row['turn'], row['title'], label])


def refresh(directory):
    records = catalogue()
    save(directory/'recovery.json', records)
    color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    lines = ''.join(row_line(row, color)+'\n' for row in records)
    (directory/'source').write_text(lines)
    (directory/'all').write_text(lines)
    (directory/'refreshed').write_text(datetime.now().strftime('%H:%M'))


def legend(width, color):
    style = lambda text, role: paint(text, role) if color else text
    groups = [('Status', [style(marker+' '+label, role) for label, (marker, role) in MARKERS.items()]),
              ('PRs', [pr_color('↴ merged', '↴') if color else '↴ merged', '○ open', '◌ draft', '- no PR']),
              ('Agent', [style(mark+' '+agent.title(), role) for agent, (mark, role) in RESUME_AGENTS.items()])]
    cells = lambda s: display_width(re.sub(r'\x1b\[[0-9;]*m', '', s))
    widths = [max(cells(values[i]) for _, values in groups if i < len(values)) for i in range(4)]
    result = [style('LEGEND', 'heading')]
    for label, values in groups:
        line = label.ljust(8)
        for i, value in enumerate(values):
            if cells(line)+cells(value) > width and line.strip() != label:
                result.append(line.rstrip())
                line = ' '*8
            line += value + ' '*(widths[i]-cells(value)+2)
        result.append(line.rstrip())
    return result


def render(directory, action, width, facet=None, identity=()):
    records = json.loads((Path(os.environ['T_RECOVERY_DATA'])/'recovery.json').read_text())
    state_file = directory/'recovery-state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {
        'status': 'all', 'repo': None, 'agent': None, 'host': None, 'merged': False,
        'sort': 'turn', 'sorting': True}
    if action in ('clear-filters', 'reset-filters', 'off', 'on', 'default', 'all'):
        state.update(status='all', repo=None, agent=None, host=None, merged=False)
    elif action == 'state':
        modes = list(STATES)
        state['status'] = modes[(modes.index(state['status'])+1) % len(modes)]
    elif action == 'merged':
        state['merged'] = not state['merged']
    elif action == 'toggle-sort':
        state['sorting'] = not state['sorting']
    elif action == 'next-sort':
        modes = list(RESUME_SORTS)
        state['sort'] = modes[(modes.index(state['sort'])+1) % len(modes)]
        state['sorting'] = True
    elif action.startswith('sort-') and action[5:] in RESUME_SORTS:
        state.update(sort=action[5:], sorting=True)
    elif action.startswith('match-') and action[6:] in KEYS:
        name = action[6:]
        focused = next((r for r in records if (r['agent'], r['sid'], r['generation']) == tuple(identity)), None)
        state[name] = None if state[name] or not focused else focused[name]
    elif action == 'search':
        (directory/'pan-query').write_text(facet or '')
    elif action != 'current':
        raise ValueError('unknown recovery filter')
    if (directory/'pan-size').exists():
        height, width = map(int, (directory/'pan-size').read_text().split())
    else:
        height = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or 24)
    compact = width <= 40 or height <= 14
    color = os.environ.get('T_PICKER_COLOR') == '1'
    selected = [r for r in records if (state['status'] == 'all' or r['status'] == STATES[state['status']])
                and all(state[name] is None or state[name] == r[name] for name in KEYS)
                and (not state['merged'] or not any(p.get('state') == 'MERGED' for p in r['prs']))]
    if state['sorting']:
        key = {'session': 'slot', 'turn': 'turn'}.get(state['sort'], state['sort'])
        selected.sort(key=lambda r: natural(r.get(key, '')), reverse=key == 'turn')
    lines = [row_line(r, color) for r in selected]
    (directory/'all').write_text(''.join(row_line(r, color)+'\n' for r in records))
    if os.environ.get('T_PICKER_PAN') == '1':
        query = (directory/'pan-query').read_text() if (directory/'pan-query').exists() else ''
        lines = search(lines, query)
        lines, columns = project(directory, lines, COLUMNS, width, fixed=8)
    else:
        columns = COLUMNS
    (directory/'current').write_text(''.join(line+'\n' for line in lines))
    def controls(labels, active=()):
        return control_rows(labels, width, selected=active, compact=compact, color=color,
                            selected_role='current', separator='  ')
    filter_choices = [('Ctrl-T Status', STATES[state['status']]), ('Ctrl-D PRs', 'Unmerged' if state['merged'] else '')]
    filter_choices += [(label, state[name]) for name, label in KEYS.items()]
    choices = [label+(': '+str(value) if value else '') for label, value in filter_choices]
    active = [label for label, (_, value) in zip(choices, filter_choices) if value]
    sorts = [sort_label(mode).capitalize() for mode in RESUME_SORTS]
    refreshed = (Path(os.environ['T_RECOVERY_DATA'])/'refreshed').read_text()
    actions = resume_action_rows(width, compact, color, attached=True)
    actions = [line.replace('resume/attach', 'open/resume') for line in actions]
    status = [clean('Last refresh '+refreshed)+'  '+(paint('⌃U refresh', 'primary') if color else 'Ctrl-U refresh')]
    if not selected:
        status.append('No matching recovery sessions')
    sections = {
        'status': status,
        'filter': picker_summary('FILTER', [] if active else ['None'], True, width, compact, color),
        'filter-options': controls(choices, active),
        'filter-actions': controls(['Ctrl-F Clear filters', 'Ctrl-X Reset filters'], ['Ctrl-F Clear filters', 'Ctrl-X Reset filters']),
        'sort': picker_summary('SORT', [] if state['sorting'] else ['Paused'], True, width, compact, color),
        'sort-choices': controls(sorts, [sort_label(state['sort']).capitalize()] if state['sorting'] else []),
        'sort-options': controls(['Ctrl-Y Next sort', 'Ctrl-S '+('Pause' if state['sorting'] else 'Resume')+' sort']),
        'legend': legend(width, color), 'actions': actions, 'columns': [columns]}
    if height <= 14:
        sections['filter-options'] = controls(['Ctrl-T', 'Ctrl-D', 'Ctrl-R', 'Ctrl-G', 'Ctrl-O', 'Ctrl-F', 'Ctrl-X'])
        sections['filter-actions'] = []
        sections['sort-options'] = controls(['Ctrl-Y Next', 'Ctrl-S '+('Pause' if state['sorting'] else 'Resume')])
    rows = picker_section_rows(sections, short=height <= 14, budget=max(3, height-7),
                               optional=('legend', 'filter', 'sort', 'status', 'sort-options', 'sort-choices'))
    if os.environ.get('T_PICKER_LEGACY_COLUMNS') == '1':
        rows[-1] = '  '+rows[-1]
    (directory/'current-header').write_text('\n'.join(rows)+'\n')
    save(state_file, state)
    (directory/'hidden-count').write_text(str(len(records)-len(selected)))


def selected_command(row, cfg, api):
    auto = recovery()
    current = next((r for r in auto['status']()['sessions']
                    if r['agent'] == row['agent'] and r['sid'] == row['sid']), None)
    if (not current or not current.get('open') or current.get('generation') != row['generation']
            or current['cwd'] != row['cwd']):
        raise ValueError('Session changed. Refresh recovery and select it again.')
    if current.get('restoring'):
        raise ValueError('Session is restoring. Wait, then refresh recovery.')
    holder = auto['find_holding'](current)
    history = auto['helper']('t_history')['HistoryCommand'](cfg, api)
    if holder:
        if not auto['workspace_matches'](current):
            raise ValueError('Holding workspace changed. Inspect the saved conversation.')
        socket, pane = holder
        args = [current['session'], current['cwd'], current['sid'], socket, pane]
        # Reverify inside the attaching process, closing the selection/attach race.
        operation = 'switch-client' if os.environ.get('TMUX') else 'attach-session'
        snippet = ('TMUX='+shlex.quote(socket+',0,0')+' _t_auto_holding_verified '+shlex.join(args)+' && '
                   + 'tmux -S '+shlex.quote(socket)+' '+operation+' -t '+shlex.quote(pane))
        return history.shell(snippet)
    owners = [o for o in auto['snapshot']() if o['sid'] == row['sid'] and o['agent'] == row['agent']]
    if len(owners) > 1:
        raise ValueError('Session ownership is uncertain. Refresh recovery.')
    if owners:
        owner = owners[0]
        if owner.get('cwd') != row['cwd'] or not auto['workspace_matches'](current):
            raise ValueError('Session workspace changed. Refresh recovery.')
        return history.attach_command(dict(host='local', slot=owner['session'].removeprefix('dev-'),
            sid=owner['sid'], agent=owner['agent'], cwd=owner['cwd']))
    alias = cfg.repo_of_dir(row['cwd'])[0]
    if not alias:
        raise ValueError('Register this repository before resuming the conversation.')
    saved = [r for r in history.records(alias) if r['sid'] == row['sid'] and r['agent'] == row['agent']]
    if len(saved) != 1:
        raise ValueError('Saved conversation unavailable. Inspect it with t session history.')
    saved_row = auto['helper']('t_history')['bind_workspace'](saved[0], row['slot'], row['cwd'], None)
    owner = history.resume_with_repair(alias, saved_row, return_owner=True)
    return history.attach_command(owner)


def command(cfg, api):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Use a terminal to select sessions, or t system recovery run --all.')
    if not shutil.which('fzf'):
        raise ValueError('Session selection requires fzf.')
    with tempfile.TemporaryDirectory(prefix='t-recovery-') as name:
        directory = Path(name)
        refresh(directory)
        records = json.loads((directory/'recovery.json').read_text())
        if not records:
            print('No sessions to recover.')
            return 0
        env = {**os.environ, 'T_HOME': str(ROOT), 'T_PICKER_SESSION_FILTERS': 'recovery',
               'T_RECOVERY_DATA': name, 'T_PICKER_SOURCE_ROWS': (directory/'source').read_text(),
               'T_PICKER_COLUMNS': COLUMNS,
               'T_PICKER_REFRESH_CALLBACK': shlex.join([sys.executable, str(Path(__file__)), 'refresh', name])}
        script = '. "$1"; rows=$(cat "$2/source"); _t_fzf_filtered "$rows" "$rows" open/resume Recovery "" "" --delimiter="$(printf \'\\t\')" --with-nth=-1 --preview=\'printf "%s\\n" {7} {2} {6} {10}\''
        result = subprocess.run(['sh', '-c', script, 'sh', str(ROOT/'ui/fzf.sh'), name],
                                env=env, text=True, stdout=subprocess.PIPE)
        if result.returncode:
            return 130 if result.returncode in (1, 130) else result.returncode
        records = json.loads((directory/'recovery.json').read_text())
        picks = [tuple(line.split('\t')[:3]) for line in result.stdout.splitlines() if line]
        rows = [next((r for r in records if (r['agent'], r['sid'], r['generation']) == identity), None) for identity in picks]
        if not rows or any(row is None for row in rows):
            raise ValueError('Session selection changed. Run recovery again.')
        from t_session_batch import prepare
        results = prepare(rows, 'recovery')
        commands = []
        failed = False
        for row, result in zip(rows, results):
            if result['code']:
                errors = result['error'].strip().splitlines()
                print(f"{row['repo']} {row['slot']}  " + clean(errors[-1] if errors else 'Resume failed'), file=sys.stderr)
                failed = True
            else:
                commands.append(json.loads(result['output']))
        if any(r['code'] == 130 for r in results):
            for argv in commands:
                print('Ready to open  '+shlex.join(argv), file=sys.stderr)
            return 130
        for argv in commands[1:]:
            shell = recovery()['helper']('t_history')['HistoryCommand'](cfg, api).shell
            if subprocess.call(shell('_dev_open_tab '+shlex.quote(shlex.join(argv)))):
                print('Ready to open  '+shlex.join(argv), file=sys.stderr)
                failed = True
        if commands:
            return subprocess.call(commands[0]) or int(failed)
        return 130 if any(r['code'] == 130 for r in results) else int(failed)



if __name__ == '__main__':
    directory = Path(sys.argv[2])
    refresh(directory)
    # The shared refresh binding renders again using the preserved picker state.
