"""Filter a private picker snapshot without rediscovery or session actions."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from t_session_details import clip_columns, display_width, enrich, format_last_turn
from t_picker_theme import key_labels, paint

FACETS = {'agent': ('Ctrl-G', 'agent'), 'interface': ('Ctrl-D', 'UI'),
          'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo')}

OPEN_FACETS = {'agent': ('Ctrl-G', 'agent'), 'progress': ('Ctrl-P', 'progress'),
               'interface': ('Ctrl-D', 'UI'), 'model': ('Ctrl-B', 'model'),
               'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo'),
               'session': ('Ctrl-V', 'session'), 'context': ('Ctrl-E', 'context'),
               'task': ('Ctrl-W', 'task')}

PR_STATES = {'MERGED', 'OPEN', 'CLOSED', 'NONE', 'UNKNOWN', 'MIXED'}


def metadata(source, profile):
    result = {}
    for line in source.splitlines():
        fields = line.split('\t')
        if profile == 'resume':
            if len(fields) < 10:
                continue
            repo, slot, sid, _, _, _, location, _, origin, agent = fields[:10]
            host = (origin if origin not in ('', '-') else 'local') if location == '-' else location
            result[tuple(fields[:3])] = dict(agent=agent or 'unknown',
                host='local' if host == 'here' else host, repo=repo, session=slot,
                turn=fields[10] if len(fields) > 11 else '-',
                pr=fields[12] if len(fields) > 12 and fields[12] in PR_STATES else 'UNKNOWN')
        else:
            if len(fields) < 7:
                continue
            host, _, _, slot, state = fields[:5]
            repo = slot.partition(':')[0] if ':' in slot else slot.rpartition('-')[0] or slot
            result[(host, slot, state)] = dict(agent=fields[7] if len(fields) > 7 else 'claude',
                interface='desktop' if state == 'app' else 'cli', host=host, repo=repo, session=slot)
            if profile == 'list':
                result[(host, slot, state)].update(session=slot, turn=fields[9] if len(fields) > 9 else '-')
            if profile == 'open':
                result[(host, slot, state)].update(
                    model=fields[8] if len(fields) > 8 and fields[8] not in ('', '-') else 'unknown',
                    session=slot, context=fields[5] or 'unknown', task=fields[6] or '(no task)',
                    progress='in progress' if re.search(r'#\d+ merged, still in progress', fields[6]) else 'not marked')
    return result


RESUME_SORTS = {'turn': ('Alt-T', 'last turn'), 'repo': ('Alt-R', 'repo'),
                'agent': ('Alt-G', 'agent'), 'host': ('Alt-O', 'host'),
                'session': ('Alt-V', 'session')}


def natural(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', value))


def turn_epoch(value):
    try:
        stamp = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo else float('-inf')
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return float('-inf')


def sort_resume(rows, records, mode='turn'):
    def key(row):
        identity = tuple(row.split('\t')[:3])
        record = records.get(identity, {})
        primary = () if mode == 'turn' else natural(record.get(mode, 'unknown'))
        return primary, -turn_epoch(record.get('turn', '-')), identity
    return sorted(rows, key=key)


def resume_turns(source, home, codex_home, owners=''):
    """Enrich one snapshot, honoring exact saved paths and local owner identities."""
    rows = [line.split('\t') for line in source.splitlines() if line]
    owner_ids = {}
    for line in owners.splitlines():
        owner = line.split('\t')
        if len(owner) >= 8 and owner[0] == 'local' and owner[1] != '-':
            owner_ids[(owner[2], owner[3])] = owner[1]
    inputs = []
    for row in rows:
        # A remote live owner must not borrow a local transcript's activity.
        local = row[6] in ('-', 'here', 'fg:here', 'app:here')
        sid = row[2]
        if sid == '-' and row[6] in ('here', 'app:here'):
            sid = owner_ids.get((row[3], row[0] + '-' + row[1]), '-')
        inputs.append('\t'.join([sid if local else '-', row[3], row[1],
            'detached', 'unknown', row[5], row[9], row[10] if len(row) > 10 else '-']))
    details = enrich(inputs, home, codex_home)
    result = []
    for row, detail in zip(rows, details):
        turn = detail.split('\t')[-1]
        if row[7] == 'history' and len(row) > 11 and turn_epoch(row[11]) != float('-inf'):
            turn = row[11]
        when = format_last_turn(turn)
        row[4] = 'Last: ' + when if row[6] == '-' else row[4] + ' · Last: ' + when
        pr = row[13] if len(row) > 13 and row[13] in PR_STATES else 'UNKNOWN'
        result.append('\t'.join(row[:10] + [turn] + ([row[12]] if len(row) > 12 else ['-']) + [pr]))
    records = metadata('\n'.join(row + '\t' for row in result), 'resume')
    return sort_resume(result, records)


def choose(rows, records, filters):
    return [row for row in rows if all(value is None or
            records.get(tuple(row.split('\t')[:3]), {}).get(name, 'unknown') == value
            for name, value in filters.items())]


def pack(parts, width):
    lines = []
    for part in parts:
        part = clip_columns(part, width)
        if lines and display_width(lines[-1] + ' · ' + part) <= width:
            lines[-1] += ' · ' + part
        else:
            lines.append(part)
    return lines


def state_label(profile, mask):
    if profile == 'open':
        return {0: 'unattached', 1: 'all attachment states', 2: 'attached'}[mask]
    if profile == 'resume':
        return 'live included' if mask else 'saved only'
    return ('attached included' if mask & 1 else 'unattached') + ', ' + (
        'pending included' if mask & 2 else 'clean only')


def header(profile, mask, filters, hidden, columns, width, sort='turn', enabled=True, color=False, hide_merged=False, sort_enabled=True):
    if profile == 'list':
        return list_header(filters, hidden, columns, width, sort, color)
    compact = width <= 40 or int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    key = lambda text: key_labels(text, compact=compact)
    style = lambda text, role='hint': paint(text, role) if color else text
    def keys(parts, selected=()):
        active = {key(part) for part in selected}
        return [' · '.join(style(part, 'active' if part in active else 'action') for part in line.split(' · '))
                for line in pack([key(part) for part in parts], width)]
    lines = [] if compact else [style('HOTKEYS', 'heading')]
    lines += keys(['Enter/Esc' if compact else f'Enter {profile}/Esc', 'Space/Tab✓', 'k kill' if profile == 'open' else 'Ctrl-/'])
    if not compact:
        lines += ['']
    lines += keys(['Filters ' + ('ON' if enabled else 'OFF'), 'Ctrl-F' if compact else 'Ctrl-F toggle', 'Ctrl-X reset'], ['Filters ON'] if enabled else [])
    controls = ([] if compact else ['Ctrl-T state']) + [f'{shortcut} {label}' for name, (shortcut, label) in FACETS.items()
                                  if profile != 'resume' or name != 'interface']
    active = [f'{FACETS[name][0]} {FACETS[name][1]}' for name, value in filters.items()
              if enabled and value is not None and name in FACETS]
    if profile == 'resume':
        controls.append('Alt-M merged')
        if enabled and hide_merged:
            active.append('Alt-M merged')
    lines += keys(controls, active)
    summary = ', '.join(f'{name}={value}' for name, value in filters.items() if value is not None)
    summary = state_label(profile, mask) + (' · ' + summary if summary else '')
    if compact:
        summary = key('Ctrl-T ') + summary
    suffix = f' · {hidden} hidden'
    lines.append(style(clip_columns(summary if enabled else 'Saved filters paused', width - display_width(suffix)) + suffix, 'heading'))
    if not compact:
        lines += [style('Match highlighted row; repeat key to clear'), '']
    sorts = {name: f'{shortcut} {"turn" if compact and name == "turn" else label}'
             for name, (shortcut, label) in RESUME_SORTS.items() if (profile == 'resume' or name != 'turn') and (not compact or name in ('turn', 'repo', 'session'))}
    label = ('turn' if compact and sort == 'turn' else RESUME_SORTS[sort][1]) if sort_enabled else 'OFF'
    lines += keys(['Sort: ' + label, 'Alt-S toggle'], ['Sort: ' + label] if sort_enabled else [])
    lines += keys(list(sorts.values()), [sorts[sort]] if sort_enabled and sort in sorts else [])
    if not compact:
        lines += ['']
    if columns:
        lines.append(style(clip_columns(columns, width)))
    return '\n'.join(lines) + '\n'


def list_header(filters, hidden, columns, width, sort, color=False):
    compact = width <= 40 or int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    key = lambda text: key_labels(text, compact=compact)
    style = lambda text, role='hint': paint(text, role) if color else text
    actions = ['Enter/Esc' if compact else 'Enter open/Esc', 'Space/Tab✓' if compact else 'Space/Tab ✓', 'Ctrl-/' if compact else 'Ctrl-/ details']
    facets = [f'{shortcut} {label}' for shortcut, label in FACETS.values()]
    controls = ['Ctrl-A all', 'Ctrl-F reset'] + facets
    sorts = {name: f'{shortcut} {"turn" if compact and label == "last turn" else label}'
             for name, (shortcut, label) in RESUME_SORTS.items()}
    active = [f'{name}={value}' for name, value in filters.items() if value is not None]
    selected = {key(sorts[sort])}
    selected.update(key(f'{FACETS[name][0]} {FACETS[name][1]}')
                    for name, value in filters.items() if value is not None)
    if not active:
        selected.add(key('Ctrl-A all'))
    def keys(parts):
        return [' · '.join(style(part, 'heading' if part in ('FILTERS', 'SORT') else 'active' if part in selected else 'action')
                           for part in line.split(' · '))
                for line in pack([key(part) for part in parts], width)]
    lines = [] if compact else [style('HOTKEYS', 'heading')]
    lines += keys(actions)
    if not compact:
        lines += ['', style('FILTERS', 'heading')]
    lines += keys((['FILTERS'] if compact else []) + controls)
    suffix = f' · {hidden} hidden'
    summary = ' · '.join(active) if active else '0 filters'
    lines.append(style(clip_columns(summary, width - display_width(suffix)) + suffix, 'heading'))
    if not compact:
        lines += ['', style('SORT', 'heading')]
    lines += keys((['SORT'] if compact else []) + list(sorts.values()))
    label = 'turn' if compact and sort == 'turn' else RESUME_SORTS[sort][1]
    lines.append(style('Sort: ' + label, 'heading'))
    if not compact:
        lines.append('')
    if columns:
        lines.append(style(clip_columns(columns, width)))
    return '\n'.join(lines) + '\n'


def pick_menu(directory, choices, label, legend):
    options = directory / 'settings'
    options.write_text(''.join(f'{index}\t{text}\n' for index, text in enumerate(choices)))
    result = subprocess.run(['sh', '-c',
        '. "$1"; _t_fzf_single "choose" "$3" '
        '--delimiter="$(printf "\\t")" --with-nth=2 --prompt="$4 > " '
        '< "$2"', 'sh', str(Path(__file__).resolve().parents[1] / 'ui/fzf.sh'),
        str(options), legend, label], text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise RuntimeError('filter settings picker failed')
    return int(result.stdout.split('\t', 1)[0])


def settings(directory, profile, records, mask, filters, hide_merged=False):
    """Stage nested choices; only Apply commits, Esc keeps the original settings."""
    filters = dict(filters)
    names = list(filters)
    labels = OPEN_FACETS if profile == 'open' else FACETS
    while True:
        choices = ['Apply filters', 'State: ' + state_label(profile, mask)]
        choices += [labels[name][1] + ': ' + (filters[name] or 'all') for name in names]
        choices.append('Reset filters to defaults')
        chosen = pick_menu(directory, choices, 'Filters', 'Apply saves; Esc discards edits')
        if chosen is None:
            return None
        if chosen == 0:
            return mask, filters, hide_merged
        if chosen == len(choices) - 1:
            mask, filters = 0, dict.fromkeys(names)
            hide_merged = False
        elif chosen == 1:
            masks = [0, 2, 1] if profile == 'open' else list(range(4 if profile == 'kill' else 2))
            value = pick_menu(directory, [state_label(profile, value) for value in masks],
                              'State', 'Current: ' + state_label(profile, mask))
            if value is not None:
                mask = masks[value]
        else:
            name = names[chosen - 2]
            value = pick_value(directory, name, records, filters[name])
            if value is not None:
                filters[name] = value[0]


def pick_value(directory, name, records, current):
    """A shared single picker chooses an exact recorded value; Esc leaves it alone."""
    values = [None] + sorted({record[name] for record in records.values()})
    options = directory / 'values'
    options.write_text(''.join(f'{index}\t{value if value is not None else "All (clear filter)"}\n'
                               for index, value in enumerate(values)))
    label = OPEN_FACETS[name][1]
    legend = f'Current {label}: {current if current is not None else "all"}'
    if name == 'progress':
        legend += '\nIn progress = work continued after a merged PR; not marked is not proof of completion.'
    result = subprocess.run(['sh', '-c',
        '. "$1"; _t_fzf_single "apply filter" "$3" '
        '--delimiter="$(printf "\\t")" --with-nth=2 --prompt="$4 > " '
        '< "$2"',
        'sh', str(Path(__file__).resolve().parents[1] / 'ui/fzf.sh'), str(options), legend, label], text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise RuntimeError('column filter picker failed')
    return (values[int(result.stdout.split('\t', 1)[0])],)


def render(directory, profile, action, width, columns='', facet=None, identity=()):
    directory = Path(directory)
    records = metadata((directory / 'source').read_text(), profile)
    names = list(OPEN_FACETS) if profile == 'open' else [
        name for name in FACETS if profile != 'resume' or name != 'interface']
    state_file = directory / 'facets.json'
    filters = json.loads(state_file.read_text()) if state_file.exists() else dict.fromkeys(names)
    mask_file = directory / 'show-all'
    mask = int(mask_file.read_text().strip() or 0) if mask_file.exists() else 0
    sort_file = directory / 'sort'
    sort = sort_file.read_text().strip() if sort_file.exists() else ('turn' if profile in ('resume', 'list') else 'session')
    sort_enabled_file = directory / 'sort-enabled'
    sort_enabled = sort_enabled_file.read_text() == '1' if sort_enabled_file.exists() else profile in ('resume', 'list')
    enabled_file = directory / 'filters-enabled'
    enabled = enabled_file.read_text() == '1' if enabled_file.exists() else True
    merged_file = directory / 'hide-merged'
    hide_merged = merged_file.exists() and merged_file.read_text() == '1'
    maximum = 3 if profile == 'kill' else 1
    if action == 'toggle-filters':
        enabled = not enabled
    elif action == 'toggle-sort':
        sort_enabled = not sort_enabled
    elif action == 'state':
        mask = (mask + 1) % (3 if profile == 'open' else maximum + 1)
        enabled = True
    elif action.startswith('match-') and action[6:] in names:
        name = action[6:]
        if filters[name] is not None:
            filters[name] = None
        elif tuple(identity) in records:
            filters[name] = records[tuple(identity)].get(name, 'unknown')
        enabled = True
    elif action in ('off', 'on'):
        enabled = action == 'on'
    elif action == 'settings':
        chosen = settings(directory, profile, records, mask, filters, hide_merged)
        if chosen is not None:
            mask, filters, hide_merged = chosen
            enabled = True
    elif action in ('all', 'default', 'reset-filters'):
        mask = maximum if action == 'all' else 0
        filters = dict.fromkeys(names)
        hide_merged = False
        if action == 'reset-filters':
            enabled = True
    elif profile == 'resume' and action == 'merged':
        hide_merged = not (enabled and hide_merged)
        enabled = True
    elif action.startswith('sort-') and action[5:] in RESUME_SORTS:
        sort = action[5:]
        sort_enabled = True
    elif action == 'first' and profile == 'open':
        mask = 2  # Attached-only; repeated presses are idempotent.
    elif action == 'unattached' and profile == 'open':
        mask = 0  # Switch attachment without resetting column choices.
    elif action == 'pick' and facet in names:
        chosen = pick_value(directory, facet, records, filters[facet])
        if chosen is not None:
            filters[facet] = chosen[0]
    elif action in ('first', 'second'):
        mask ^= 1 if action == 'first' else 2
    elif action in names:
        if profile == 'list':
            if filters[action] is not None:
                filters[action] = None
            else:
                chosen = pick_value(directory, action, records, None)
                if chosen is not None:
                    filters[action] = chosen[0]
        else:
            values = [None] + sorted({record.get(action, 'unknown') for record in records.values()})
            filters[action] = values[(values.index(filters[action]) + 1) % len(values)]
    elif action != 'current':
        raise ValueError('unknown picker filter: ' + action)
    if mask > (2 if profile == 'open' else maximum):
        raise ValueError('unsupported state filter')
    if not enabled:
        rows = (directory / 'all').read_text().splitlines()
    elif profile == 'open' and mask == 2:
        unattached = set((directory / 'rows-0').read_text().splitlines())
        rows = [row for row in (directory / 'all').read_text().splitlines() if row not in unattached]
    else:
        rows = (directory / f'rows-{mask}').read_text().splitlines()
    selected = choose(rows, records, filters) if enabled else rows
    if profile in ('resume', 'list'):
        if profile == 'resume' and enabled and hide_merged:
            selected = [row for row in selected if records.get(tuple(row.split('\t')[:3]), {}).get('pr') != 'MERGED']
        if action == 'default':
            sort = 'turn'
        if sort_enabled:
            selected = sort_resume(selected, records, sort)
        merged_file.write_text('1' if hide_merged else '0')
    if profile not in ('resume', 'list') and sort_enabled:
        selected = sort_resume(selected, records, sort)
    sort_file.write_text(sort)
    sort_enabled_file.write_text('1' if sort_enabled else '0')
    total = len((directory / 'all').read_text().splitlines())
    (directory / 'current').write_text(''.join(row + '\n' for row in selected))
    warning = ''
    if profile == 'resume':
        # The invocation snapshot persists through every filter/sort/reset reload.
        hosts = (directory / 'auth-hosts').read_text().splitlines() if (directory / 'auth-hosts').exists() else []
        hosts = [host for host in hosts if host]
        if hosts:
            summary = 'GitHub auth failed: ' + ', '.join(hosts)
            warning = clip_columns(summary, width) + '\n'
    controls = header(profile, mask, filters, total - len(selected), columns, width,
                      sort, enabled, os.environ.get('T_PICKER_COLOR') == '1', hide_merged, sort_enabled)
    (directory / 'current-header').write_text(warning + controls)
    enabled_file.write_text('1' if enabled else '0')
    mask_file.write_text(str(mask))
    state_file.write_text(json.dumps(filters))


if __name__ == '__main__':
    if sys.argv[1] == '--resume-turns':
        print('\n'.join(resume_turns(sys.stdin.read(), os.environ['HOME'],
            os.environ.get('CODEX_HOME', str(Path.home() / '.codex')),
            os.environ.get('T_RESUME_OWNER_ROWS', ''))))
        sys.exit(0)
    render(os.environ['T_PICKER_VIEWS'], os.environ['T_PICKER_SESSION_FILTERS'], sys.argv[1],
           int(os.environ['T_PICKER_HEADER_WIDTH']), os.environ.get('T_PICKER_COLUMNS', ''),
           sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3:6])
